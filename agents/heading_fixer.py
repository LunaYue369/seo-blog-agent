"""定点修复 Agent — 只重写出问题的那几个小标题，正文一个字不动

## 为什么需要这个东西（2026-07-27 实测出来的缺口）
h2 关键词堆砌是 **soft** 级（读起来蠢，但文章能发，不该阻断出稿）。而 soft 失败的
修复路径原本只有一条：「因为别的原因已经要重写了 → 顺路把 soft 一起塞进 issues」。
这条路径有个前提：**reviewer 会经常判不过**。实测它不成立 ——
连续两篇 reviewer 都给 90 分直接通过，于是：

    soft 不阻断 → 没有重写 → soft 搭不上车 → **这类问题无限累积，一条都修不到**

所以补这一条路：不阻断，但花一次很便宜的模型调用把它修掉。

## 核心判据：修复范围应该匹配问题范围
    问题只在标题里 → 就只改标题，不要整篇重写。

## ⚠️ 但"问题范围"是整个标题集合，不是被点名的那几个（2026-07-27 实测纠正）
第一版按 metric 里的标题序号，**只把被点名的 3 个标题发给模型**。结果修完堆砌还在：
模型换掉了旧的重复短语，又和**它没看见的那 5 个标题**撞出了新的重复短语。

    ⭐ **重复是集合的属性，不是元素的属性。**

要判断"这批标题之间有没有重复"，模型必须看到**全部**标题。所以现在发全集
（仍然排除受保护的 FAQ 标题）—— 范围依然是"只有标题、没有正文"，
成本还是同一个数量级（~$0.0003），但它现在有足够信息做全局去重。

| | 整篇重写（rewrite_blog） | 定点改标题（本模块） |
|---|---|---|
| 成本 | 约 $0.028（gpt-4.1，全文进出） | 约 $0.0003（gpt-4.1-mini，只有标题） |
| 风险 | 可能把已通过的部分改坏 | 正文一个字不变，风险只限于标题 |
| 输入 | 全文 + 所有 issues | 那几个标题 + 一条堆砌指令 |

两个数量级的差价，来自"别把整篇文章塞进 prompt"这一个决定。

## 和确定性修复（eval/repair.py）的分工
    eval/repair.py   删字/裁剪 —— **不需要新信息**，代码自己能做 → 零成本、结果确定
    本模块           换措辞   —— **需要新语言**，代码编不出来 → 必须问模型，但只问最小范围

判据还是那条不对称原则：删是代码的活，写是模型的活。

## 失败即放弃（fail-safe，不是 fail-fast）
校验不过（数量不对 / 有空标题 / 换了等于没换）→ **保留原标题**，只记 warning。
理由：这是个"锦上添花"的修复，不是正确性要求。为了修一个 soft 问题而把文章搞坏，
是明显的亏本交易 —— 所以这里的失败处理刻意选"什么都不做"。
"""

from __future__ import annotations

import json
import logging
import re

import config as cfg
from core import merchant_config
from llm import route

log = logging.getLogger(__name__)

# 匹配 h2 标签（含属性），分三段捕获：开标签 / 内部内容 / 闭标签。
# 之所以要保留开标签原样：模板和布局会给 h2 加 class，替换时不能把属性冲掉。
_H2_RE = re.compile(r"(<h2\b[^>]*>)(.*?)(</h2>)", re.S | re.I)

# 布局强制要求的标题，**绝不改**：
#   - "Frequently Asked Questions" 是 layouts/*.md 里写死的 FAQ 段落标题，
#     改了会破坏 FAQ 结构（前端和 SEO 的 FAQ schema 都认它）
# 用宽松匹配（小写去空格后判包含），因为模型偶尔会写成 "Frequently Asked Questions:"
_PROTECTED_HEADINGS = ("frequentlyaskedquestions",)

# 标题里的**结构性序号前缀**，必须原样保住。
#
# ⚠️ 这个正则被真实回归打了两次，两次都是"覆盖不全"：
#   第一次：prompt 里写了 "no numbering"（本意是别让模型自己加列表标记），结果在
#           listicle 布局（"7 Tips for…"）下把真正的 `1. ` `2. ` 删掉了 → 列表断了。
#           修法：代码摘下来、改完贴回去。
#   第二次：正则只认裸编号 `1. `，而 **how-to 布局用的是 `Step 1: `** ——
#           五个步骤的序号全丢，"分步指南"变成了一堆平行小标题。
#
# 教训不是"再补一种格式"，而是：**这类前缀由布局决定，而我们有 6 种布局**。
# 所以这里按"序数词 + 数字 + 分隔符"的**通用形状**匹配，而不是穷举具体写法：
#   1. / 2) / 3、 / Step 1: / Step 2 — / Part 3: / Tip 4. / Phase 5 -  / #6:
# 两条防误伤的约束：
#   · 数字最多两位（`2026 in review` 这种年份开头的标题不会被当成序号）
#   · 数字后**必须**有分隔符（`5 Ways to Save` 不会被当成序号，因为 "5" 后面是空格+字母）
_NUMBER_PREFIX_RE = re.compile(
    r"^\s*((?:step|part|tip|phase|stage|chapter|section)?\s*#?\s*\d{1,2}\s*[.)、:：\-–—]\s*)",
    re.I,
)


def _normalize(text: str) -> str:
    """标题归一化 —— 只用于"是不是受保护标题"和"改了没改"的比较。"""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"<[^>]+>", "", text).lower())


def _extract_headings(content_html: str) -> list[tuple[int, str]]:
    """抽出所有 h2 的**纯文本内容**，返回 [(序号, 文本), ...]。

    序号是从 0 开始的出现顺序 —— 必须和 `_check_h2_keyword_stuffing` 里
    `phrase_to_headings` 用的序号口径完全一致，否则"要改第几个"会对错位。
    两处都是按 `find_all("h2")` / `_H2_RE` 的出现顺序，天然一致。
    """
    out = []
    for i, m in enumerate(_H2_RE.finditer(content_html)):
        inner = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        out.append((i, inner))
    return out


def _apply_headings(content_html: str, replacements: dict[int, str]) -> str:
    """把第 i 个 h2 的内部文本换成新文本，**保留开闭标签和属性原样**。

    用 re.sub 的回调按出现顺序计数，而不是做字符串查找替换 ——
    查找替换会在"两个标题文字相同"时改错地方（而堆砌场景下标题高度相似，
    正是最容易撞上这种情况的场景）。
    """
    counter = {"i": -1}

    def _repl(m: re.Match) -> str:
        counter["i"] += 1
        i = counter["i"]
        if i in replacements:
            return f"{m.group(1)}{replacements[i]}{m.group(3)}"
        return m.group(0)

    return _H2_RE.sub(_repl, content_html)


def fix_stuffed_headings(
    merchant_id: str,
    blog_data: dict,
    stuffing_metric: dict,
    topic: dict,
) -> tuple[bool, str]:
    """把重复堆砌的小标题重写掉。★原地修改 blog_data["content_html"]★

    Args:
        merchant_id:     商家标识（记账 + 人格）
        blog_data:       稿件 dict，原地改
        stuffing_metric: `_check_h2_keyword_stuffing` 的 metric，形如
                         {"regular fit t": [0, 1, 2, 4], "fit t shirt": [1, 2, 3]}
                         —— 键是重复的短语，值是**出现它的标题序号**
        topic:           选题 dict（给模型关键词上下文，让它换措辞但别丢 SEO 意图）

    Returns:
        (是否真的改了, 人话说明) —— 说明会进日志和 Slack 卡片的 🔧 那一列
    """
    content_html = blog_data.get("content_html") or ""
    headings = _extract_headings(content_html)
    if not headings:
        return False, ""

    # ── Step 1: 算出「要改哪些」= **整个标题集合**（除受保护的）────────
    # 为什么是全集而不是 metric 里点名的那几个：**重复是集合的属性**。
    # 只发被点名的标题，模型会和它没看见的那些撞出新的重复（第一版实测就是这样）。
    # 受保护的标题（FAQ）剔除 —— 布局强制要求它叫那个名字，改了破坏 FAQ 结构。
    by_index = dict(headings)
    target_idx = [
        i for i, text in headings
        if _normalize(text) not in _PROTECTED_HEADINGS
    ]
    if len(target_idx) < 2:
        # 少于两个就没有"重复"可谈了（剔除保护标题后可能只剩一个）→ 不值得花一次调用
        return False, ""

    phrases = list(stuffing_metric.keys())
    targets = [(i, by_index[i]) for i in target_idx]

    # 中间产物 prefixes：{标题序号: 原有的编号前缀}。
    # listicle 布局（"7 Tips for…"）的标题自带 `1. ` `2. ` 这种结构编号，
    # 模型改写时会丢掉它 → 列表就断了。所以**先摘下来，改完再原样贴回去**。
    # 发给模型的是去掉编号的纯标题：编号是结构不是内容，让模型看见只会干扰它。
    prefixes: dict[int, str] = {}
    stripped: list[tuple[int, str]] = []
    for i, text in targets:
        m = _NUMBER_PREFIX_RE.match(text)
        if m:
            prefixes[i] = m.group(1)
            stripped.append((i, text[m.end():]))
        else:
            stripped.append((i, text))
    targets = stripped

    # ── Step 2: 只把那几个标题发给模型 ────────────────────────────────
    # ★ 全文一个字都不进 prompt ★ —— 这是成本从 $0.028 降到 $0.0003 的唯一原因。
    # 模型不需要看正文也能改标题：它需要知道的是"每个标题在回答什么问题"，
    # 而标题自己就写着。给它正文只会让它顺手改别的东西。
    numbered = "\n".join(f"{n}. {text}" for n, (_, text) in enumerate(targets, 1))
    phrase_list = ", ".join(f'"{p}"' for p in phrases)

    system_prompt = (
        "You rewrite blog section headings. You change ONLY the headings you are given. "
        "You never comment, never explain, never touch anything else."
    )
    user_prompt = f"""Below are ALL {len(targets)} section headings from one article.
Some of them repeat the same phrase(s) — {phrase_list} — which reads as keyword stuffing.

Headings:
{numbered}

Rewrite them as a SET so that:
1. No phrase of 3+ words is shared by more than 2 headings **across the whole list**.
   This is a property of the set, not of individual headings — check every heading
   against every other one before you answer.
2. Vary the sentence shape, not just the verb. "How to know / How to get /
   How to figure out" is the same heading three times, which is exactly the problem.
3. Each heading still says what that section actually answers. Do not make them generic.
4. Keep the topic's intent: primary keyword "{topic.get('primary_keyword', '')}".
   The keyword (or its head noun) may appear in AT MOST 2 headings — not in all of them.
5. Plain text only: no emoji, no markdown, no leading numbers, no trailing colon.
6. Similar length to the original (these are H2 headings, not sentences).
7. If a heading is already fine and creates no repetition, return it unchanged.

Return ONLY valid JSON, an object whose keys are the numbers above as strings:
{{"1": "heading", "2": "heading"}}  — include an entry for every number."""

    mcfg = merchant_config.get_merchant(merchant_id) or {"merchant_id": merchant_id}
    try:
        result = route("headings", mcfg).complete(
            system=system_prompt,
            user=user_prompt,
            temperature=0.6,
            max_tokens=800,
            json_mode=True,
            label="heading_fixer",
        )
    except Exception as e:
        # 包含预算闸门抛的 BudgetExceeded。修标题是锦上添花，绝不能因此让整篇失败。
        log.warning("[%s] 定点改标题调用失败（保留原标题）: %s: %s",
                    merchant_id, type(e).__name__, e)
        return False, ""

    # ── Step 3: 校验模型的回答（失败就整批放弃）─────────────────────
    # 这里的每一条都对应一种真实会发生的返回值畸形，缺一条就可能把文章改坏。
    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError:
        log.warning("[%s] 定点改标题返回的不是合法 JSON（保留原标题）", merchant_id)
        return False, ""
    if not isinstance(parsed, dict):
        # json_mode 只保证合法 JSON，**不保证是对象** —— 可能返回一个数组
        log.warning("[%s] 定点改标题返回的 JSON 顶层不是对象（保留原标题）", merchant_id)
        return False, ""

    replacements: dict[int, str] = {}
    for n, (idx, old) in enumerate(targets, 1):
        new = parsed.get(str(n)) or parsed.get(n)      # 键可能是 "1" 也可能是 1
        if not isinstance(new, str):
            continue
        new = re.sub(r"\s+", " ", new).strip().strip(":：").strip()
        if not new:
            continue
        if _normalize(new) == _normalize(old):
            continue                                   # 换了等于没换，不算改动
        if len(new) > 120:
            continue                                   # 异常长 = 模型开始写句子了，丢掉
        # 模型可能自己又加上 "1. " —— 先剥掉它自己加的，再贴回原来那个（如果有）。
        # 顺序很重要：先剥后贴，否则会出现 "2. 1. Focus on…" 这种双重编号。
        new = _NUMBER_PREFIX_RE.sub("", new).strip()
        if idx in prefixes:
            new = prefixes[idx] + new                  # ★ 把 listicle 的结构编号原样贴回
        replacements[idx] = new

    # 现在发的是全集，其中有些标题模型会原样返回（第 7 条明确允许）——
    # 那些不算"改动"，所以门槛只要求**至少改到 2 个**，不再按比例算。
    # 改 1 个改不掉"重复"这件事本身（重复至少涉及两个标题）。
    if len(replacements) < 2:
        log.warning("[%s] 定点改标题只拿到 %d 个可用改动（保留原标题）",
                    merchant_id, len(replacements))
        return False, ""

    # ── Step 4: 就地替换，正文其它部分逐字不动 ────────────────────────
    blog_data["content_html"] = _apply_headings(content_html, replacements)
    note = (f"Targeted fix: rewrote {len(replacements)} duplicated subheads "
            f"(headings only, body untouched; cost ${result.cost:.4f})")
    log.info("[%s] %s", merchant_id, note)
    for idx, new in replacements.items():
        log.info("[%s]   h2[%d]: %r → %r", merchant_id, idx, by_index[idx][:60], new[:60])
    return True, note
