"""从一篇文章派生各渠道的内容原子 —— 复用同一套 eval 校验

## 一次派生的完整链路（和博客那条闸门同构）
    文章（内容原子）
      → ① 按渠道 spec 派生（一次便宜的模型调用）
      → ② eval.check_channel_content()  ★和博客走同一个函数、同一张严重度表★
      → ③ hard 失败 → 把 failures 当指令**重派一次**（上限 1 次）
      → ④ 还不过 → 标成 blocked 交回去，**不阻断文章本身**

第 ④ 条是刻意的：文章已经过了自己的闸门、可以发布。渠道派生失败不该把
文章一起拖下水 —— 它是**附加产物**，不是文章的一部分。所以这里的失败语义是
"这条渠道原子不可用"，而不是"这次生成失败了"。

## 成本形状
每个渠道约 500 in / 200 out（gpt-4.1-mini）≈ **$0.0005**。
输入只有标题 + 摘要 + 目录片段，**正文一个字都不进 prompt** ——
派生一条邮件主题不需要读完 1200 词，需要的是"这篇在讲什么 + 真实价格"。
和 heading_fixer 是同一条判据：把最小必要输入塞进 prompt，不多给。

## 为什么用一张 spec 表而不是 if/elif
加一个渠道 = 加一条数据（不是加一个分支）。每条 spec 自带：
输出字段、字段上限、允不允许 emoji、哪些字段算"门面"。
校验和渲染都从 spec 读，所以新渠道**不需要碰这个文件之外的任何代码**。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core import merchant_config
from eval.deterministic import DeterministicReport, check_channel_content
from llm import route
from retrieval.catalog import build_catalog_brief, load_catalog

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelSpec:
    """一个渠道的派生规格 —— 加渠道就是往 _SPECS 里加一条这个。

    name           渠道 key（也是 merchant.json 里 channels 数组的值）
    label          人话名字，进日志和 Slack 卡片
    fields         输出字段 → 字符限制（None = 不限）。**限制会写进 prompt，也会在解析后校验**
    headline_fields 哪些字段算"门面"（进收件箱预览 / 信息流第一行）→ 品牌禁词在这里是 hard
    allow_emoji    这个渠道允不允许 emoji（社媒允许，邮件不允许）
    guidance       给模型的渠道特有指令
    """

    name: str
    label: str
    fields: dict[str, int | None]
    headline_fields: tuple[str, ...]
    allow_emoji: bool
    guidance: str


_SPECS: dict[str, ChannelSpec] = {
    "email": ChannelSpec(
        name="email",
        label="Email (Klaviyo)",
        # 上限不是随便定的：主题行超过约 50 字符在手机收件箱会被截断，
        # preview_text 超过约 100 同理。这两个数字是渠道的**展示约束**，
        # 和博客摘要 150-160 是同一类事实（不是审美偏好）。
        fields={"subject": 50, "preview_text": 100, "body": 700},
        headline_fields=("subject", "preview_text"),
        allow_emoji=False,          # 邮件客户端渲染差异大，且部分企业网关会拦
        guidance=(
            "Write a Klaviyo campaign email. The subject line must earn the open without "
            "clickbait: name the concrete thing the reader gets. preview_text continues the "
            "subject, it does not repeat it. body is 2-3 short paragraphs in plain sentences, "
            "ending with one clear call to action. No markdown, no headings, no emoji."
        ),
    ),
    "social": ChannelSpec(
        name="social",
        label="Instagram caption",
        fields={"caption": 300, "hashtags": 120},
        headline_fields=("caption",),
        allow_emoji=True,           # ⭐ 社媒里 emoji 是正常表达，不是缺陷
        guidance=(
            "Write one Instagram caption. Open with a line that works on its own, because "
            "everything after it is hidden behind 'more'. Conversational, no hard sell. "
            "A couple of emoji are fine. hashtags is 4-6 space-separated tags, each starting "
            "with '#', relevant rather than generic."
        ),
    ),
}


@dataclass
class ChannelAtom:
    """一条派生出来的渠道原子 + 它自己的校验报告。

    blocked=True 表示这条原子没通过 hard 校验（比如编了个价格）——
    **文章本身不受影响**，只是这条不可用。所以调用方拿到的是"部分成功"，
    而不是整体失败：这符合渠道派生是附加产物的定位。
    """

    channel: str
    label: str
    content: dict[str, str] = field(default_factory=dict)
    report: DeterministicReport | None = None
    blocked: bool = False
    attempts: int = 0
    cost: float = 0.0


def _truncate_note(spec: ChannelSpec, content: dict[str, str]) -> list[str]:
    """检查字段长度是否超限。产物是超限说明列表（空 = 全合规）。

    ⚠️ 这里**只报告不截断**，和摘要那边的自动裁剪刻意不同：
    邮件主题行截断会把句子切断（"Save 20% on the tee tha"），语义直接坏掉；
    而博客摘要是完整句子的尾部裁剪，损失小。**能不能安全机械修，取决于字段的语义结构**，
    不是所有超长都能一刀切。所以这里把它交回模型重写。
    """
    notes = []
    for f, limit in spec.fields.items():
        if limit is None:
            continue
        n = len(content.get(f, ""))
        if n > limit:
            notes.append(f"{f} 有 {n} 字符，超过 {limit} 上限，需要压缩")
    return notes


def _build_prompt(spec: ChannelSpec, blog_data: dict, topic: dict,
                  catalog_brief: str, fix_issues: list[str]) -> str:
    """拼派生 prompt。★正文一个字都不进★ —— 只给标题、摘要、目录片段。"""
    field_lines = "\n".join(
        f'  "{f}": "…"' + (f"   (max {lim} characters)" if lim else "")
        for f, lim in spec.fields.items()
    )
    fix_block = ""
    if fix_issues:
        # 重派时把上一轮的失败当**定点指令**塞进去 —— 和博客那条 rewrite 路径同一个思路
        bullets = "\n".join(f"- {i}" for i in fix_issues)
        fix_block = (
            "\n## Your previous attempt was rejected by an automated check\n"
            f"{bullets}\n"
            "Fix exactly these problems. Do not change anything that was not flagged.\n"
        )

    return f"""Derive one piece of channel content from an article that has already been published.

## Article (this is the single source of truth — do not add facts it does not contain)
Title: {blog_data.get('title', '')}
Summary: {blog_data.get('excerpt', '')}
Primary keyword: {topic.get('primary_keyword', '')}
{catalog_brief}
## Channel
{spec.guidance}
{fix_block}
## Output Format (JSON)
Return ONLY valid JSON with exactly these keys:
{{
{field_lines}
}}

Hard rules, enforced by an automated check after you answer:
- Every price you mention must come from the product catalog above, quoted exactly.
  If the number you want is not there, describe it qualitatively instead.
- No absolute promises ("100% guaranteed", "never fades", "lasts forever").
- Never describe a body as a problem to hide or flatter."""


def derive_channel(
    merchant_id: str,
    blog_data: dict,
    topic: dict,
    channel: str,
    *,
    merchant_cfg: dict,
    max_attempts: int = 2,
) -> ChannelAtom:
    """派生单个渠道的内容原子，并用**同一套 eval** 校验。

    Args:
        merchant_id:  商家标识（记账 + 人格）
        blog_data:    已定稿的文章（只读 title / excerpt —— 正文不进 prompt）
        topic:        选题 dict
        channel:      "email" | "social"
        merchant_cfg: 商家配置（取 brand_voice 禁词）
        max_attempts: 含首次在内的最多尝试次数。默认 2 = 首次 + 一次定点重派。
                      为什么是 2 而不是 3：每次约 $0.0005，但**重派两次还不过说明
                      问题不在措辞而在事实**（比如目录里根本没有它想说的价格），
                      再试是浪费。上限低一点，失败就交回去让人看。

    Returns:
        ChannelAtom —— blocked=True 表示这条不可用，**文章本身不受影响**。
    """
    spec = _SPECS.get(channel)
    if not spec:
        log.warning("[%s] 未知渠道 %r，跳过（已知：%s）",
                    merchant_id, channel, ", ".join(_SPECS))
        return ChannelAtom(channel=channel, label=channel, blocked=True)

    brand_terms = (merchant_cfg.get("brand_voice") or {}).get("forbidden_terms") or []
    catalog = load_catalog(merchant_id)
    # 目录片段：给它真实价格（预防）。top_n 给 2 —— 一条邮件用不到 3 个商品，
    # 而 prompt 越短越便宜；出口还有白名单校验兜着（保证）。
    catalog_brief = build_catalog_brief(topic, catalog, top_n=2) if catalog else ""

    atom = ChannelAtom(channel=spec.name, label=spec.label)
    mcfg = merchant_config.get_merchant(merchant_id) or {"merchant_id": merchant_id}
    issues: list[str] = []

    for attempt in range(1, max_attempts + 1):
        atom.attempts = attempt
        prompt = _build_prompt(spec, blog_data, topic, catalog_brief, issues)
        try:
            result = route("channel", mcfg).complete(
                system=(f"You adapt already-written content for one specific channel. "
                        f"You never invent facts. You return only JSON."),
                user=prompt,
                temperature=0.7,
                max_tokens=900,
                json_mode=True,
                label=f"channel_{spec.name}",
            )
        except Exception as e:
            # 含预算闸门的 BudgetExceeded。渠道派生是附加产物，绝不能让文章失败。
            log.warning("[%s] 渠道派生调用失败 %s（第 %d 次）: %s: %s",
                        merchant_id, spec.name, attempt, type(e).__name__, e)
            atom.blocked = True
            return atom

        atom.cost += result.cost

        # ── 解析 + 形状校验 ────────────────────────────────────────
        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError:
            log.warning("[%s] 渠道派生 %s 返回的不是合法 JSON（第 %d 次）",
                        merchant_id, spec.name, attempt)
            issues = ["Return valid JSON only."]
            continue
        if not isinstance(parsed, dict):
            # json_mode 只保证合法 JSON，不保证是对象
            issues = ["Return a JSON object, not an array."]
            continue

        content = {
            f: re.sub(r"\s+", " ", str(parsed.get(f, ""))).strip()
            for f in spec.fields
        }
        missing = [f for f, v in content.items() if not v]
        if missing:
            issues = [f"Field '{f}' was empty — it is required." for f in missing]
            continue

        atom.content = content

        # ── ★ 同一套 eval 校验 ★ ──────────────────────────────────
        # 门面/正文的划分从 spec 读 —— 邮件的门面是 subject+preview_text，
        # 社媒的门面是 caption 本身。品牌禁词在门面是 hard、在正文是 soft。
        headline = {f: v for f, v in content.items() if f in spec.headline_fields}
        body = {f: v for f, v in content.items() if f not in spec.headline_fields}
        report = check_channel_content(
            headline, body,
            brand_terms=brand_terms, catalog=catalog, allow_emoji=spec.allow_emoji,
        )
        length_notes = _truncate_note(spec, content)
        atom.report = report

        if report.hard_ok and not length_notes:
            log.info("[%s] 渠道派生 %s 通过（第 %d 次尝试，累计 $%.4f）",
                     merchant_id, spec.label, attempt, atom.cost)
            for f, v in content.items():
                log.info("[%s]   %s: %s", merchant_id, f, v[:90])
            return atom

        # 没过 → 把失败当定点指令，重派一次
        issues = list(report.hard_failures) + length_notes
        log.warning("[%s] 渠道派生 %s 未通过（第 %d 次）：%s",
                    merchant_id, spec.label, attempt, "；".join(issues)[:200])

    # 尝试用尽仍不过 —— 标 blocked 交回去。文章本身不受影响。
    atom.blocked = True
    log.warning("[%s] 渠道派生 %s 最终未通过，标记为不可用（文章本身不受影响）",
                merchant_id, spec.label)
    return atom


def derive_all(
    merchant_id: str,
    blog_data: dict,
    topic: dict,
    *,
    merchant_cfg: dict,
) -> list[ChannelAtom]:
    """按 merchant.json 里的 channels 配置派生全部渠道。没配就返回空列表。

    这是 pipeline 唯一需要调的入口。**租户级可选能力** ——
    和 brand_voice / catalog 一样，没配的商家一行代码都不会跑到。
    """
    channels = merchant_cfg.get("channels") or []
    if not channels:
        return []
    log.info("[%s] 开始渠道派生：%s", merchant_id, ", ".join(channels))
    return [
        derive_channel(merchant_id, blog_data, topic, ch, merchant_cfg=merchant_cfg)
        for ch in channels
    ]
