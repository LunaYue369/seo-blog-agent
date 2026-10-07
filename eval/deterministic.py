"""确定性 SEO 校验器 —— 三层评估的第①层：零 LLM、零成本、可断言、可在 CI 里跑

## 这个模块存在的理由（一句话）
copywriter 的 prompt 里写着「SEO 十条」（agents/copywriter.py:243-253），但那十条
**过去只有 LLM 自己检查自己**：写稿的是 gpt-4.1，审稿的是 gpt-4.1-mini —— 同族自评，
盲点一样。而其中至少八条是**能用代码算出真假的客观事实**（摘要多少字、有几个 h2、
副关键词出现了没有、有没有 emoji）。让 LLM 去数字符数，既贵又不准；这本来就是正则的活。

    判据：**能用代码算出真假的，绝不问 LLM。** 只有"写得好不好读"这类主观问题才交给裁判。

## 在 pipeline 里的位置（接线点只有 review_loop 一处）
    copywriter 出稿
      → ★check_seo()★  零成本      ← 本模块
          ├─ hard_ok=False → 直接把 failures 当 issues 打回 rewrite，**不调 reviewer（省钱）**
          └─ hard_ok=True  → 才调 reviewer（花钱）→ 分数 ≥80 才算过
    通过条件从「passed AND score>=80」升级成「hard_ok AND passed AND score>=80」

## 为什么是「先确定性、后 LLM」而不是反过来
① 省钱：硬性不合格的稿子根本不值得花 reviewer 的钱。
② 反馈质量更高：确定性检查给出的是「摘要 138 字，要求 150-160」这种**带数字的定点指令**，
   LLM 反馈往往是「摘要可以更吸引人」这种没法定点修的话。定点修才不会破坏已通过的部分。
③ 可断言：有了确定性指标，golden set + CI 才有东西可断言（维度④的前置依赖）。

## 设计约束：本模块必须是纯函数
不读 merchant.json、不碰网络、不碰文件系统、不看时间。所有外部事实（商家官网域名、
图片槽位集合）都由调用方作为参数传进来。这样 `tests/test_deterministic.py` 在 CI 里
**不需要 API key、不需要联网**就能跑 —— 这是「prompt 回归测试」唯一能真正落地的做法。

## 输入 / 输出
    check_seo(blog_data, topic, *, website=..., expected_slots=...) -> DeterministicReport

    blog_data       copywriter 的输出 dict（title / content_html / excerpt / image_alts …）
    topic           选题 dict，取 primary_keyword / secondary_keywords 做对标
    website         商家官网 URL，用来区分内链和外链（不传则跳过链接检查并说明原因）
    expected_slots  本篇**应该**有哪几个图片槽位，如 ("hero","mid","end") 或 ("img_1","img_2")

    返回 DeterministicReport：
        hard_ok        全部通过才 True —— 这是给 pipeline 做闸门用的唯一布尔值
        passed_checks  通过项的人话描述（渲进 Slack = 一列绿勾）
        failures       未通过项的人话描述，**每条都带具体数字**，可直接当 rewrite 的 issues
        metrics        算出来的原始数值（excerpt_chars=138 / h2_count=9 …），给 golden test 断言
        checks         逐项明细（CheckResult 列表），需要细看时用
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import config as cfg

# ── 常量：正则和白名单 ────────────────────────────────────────────────

# 图片占位符的**严格**格式：<!-- BLOG_IMAGE:hero -->
# 空格数量、大小写、冒号位置都必须完全一致 —— 因为下游 preview_server /
# _execute_image_plan 是按这个字面量做 str.replace 的，差一个空格就替换不上、
# 最后渲染出来是一条 HTML 注释（用户看到的是"图片没了"，而且零日志）。
_PLACEHOLDER_STRICT_RE = re.compile(r"<!-- BLOG_IMAGE:([A-Za-z0-9_]+) -->")

# **宽松**格式：容忍多余空白、大小写混乱。用来抓「差一点点」的畸形占位符。
# 中间产物用途：strict 和 loose 的**数量差**就是畸形占位符的数量。
# 只有 loose 也匹配不上的（比如 GPT 写成 {{image_hero}}）才会被当成"槽位缺失"。
_PLACEHOLDER_LOOSE_RE = re.compile(r"<!--\s*blog_image\s*:\s*([A-Za-z0-9_\-]+)\s*-->", re.I)

# emoji / Unicode 图标区块。踩过的坑见 [feedback_tts-special-chars]：
# 这些字符在 WordPress 渲染和多语言 TTS 里都出问题，SEO 十条第 10 条明令禁止。
# 注意**故意不包含**排版符号：— (U+2014) ' (U+2019) … (U+2026) ° × 等都是合法正文用字。
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # emoji 主区（表情/手势/物品/旗帜/符号）
    "\U00002600-\U000027bf"  # 杂项符号 + Dingbats：☀ ✅ ✂ ✈
    "\U00002b00-\U00002bff"  # 箭头补充区
    "\U00002190-\U000021ff"  # 箭头区：← → ⇒（技术文里常见，但同样属"Unicode 图标"）
    "\U0000fe0f"             # 变体选择符-16（把前一个字符变成 emoji 呈现）
    "\U0000200d"             # ZWJ（emoji 组合用零宽连接符）
    "]"
)

# A 类硬禁词：**没有任何合法用法**的绝对化承诺 → 确定性阻断，不问 LLM。
# 分类依据见 [幻觉三分类] 的四档处置：
#   A 类硬禁（本表）= 直接拦   B 类查出处（数字）  C 类判语境（因果词）  D 类只记录
# ⚠️ 刻意**不拦** "lifetime warranty"：那是 XPEL / Ceramic Pro 这类厂商的真实条款名，
#    贴膜行业里天天出现。拦它会制造大量假阳性 —— "宁可漏拦有合法用法的，
#    也不能误拦真实业务用语"，这条取舍本身就是设计观点。
_FORBIDDEN_CLAIMS: tuple[tuple[str, str], ...] = (
    (r"100\s*%\s*(?:guarantee\w*|satisfaction|effective|protection|waterproof|scratch[-\s]?proof)",
     "100% absolute guarantee"),
    (r"\bguarantee[ds]?\s+(?:to\s+)?(?:last|never|results?)", "promised result or lifespan"),
    (r"\bnever\s+(?:fades?|scratches?|peels?|cracks?|wears?\s+out)", '"never X" claim'),
    (r"\blasts?\s+forever\b", '"lasts forever" claim'),
    (r"\bpermanent(?:ly)?\s+(?:protects?|prevents?|eliminates?)", "permanent-effect claim"),
    (r"\brisk[-\s]free\b", '"risk-free" claim'),
)
_FORBIDDEN_CLAIMS_COMPILED = tuple(
    (re.compile(pat, re.I), label) for pat, label in _FORBIDDEN_CLAIMS
)

# 不参与"内链/外链"统计的 href 前缀：页内锚点和联系方式都不是"链接到一个页面"。
# 把它们算进内链数会让「内链 3-5 条」这条永远轻松达标，检查就失去意义了。
_NON_PAGE_HREF_PREFIXES = ("#", "mailto:", "tel:", "sms:", "javascript:")


# ── 数据结构：单项结果 + 整份报告 ─────────────────────────────────────


# ── 严重度分级：哪些检查该阻断，哪些只报告 ────────────────────────────
#
# ⭐ 判据（加新检查项时按这一条对号入座，不要凭感觉）：
#
#     hard = 产出的东西**是坏的或有风险的**
#     soft = 产出的东西**只是不够好**
#
# 这和 fail-fast 的判据是同一条：**当降级产物是"看不出错的错"（silent corruption）
# 时必须阻断**。碎图、裸露的 HTML 注释、混乱的标题层级、绝对化承诺 —— 这些要么
# 用户直接看到坏东西，要么是合规风险，都不能放行。
# 而"摘要 161 字符"、"内链 6 条"、"h2 只有 3 个"是**质量不够**，不是坏 ——
# 它们值得提醒，但不值得为它们触发一次整篇重写（实测一次 rewrite ≈ $0.028）。
#
# 为什么这张表集中放在这里、而不是写在各个 check 函数里：
# **"哪些问题严重到要拦停出稿"是产品决策，不是实现细节。** 它必须一眼看得全、
# 能被 review、能被质疑。散落在 11 个函数里的话，没人能回答"我们到底拦什么"。
#
# 2026-07-27 实测促成这次分级：首篇实测稿最后卡在「摘要 161 字符（超 1 个）」+
# 「内链 6 条（超 1 条）」上，为这两条重写了两遍整篇，烧掉 $0.055。
# 两条都属于"不够好"，不属于"坏"。
_SEVERITY: dict[str, str] = {
    # ── hard：会产出坏东西或合规风险，必须拦 ──
    "no_h1": "hard",               # 一页两个 h1 → 标题层级混乱，肉眼看不出来
    "no_img_tags": "hard",         # 编造的 /images/xxx.jpg → 用户看到碎图
    "image_placeholders": "hard",  # 集合对不上 → 图片不出现 / 重复 / 注释裸露在正文
    "no_emoji": "hard",            # 破坏 WordPress 渲染和多语言 TTS
    "forbidden_claims": "hard",    # 绝对化承诺 = 品牌安全 / 虚假宣传风险
    "catalog_grounding": "hard",   # 编造价格 = 客服问题/信任损失，是整套检查里最该 hard 的一条
    # ⭐ 品牌禁词分成 hard / soft 两级，是因为**同一个词在不同位置的性质不同**：
    "brand_voice_headline": "hard",  # 标题/摘要/小标题 = 品牌门面，零容忍
    # ── soft：只是不够好，报告 + 交给 reviewer 顺手提，不阻断 ──
    "brand_voice_body": "soft",      # 正文里同一个词可能有合法用法（见下面函数注释）
    "excerpt_length": "soft",      # 超长已由 eval/repair.py 自动裁掉；剩下"过短"= 浪费展示位，不是坏
    "h2_count": "soft",            # 4-7 是启发式，不是事实（3 个 h2 的文章不算坏）
    "h2_keyword_stuffing": "soft",  # 读起来蠢、SEO 上不利，但文章能发 —— "不够好"不是"坏"
    "internal_links": "soft",      # 同上，3-5 条是从 prompt 抄来的数字
    "external_links": "soft",      # 同上
    "primary_keyword_early": "soft",   # 前 100 词是强启发式，但漏了不等于文章坏
    "secondary_keywords": "soft",      # 真实 SEO 损失，但可发布；交给 reviewer 一起修
}


@dataclass
class CheckResult:
    """一项检查的结果。

    name      机器可读的检查项 ID（"excerpt_length"），给 CI 断言和日志过滤用
    ok        过没过
    detail    人话描述，**必须带具体数字**。它会出现在三个地方：
                - ok=True  → Slack 卡片的绿勾列表
                - ok=False → 卡片红叉/黄叹号 + 打回 rewrite 时的 issues 条目
              所以它必须是**可执行的指令**，不是评价。
    severity  "hard"（阻断出稿）| "soft"（只报告）。由 _SEVERITY 表决定，见上面的判据。
    metric    这项算出来的原始值，会被汇总进 report.metrics
    """

    name: str
    ok: bool
    detail: str
    metric: Any = None
    severity: str = "hard"    # 兜底给 hard：新加的检查项忘了进 _SEVERITY 表时，
                              # 宁可误拦（有人会立刻发现）也不要误放（静默降级没人发现）


@dataclass
class DeterministicReport:
    """一次 check_seo 的完整产物。

    ⭐ 字段之间的关系（别搞混，闸门只看第一个）：
        hard_ok         **唯一**给 pipeline 当闸门用的布尔值 = 没有任何 hard 项失败。
                        soft 项全炸也不影响它。
        hard_failures   会阻断出稿的失败项（"坏的/有风险的"）
        soft_failures   只报告的失败项（"不够好"）
        failures        = hard + soft，**给人看和喂给 LLM 用的全集**。
                        为什么全集：一旦决定要花钱重写，顺手把软问题一起修掉不额外收费。
        passed_checks   通过项
    """

    hard_ok: bool
    passed_checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)      # hard + soft
    hard_failures: list[str] = field(default_factory=list)
    soft_failures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    checks: list[CheckResult] = field(default_factory=list)

    # repairs 由 **review_loop** 在循环结束后填入，`check_seo` 自己永远不产生修复
    # （校验器只读，修复是另一个模块的事 —— 见 eval/repair.py）。
    # 放在这个 dataclass 里的理由是消费方视角：pipeline 和 Slack 卡片要的是
    # 「eval 层对这份稿子做了什么」的完整答案，而"代码自动改了摘要"属于那个答案。
    # 契约由消费方定义 —— 硬把它拆成第四个返回值只是为了模块洁癖，对调用方更差。
    repairs: list[str] = field(default_factory=list)

    # ── 便利方法 ──────────────────────────────────────────────
    def as_feedback(self) -> dict:
        """把 failures 包装成和 reviewer 输出**同构**的 feedback dict。

        为什么要同构：`rewrite_blog(merchant_id, blog_data, feedback, ...)` 读的是
        feedback["issues"] / ["suggestions"]。做成同一个形状，硬性打回就能直接复用
        现成的 rewrite 路径，**不用为它另写一条分支**（少一条分支 = 少一处会腐烂的代码）。

        issues 里放的是 **failures 全集（hard + soft）**：既然已经决定要花这笔重写的钱，
        顺手把软问题一起修掉是免费的 —— 分开修才是浪费。

        score 固定给 0：这时候 reviewer 根本没跑过，没有真实分数。给 0 而不是编一个
        中间值，是为了让「没评过分」和「评了个低分」在数据上可区分。
        """
        return {
            "score": 0,
            "passed": False,
            "issues": list(self.failures),
            "suggestions": [
                "These are objective metrics computed by code, not subjective opinions. "
                "Fix each one against the numbers given; do not rewrite the whole article "
                "(leave everything that already passed exactly as it is)."
            ],
            "source": "deterministic",  # 溯源字段：让日志能区分这轮是谁打回的
        }

    def summary_line(self) -> str:
        """一行摘要，给日志和 Slack 用。hard 和 soft 分开说，否则看不出要不要拦。"""
        total = len(self.checks)
        passed = total - len(self.failures)
        tail = ""
        if self.hard_failures:
            tail += f"（{len(self.hard_failures)} 项 HARD 阻断"
            tail += f" / {len(self.soft_failures)} 项 soft）" if self.soft_failures else "）"
        elif self.soft_failures:
            tail = f"（{len(self.soft_failures)} 项 soft，仅提醒不阻断）"
        return f"SEO 检查 {passed}/{total} 通过{tail}"


# ── 内部小工具 ────────────────────────────────────────────────────────


def _keyword_regex(keyword: str) -> re.Pattern | None:
    """把关键词编译成"宽松但不过宽"的匹配正则。

    为什么不能用 `keyword.lower() in text.lower()`：
      - "ceramic coating" 和 "ceramic  coating"（两个空格）会漏；
      - "window tint" 和 "window tints"（复数）会漏 —— 而 SEO 上这两个算同一个词；
      - 反过来，裸 `in` 又会把 "car" 匹配进 "carpet"，太宽。
    所以规则是：**词间允许任意空白/连字符/斜杠，末词允许 s / es 复数，两端要求非字母数字边界。**

    返回 None 表示这个关键词是空的（调用方应跳过，而不是当成"没找到"）。
    """
    words = [re.escape(w) for w in re.split(r"[\s\-_/]+", keyword.strip()) if w]
    if not words:
        return None
    body = r"[\s\-_/]+".join(words)
    # (?<![A-Za-z0-9]) / (?![A-Za-z0-9]) 是自己写的词边界：比 \b 更适合含空格的短语
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?:e?s)?(?![A-Za-z0-9])", re.I)


def _host_of(url: str) -> str:
    """从 URL 取出用于比对的主机名（小写、去掉 www. 前缀）。

    去 www. 是因为商家配置里写的可能是 https://harborautospa.example/，
    而文章里的链接可能是 https://www.harborautospa.example/services/ —— 这两个是同一个站，
    不去前缀就会被误判成外链。
    """
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _classify_links(soup: BeautifulSoup, site_host: str) -> tuple[list[str], list[str], list[str]]:
    """把正文里所有 <a href> 分成三堆。

    返回 (internal, external, skipped)：
      internal  相对路径，或主机名等于/属于商家官网 → 内链
      external  http(s) 且主机名不是商家的           → 外链
      skipped   #锚点 / mailto: / tel: 等非页面链接  → 不计入任何一边

    中间产物：这三个列表的**长度**就是后面两项检查要比的数字。
    """
    internal: list[str] = []
    external: list[str] = []
    skipped: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(_NON_PAGE_HREF_PREFIXES):
            skipped.append(href)
            continue

        host = _host_of(href)
        if not host:
            # 没有主机名 = 相对路径（/services/、about.html）→ 必然指向本站
            internal.append(href)
        elif site_host and (host == site_host or host.endswith("." + site_host)):
            # host.endswith("."+site_host) 让 blog.harborautospa.example 也算内链（子域同属本站）
            internal.append(href)
        else:
            external.append(href)

    return internal, external, skipped


def _first_n_words(text: str, n: int) -> str:
    """取正文前 n 个词（用来判断"主关键词在不在前 100 词"）。

    用空白切词而不是分句：SEO 的说法就是 "first 100 words"，而且这样对中英文都不敏感。
    """
    return " ".join(text.split()[:n])


# ── 各项检查（每个函数 = 一项，返回一个 CheckResult）─────────────────
# 拆成小函数而不是写成一个大 if 链，图的是两件事：
#   ① 每一项能被单元测试单独打（tests 里可以只喂一个畸形输入验一项）
#   ② 加新检查项时不用碰已有代码，只在 check_seo 的清单里加一行


def _check_excerpt_length(excerpt: str) -> CheckResult:
    """SEO 第 6 条：摘要（meta description）必须 150-160 字符。

    为什么是硬性：Google 搜索结果对 description 的截断位置是死的，短了浪费展示位、
    长了被截断。这是纯客观的字符数问题，没有"见解"可言。
    """
    lo, hi = cfg.EVAL_EXCERPT_MIN_CHARS, cfg.EVAL_EXCERPT_MAX_CHARS
    n = len((excerpt or "").strip())          # 中间产物：字符数（注意 strip，尾随空格不算）
    ok = lo <= n <= hi
    if ok:
        detail = f"Excerpt length {n} chars (target {lo}-{hi})"
    else:
        direction = "too short, extend it" if n < lo else "too long, trim it"
        detail = (f"Excerpt length {n} chars — {direction} to {lo}-{hi} chars "
                  f"(off by {abs(n - (lo if n < lo else hi))} chars)")
    return CheckResult("excerpt_length", ok, detail, metric=n)


def _check_no_h1(soup: BeautifulSoup) -> CheckResult:
    """SEO 第 1 条：正文里不能有 <h1>。

    为什么是硬性：h1 由 HTML 模板输出（模板里已经有一个 = 文章标题）。正文再写一个
    就是一页两个 h1，Google 判定标题层级混乱。这个用代码一查就知道，没有灰色地带。
    """
    tags = soup.find_all("h1")               # 中间产物：所有 h1 标签（正常应为空列表）
    ok = not tags
    if ok:
        detail = "No <h1> in body (h1 comes from the template, hierarchy is correct)"
    else:
        texts = ", ".join(f'"{t.get_text(strip=True)[:40]}"' for t in tags[:3])
        detail = (f"Body contains {len(tags)} <h1> tag(s) ({texts}) — demote all of them to <h2>; "
                  f"only the template may emit h1")
    return CheckResult("no_h1", ok, detail, metric=len(tags))


def _check_h2_count(soup: BeautifulSoup) -> CheckResult:
    """SEO 第 3 条：h2 数量 4-7 个。

    ⚠️ 计数**包含** FAQ 那个 `<h2>Frequently Asked Questions</h2>`。
    这是刻意的：读者看到的目录里它就是一个章节，凑数不该只算"内容章节"。
    所以实际内容章节是 3-6 个 + FAQ 一个 —— 阈值在 config 里，觉得紧就改那里，不改代码。
    """
    lo, hi = cfg.EVAL_H2_MIN, cfg.EVAL_H2_MAX
    headings = soup.find_all("h2")           # 中间产物：h2 标签列表
    n = len(headings)
    ok = lo <= n <= hi
    if ok:
        detail = f"{n} h2 sections (target {lo}-{hi}, FAQ section included)"
    else:
        direction = "too few, split out more sections" if n < lo else "too many, merge sections"
        detail = f"{n} h2 sections — {direction} to reach {lo}-{hi} (FAQ section included)"
    return CheckResult("h2_count", ok, detail, metric=n)


def _check_primary_keyword_early(body_text: str, primary_kw: str) -> CheckResult:
    """SEO 第 2 条：主关键词必须出现在正文前 N 个词里（默认 100）。

    为什么是硬性：这是 Google 判断"这篇讲的是什么"的最强位置信号之一。
    "出现没出现在前 100 词"是可数的，不是审美问题。
    """
    window = cfg.EVAL_PRIMARY_KW_WINDOW_WORDS
    if not primary_kw:
        # 没给主关键词 → 这一项没法判。**明确报"跳过"并说明原因**，不要默认判过。
        # 判据来自 [缺失指标绝不能当 0] 那条铁律：没数据 ≠ 达标。
        return CheckResult("primary_keyword_early", True,
                           f"Skipped (topic has no primary_keyword, cannot check the first {window} words)",
                           metric=None)

    opening = _first_n_words(body_text, window)   # 中间产物：正文前 window 个词拼成的字符串
    pattern = _keyword_regex(primary_kw)
    hit = bool(pattern and pattern.search(opening))
    if hit:
        detail = f'Primary keyword "{primary_kw}" appears within the first {window} words'
    else:
        # 顺手告诉它全文有没有 —— "全文有但位置太后"和"全文根本没提"是两种不同的病，修法不同
        anywhere = bool(pattern and pattern.search(body_text))
        where = ("it does appear but too far down — move it into the opening paragraph"
                 if anywhere else
                 "it never appears — the opening must mention it naturally")
        detail = f'Primary keyword "{primary_kw}" not in the first {window} words — {where}'
    return CheckResult("primary_keyword_early", hit, detail, metric=hit)


def _check_secondary_keywords(body_text: str, secondary_kws: Sequence[str]) -> CheckResult:
    """SEO 第 5 条：每个副关键词至少在正文出现 1 次。

    中间产物 missing：**没出现的那几个词本身**。这个列表直接进 failures 文案 ——
    打回时说的是"这 2 个词没出现：X、Y"，而不是"副关键词覆盖不足"。
    定点指令才能定点修，不会连带把已通过的部分重写坏。
    """
    kws = [k for k in (secondary_kws or []) if str(k).strip()]
    if not kws:
        return CheckResult("secondary_keywords", True,
                           "Skipped (topic has no secondary_keywords)", metric=None)

    missing: list[str] = []
    for kw in kws:
        pattern = _keyword_regex(str(kw))
        if not pattern or not pattern.search(body_text):
            missing.append(str(kw))

    ok = not missing
    if ok:
        detail = f"All {len(kws)} secondary keywords appear in the body"
    else:
        detail = (f"{len(missing)}/{len(kws)} secondary keywords missing from the body: "
                  f"{', '.join(chr(34) + m + chr(34) for m in missing)} "
                  f"— each must appear naturally at least once")
    return CheckResult("secondary_keywords", ok, detail, metric=missing)


def _check_internal_links(internal: list[str], site_host: str) -> CheckResult:
    """SEO 第 7 条：内链 3-5 条（指向商家官网）。"""
    lo, hi = cfg.EVAL_INTERNAL_LINKS_MIN, cfg.EVAL_INTERNAL_LINKS_MAX
    if not site_host:
        # 没有官网域名就没法区分内外链 —— 同样明确跳过并说原因，不假装通过
        return CheckResult("internal_links", True,
                           "Skipped (no merchant website host, cannot tell internal from external links)",
                           metric=None)
    n = len(internal)
    ok = lo <= n <= hi
    if ok:
        detail = f"{n} internal links (target {lo}-{hi}, pointing to {site_host})"
    else:
        direction = (f"too few, add up to {lo}-{hi}" if n < lo
                     else f"too many, cut down to {lo}-{hi}")
        detail = (f"{n} internal links (pointing to {site_host}) — {direction}; "
                  f"anchor text should carry the keyword")
    return CheckResult("internal_links", ok, detail, metric=n)


def _check_external_links(external: list[str], site_host: str) -> CheckResult:
    """SEO 第 8 条：外链 1-2 条（指向权威站：行业协会/厂商/政府）。"""
    lo, hi = cfg.EVAL_EXTERNAL_LINKS_MIN, cfg.EVAL_EXTERNAL_LINKS_MAX
    if not site_host:
        return CheckResult("external_links", True,
                           "Skipped (no merchant website host, cannot tell internal from external links)",
                           metric=None)
    n = len(external)
    ok = lo <= n <= hi
    if ok:
        hosts = ", ".join(sorted({_host_of(u) for u in external})[:3])
        detail = f"{n} external links (target {lo}-{hi}): {hosts}"
    else:
        direction = (f"too few, add up to {lo}-{hi} authoritative links" if n < lo
                     else f"too many, cut down to {lo}-{hi}")
        detail = (f"{n} external links — {direction} "
                  f"(industry associations / manufacturer sites / government sites)")
    return CheckResult("external_links", ok, detail, metric=n)


def _check_no_emoji(fields: dict[str, str]) -> CheckResult:
    """SEO 第 10 条：任何字段都不许有 emoji / Unicode 图标。

    fields 传的是「字段名 → 文本」，因为要检查的不止正文：title、excerpt、
    image_alts 每一条都会被渲染出去，任何一处有 emoji 都会出问题。
    中间产物 hits：`[(字段名, 那个字符), ...]` —— 报错时能直接指出"在哪个字段的哪个字符"。
    """
    hits: list[tuple[str, str]] = []
    for name, text in fields.items():
        for ch in _EMOJI_RE.findall(text or ""):
            hits.append((name, ch))

    ok = not hits
    if ok:
        detail = "No emoji / Unicode icons (checked " + str(len(fields)) + " fields)"
    else:
        shown = ", ".join(f"{ch!r} in {name}" for name, ch in hits[:5])
        more = f", plus {len(hits) - 5} more" if len(hits) > 5 else ""
        detail = (f"Found {len(hits)} emoji / Unicode icons: {shown}{more} "
                  f"— remove all of them (they break rendering and TTS)")
    return CheckResult("no_emoji", ok, detail, metric=[h[1] for h in hits])


def _check_no_img_tags(soup: BeautifulSoup) -> CheckResult:
    """图片必须走占位符，正文里不许有 <img>。

    为什么是硬性而不是提醒：LLM 编出来的 `<img src="/images/hero.jpg">` 指向一个
    不存在的文件，用户看到的是**碎图**。而且它不是占位符，图片系统不会去替换它 ——
    典型的"看不出错的错"（silent corruption），必须 fail-fast。
    """
    tags = soup.find_all("img")
    ok = not tags
    if ok:
        detail = "No <img> tags in body (all images go through placeholders)"
    else:
        srcs = ", ".join(f'"{t.get("src", "")[:40]}"' for t in tags[:3])
        detail = (f"Body contains {len(tags)} <img> tag(s) ({srcs}) — these paths are invented and "
                  f"render as broken images; remove them and use <!-- BLOG_IMAGE:slot --> placeholders")
    return CheckResult("no_img_tags", ok, detail, metric=len(tags))


def _check_placeholders(content_html: str, expected_slots: Sequence[str] | None) -> CheckResult:
    """占位符集合必须和槽位集合**精确**相等，且每个恰好出现一次。

    这是一条 **pipeline 层不变式（invariant）**，不是普通的字段校验 ——
    schema 只能验"这个对象自己长得对不对"，验不了"它和别的东西对不对得上"。
    这里要对的是两个不同来源：
        content_html 里的占位符集合  ==  image_plan 推出来的槽位集合
    对不上的后果分两种，都很脏：
        少了 → 生成的图片没地方放，图白花钱生了（[image_alts 那个 bug] 的同族）
        多了 → 占位符原样留在正文里，用户看到一条 HTML 注释

    三个中间产物，各查一件事：
        strict  严格格式命中的槽位列表  → 数量统计和集合比对都基于它
        loose   宽松格式命中的槽位列表  → 和 strict 的差 = 畸形占位符（差个空格那种）
        counts  每个槽位出现了几次      → 抓重复（重复会导致同一张图出现两次）
    """
    if not expected_slots:
        return CheckResult("image_placeholders", True,
                           "Skipped (caller passed no expected_slots, cannot compare the slot set)",
                           metric=None)

    strict = _PLACEHOLDER_STRICT_RE.findall(content_html or "")
    loose = _PLACEHOLDER_LOOSE_RE.findall(content_html or "")
    counts: dict[str, int] = {}
    for slot in strict:
        counts[slot] = counts.get(slot, 0) + 1

    expected = list(dict.fromkeys(expected_slots))   # 去重但保持顺序（顺序影响报错可读性）
    problems: list[str] = []

    # ① 格式畸形：宽松能匹配但严格匹配不上 → 数量差就是畸形数
    if len(loose) > len(strict):
        problems.append(
            f"{len(loose) - len(strict)} placeholder(s) malformed (whitespace or casing is off) "
            f"— must be written exactly as <!-- BLOG_IMAGE:slot -->"
        )

    # ② 缺槽位
    missing = [s for s in expected if s not in counts]
    if missing:
        problems.append("missing placeholders: "
                        + ", ".join(f"<!-- BLOG_IMAGE:{s} -->" for s in missing))

    # ③ 多出不该有的槽位（GPT 自己发明了一个槽位名）
    unexpected = [s for s in counts if s not in expected]
    if unexpected:
        problems.append("unknown slots: " + ", ".join(unexpected)
                        + f" (this article only has these slots: {', '.join(expected)})")

    # ④ 重复：同一个槽位写了两次 → 同一张图会出现两次
    duplicated = [f"{s}×{c}" for s, c in counts.items() if c > 1]
    if duplicated:
        problems.append("duplicated placeholders: " + ", ".join(duplicated)
                        + " (each slot may appear only once)")

    ok = not problems
    if ok:
        detail = (f"{len(expected)} image placeholders present and exactly formatted "
                  f"({', '.join(expected)})")
    else:
        detail = "Image placeholder problems — " + "; ".join(problems)
    return CheckResult("image_placeholders", ok, detail, metric=counts)


# h2 短语重复检测用的停用词：只去掉纯功能词。
# ⚠️ 刻意保持很短 —— 停用词越多，任意两个标题看起来就越像，检查会开始误报。
# "best" / "guide" 这类词故意**不**去掉：它们重复出现本身就是堆砌的症状。
_HEADING_STOPWORDS = frozenset(
    "a an the of for to in on at and or but with your you my it that this is are"
    " do does how what why when which".split()
)


def _singularize(word: str) -> str:
    """把英文简单复数收成单数 —— 只做这一步，**不引入词干库**。

    ## 为什么必须做（2026-07-27 实测证伪了原来的决定）
    第一版刻意不做任何归一化，理由写的是"堆砌信号是名词短语原样重复，精确匹配就够"。
    实测被打脸：真实产出里
        'fitting t shirt'  出现在 2 个标题
        'fitting t shirts' 出现在 2 个标题
    合起来 4 个标题都在说同一件事，**但因为 shirt / shirts 被当成两个词，
    两边各 2 个、都正好卡在"最多允许 2 个"的阈值上，于是一条都没报。**
    单复数把计数劈成两半，是这个检查最容易被绕过的方式。

    ## 为什么只做复数、不做完整 stemming
    完整词干还原（Porter/Snowball）会把 "fitting"→"fit"、"matching"→"match"，
    那会**过度合并**：'best fitting t' 和 'best fit t' 本来是不同的表达。
    而我们要抓的恰恰是"同一个名词短语反复出现"，名词的变形主要就是复数。
    所以只处理复数：收益全拿到，假阳性风险最小，还不用加依赖。

    ## 三条防误伤的排除（都是真实英文词的形态）
        ss 结尾  business / dress   → 不是复数
        us 结尾  status  / canvas   → 不是复数
        is 结尾  analysis / basis   → 不是复数（而且它的复数是 -es → analyses）
        as 结尾  canvas / atlas     → 同上
    再加长度 >= 4 的门槛，避免把 "is" / "as" / "its" 这类短词切坏。
    """
    if len(word) >= 4 and word.endswith("s") and not word.endswith(("ss", "us", "is", "as")):
        return word[:-1]
    return word


def _normalize_heading(text: str) -> list[str]:
    """把一个小标题切成可比较的词序列：小写、去标点、去停用词、收单数。

    中间产物：词列表。收单数这一步的由来见 `_singularize` ——
    不做的话单复数会把重复计数劈成两半，检查形同虚设。
    （顺便：这也让本模块和 `_keyword_regex` 的口径一致了 ——
      那边匹配副关键词时本来就容忍 `(?:e?s)?` 复数，标题检查原本是唯一的例外。）
    """
    words = re.split(r"[^A-Za-z0-9]+", text.lower())
    return [_singularize(w) for w in words if w and w not in _HEADING_STOPWORDS]


def _check_h2_keyword_stuffing(soup: BeautifulSoup) -> CheckResult:
    """同一个短语在太多 h2 里重复出现 = 关键词堆砌。

    ## 为什么加这条（2026-07-27 实测）
    第二篇实测稿的四个 h2 是同一件事的四种说法：
        How to Know Your T Shirt Size Male: The Problem with Guessing
        How to Get Your T Shirt Size: The Measurement Method That Works
        How to Figure Out T Shirt Size: Understanding Fit, Fabric, and Style
        How to Find Your T Shirt Size for Any Brand: Size Charts, Conversions...
    读起来很蠢（"Size Male" 还不通顺），而且是典型的 keyword stuffing。
    **确定性层没抓到（它只数数量），reviewer 也没扣分（给了 88 分放行）。**

    ## 为什么检"短语重复"而不是"标题相似度"
    第一版想做的是两两 Jaccard 相似度，但实测算下来只有 0.27 —— 因为这四个标题的
    **尾巴各不相同**（Problem with Guessing / Measurement Method / Fit Fabric Style…），
    整体相似度被稀释了。真正的信号是**共同的关键词内核**「t shirt size」在四个里都出现。
    所以改成检 n-gram 重复：把"看起来像不像"换成"同一个短语出现了几次"——
    后者可数、可解释、也正是 SEO 上真正要避免的东西。

    ## 为什么允许重复 2 次而不是 0 次
    SEO 十条第 3 条本来就要求「每个 h2 都要含一个副关键词」，所以少量重复是**期望行为**。
    阈值定在"最多 2 个 h2 可以共享同一短语"，超了才算堆砌 —— 见 config.EVAL_H2_PHRASE_MAX_REPEAT。
    """
    allowed = cfg.EVAL_H2_PHRASE_MAX_REPEAT
    n = cfg.EVAL_H2_PHRASE_MIN_WORDS

    headings = [h.get_text(" ", strip=True) for h in soup.find_all("h2")]
    if len(headings) < allowed + 1:
        # 标题数还不够触发条件（比如只有 2 个 h2）→ 明确跳过，不假装检查过
        return CheckResult("h2_keyword_stuffing", True,
                           f"Skipped (only {len(headings)} h2, not enough to judge phrase repetition)",
                           metric=None)

    # 中间产物 phrase_to_headings：短语 → 出现过它的标题序号集合
    # 用集合而不是计数，是为了让"同一个标题里重复三遍"不被误算成三个标题都有
    phrase_to_headings: dict[str, set[int]] = {}
    for idx, text in enumerate(headings):
        words = _normalize_heading(text)
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i:i + n])
            phrase_to_headings.setdefault(phrase, set()).add(idx)

    # 中间产物 offenders：超过允许次数的短语，按出现的标题数从多到少
    offenders = sorted(
        ((p, hs) for p, hs in phrase_to_headings.items() if len(hs) > allowed),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )

    # 去掉被更长/更频繁短语包含的子短语，避免同一个问题报三遍
    # （"t shirt size" 和 "shirt size male" 这种重叠只保留最有代表性的那个）
    deduped: list[tuple[str, set[int]]] = []
    for phrase, hs in offenders:
        if any(phrase in kept and hs <= kept_hs for kept, kept_hs in deduped):
            continue
        deduped.append((phrase, hs))

    ok = not deduped
    if ok:
        detail = (f"No keyword stuffing in h2 ({len(headings)} headings, no {n}-word phrase "
                  f"repeated more than {allowed} times)")
    else:
        shown = "; ".join(
            f'"{p}" appears in {len(hs)} h2' for p, hs in deduped[:3]
        )
        detail = (f"Keyword stuffing in h2: {shown} (at most {allowed} h2 may share the same phrase)"
                  f" — rewrite the duplicates around the distinct question each one answers, "
                  f"don't just swap synonyms")
    return CheckResult("h2_keyword_stuffing", ok, detail,
                       metric={p: sorted(hs) for p, hs in deduped})


def _check_brand_voice(
    surface: str,
    text_by_field: dict[str, str],
    terms: Sequence[str],
) -> CheckResult:
    """商家级品牌禁词 —— 按**出现位置**分成两个严重度不同的检查。

    ## 这条检查是怎么来的（2026-07-27 实测教训，第二次撞同一面墙）
    某服装电商租户的三个人格文件里用三种说法写了「不要把身体当成需要遮掩的问题」
    （researcher 说选题就该重构、copywriter 说 Never frame a body as a problem、
    assistant 说遇到这种框架要 push back）。结果 auto 流程照样产出了标题
    `Best Fitting T-Shirts for Men with Belly`。

        ⭐ **prompt 里的禁令不是 enforcement。**
        这跟"摘要 150-160 在 prompt 里写了三遍还是不合规"是同一个教训 ——
        真要兜住的东西，必须放进确定性层（正则拦）或裁判层（判语境），
        写在 prompt 里只提高首次正确率，不提供保证。

    ## 为什么按位置分两级，而不是一个 hard 了事
    同一个词在不同位置的性质不一样：
      · 标题 / 摘要 / 小标题 = **品牌门面**（进搜索结果、进社交分享卡）→ 零容忍，hard
      · 正文 = 可能有合法用法（"the hem sits below the belly" 是中性的身体部位描述，
        不是"把肚子当问题"）→ 只提醒，soft
    一律 hard 会制造假阳性并触发昂贵的整篇重写；一律 soft 则等于没拦住最要命的那处。

    ## 分档定位（幻觉/合规四档处置里的 C 类）
    这是**高召回低精确的筛子**：作用不是"判断这句话对不对"，而是把审查范围从
    一整篇文章缩到三句话。正文那一档标成 soft 之后，真正的语境判断交给
    reviewer（它的 soul 里写了 body-neutral 要求）—— 正则负责发现，LLM 负责判语境。

    Args:
        surface:       "headline" | "body"，只用于 detail 文案和检查项命名
        text_by_field: {字段名: 文本}，命中时能精确说出"在哪个字段"
        terms:         商家配置的禁词列表（来自 merchant.json 的 brand_voice.forbidden_terms）
    """
    name = f"brand_voice_{surface}"
    label = "title/excerpt/subheads" if surface == "headline" else "body"

    # 没配禁词 → 明确报"跳过 + 原因"，不假装通过。
    # 这是租户级政策：Harbor Auto Spa 那边根本没有这条禁令，所以它那边永远走这一支。
    if not terms:
        return CheckResult(name, True,
                           "Skipped (this merchant's merchant.json has no brand_voice.forbidden_terms)",
                           metric=None)

    # 中间产物 hits：[(字段名, 命中的禁词, 命中的原文片段), ...]
    # 带原文片段是为了让打回指令能精确到句，而不是只说"有个禁词"。
    hits: list[tuple[str, str, str]] = []
    for field_name, text in text_by_field.items():
        if not text:
            continue
        for term in terms:
            pattern = _keyword_regex(str(term))   # 复用关键词那套宽松匹配（词边界 + 可选复数）
            if not pattern:
                continue
            m = pattern.search(text)
            if m:
                # 截一小段上下文（前后各 30 字符），够定位又不至于把整篇塞进日志
                start, end = max(0, m.start() - 30), min(len(text), m.end() + 30)
                hits.append((field_name, str(term), text[start:end].strip()))

    ok = not hits
    if ok:
        detail = f"No banned brand terms in {label} (checked {len(terms)} terms)"
    else:
        shown = "; ".join(f'"{t}" in {f}: …{ctx}…' for f, t, ctx in hits[:3])
        more = f", plus {len(hits) - 3} more" if len(hits) > 3 else ""
        action = (
            "the whole angle has to change, not just the word — this phrasing is not allowed "
            "on brand-facing surfaces"
            if surface == "headline"
            else "check whether these treat the body as a problem to hide; "
                 "a neutral body-part description can stay"
        )
        detail = f"Banned brand term in {label} ({len(hits)} occurrence(s)): {shown}{more} — {action}"
    return CheckResult(name, ok, detail, metric=[t for _, t, _ in hits])


# 价格的字面形式：`$32` / `$32.00` / `$1,299` / `USD 46`。
# ⚠️ 刻意**只认带货币符号的数字**。裸数字绝不能碰 ——
# 正文里到处都是无害的数字（"40% cotton"、"6-inch inseam"、"2-4 years"、"size 42"），
# 把它们当价格核对会制造大量假阳性，这个检查第一天就会被关掉。
# 判据：**只在能确定语义的地方做确定性判断**，语义模糊的交给别人。
_PRICE_RE = re.compile(r"(?:\$|USD\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)", re.I)


def _check_catalog_grounding(
    text_by_field: dict[str, str],
    allowed_prices: set[float],
    has_catalog: bool,
) -> CheckResult:
    """正文里出现的每个价格，都必须在商品目录里存在。否则 hard 阻断。

    ## 为什么这条是 hard（而且是整套检查里最该 hard 的一条）
    那个租户的 copywriter 人格里写着「**Never invent a number**」，reviewer 人格里写着
    「一个编造的价格比一篇枯燥的文章更糟，因为枯燥不花钱，而错价格是客服问题」。
    但这是**第三次**验证同一件事了（前两次是摘要长度、body-negative 措辞）：

        ⭐ **prompt 里的禁令不是 enforcement。**
        写在 prompt 里只提高首次正确率；要保证，必须放进确定性层。

    幻觉三分类里，编造价格属于**事实性幻觉（extrinsic hallucination）**，
    对应的处置是 grounding + attribution + verification 三件套：
        grounding    → retrieval/catalog.py 把真实价格摆进 prompt（预防）
        attribution  → prompt 里要求"只能引用给你的价格"（预防）
        verification → ★这一条★ 出口白名单校验（**唯一的保证**）
    前两件都只是降低概率，**只有出口校验是保证**。

    ## 为什么是白名单而不是黑名单
    我们不可能列出"所有错误的价格"，但**可以列出所有正确的价格**（目录就是全集）。
    所以判定方式是：抽出正文里所有价格 → 不在白名单里的就是编的。
    这也是"模型不许创造标识符，只许引用我给它的标识符"这条原则的价格版。

    Args:
        text_by_field: {字段名: 文本}，命中时能说出"在哪个字段"
        allowed_prices: 目录里的合法价格集合（float，所以 $32 和 $32.00 等价）
        has_catalog:    该商家有没有配目录。没有 → 明确跳过，不假装通过
    """
    if not has_catalog:
        # 租户级可选能力：harbor_auto_spa（汽车贴膜服务）没有商品目录。
        # 明确报"跳过 + 原因"，绝不默认判过 —— 缺失的检查不等于通过的检查。
        return CheckResult("catalog_grounding", True,
                           "Skipped (this merchant has no catalog.json, cannot verify prices)",
                           metric=None)

    # 中间产物 found：[(字段名, 原文里的价格字面量, 解析出的数值), ...]
    found: list[tuple[str, str, float]] = []
    for field_name, text in text_by_field.items():
        for m in _PRICE_RE.finditer(text or ""):
            raw = m.group(0)
            try:
                value = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            found.append((field_name, raw, value))

    if not found:
        # 一个价格都没提 → 通过。**不要求文章必须提价格** ——
        # 「没提」是完全正当的写法（人格里也鼓励"说不准就定性描述"），
        # 要求必须提反而会逼模型去编。
        return CheckResult("catalog_grounding", True,
                           f"No prices mentioned ({len(allowed_prices)} catalog prices were available)",
                           metric=[])

    # 中间产物 ungrounded：核不上目录的那些 —— 就是编出来的
    ungrounded = [(f, raw, v) for f, raw, v in found if v not in allowed_prices]

    ok = not ungrounded
    if ok:
        quoted = ", ".join(sorted({raw for _, raw, _ in found}))
        detail = f"All {len(found)} prices in the article verified against the catalog ({quoted})"
    else:
        shown = "; ".join(f"{raw} in {f}" for f, raw, _ in ungrounded[:4])
        more = f", plus {len(ungrounded) - 4} more" if len(ungrounded) > 4 else ""
        allowed_str = ", ".join(f"${p:g}" for p in sorted(allowed_prices)[:8])
        detail = (
            f"Prices that don't exist in the catalog ({len(ungrounded)}): {shown}{more}"
            f" — the only valid catalog prices are {allowed_str}. "
            f"Delete these numbers or use a real catalog price; if unsure, describe it "
            f"qualitatively instead of estimating or rounding"
        )
    return CheckResult("catalog_grounding", ok, detail,
                       metric=[v for _, _, v in ungrounded])


def _check_forbidden_claims(fields: dict[str, str]) -> CheckResult:
    """A 类硬禁词：绝对化承诺，确定性阻断，不问 LLM。

    正则在这里的定位要说清楚：它是**高召回低精确的筛子**。作用不是"判断这句话对不对"，
    而是"把审查范围从一整篇文章缩到三句话"。A 类之所以能直接拦，是因为这几个模式
    **压根没有合法用法** —— 不需要判语境，所以不用送 LLM，省一次调用。

    中间产物 hits：`[(字段名, 违规类型, 命中的原文), ...]` —— 打回时精确到句，
    这样 rewrite 是定点改那一句，而不是整篇重写（省钱，且不破坏已通过的部分）。
    """
    hits: list[tuple[str, str, str]] = []
    for field_name, text in fields.items():
        for pattern, label in _FORBIDDEN_CLAIMS_COMPILED:
            for m in pattern.finditer(text or ""):
                hits.append((field_name, label, m.group(0)))

    ok = not hits
    if ok:
        detail = (f"No absolute-claim language "
                  f"(checked {len(_FORBIDDEN_CLAIMS_COMPILED)} banned patterns)")
    else:
        shown = "; ".join(f'{label} in {name}: "{text}"' for name, label, text in hits[:4])
        more = f", plus {len(hits) - 4} more" if len(hits) > 4 else ""
        detail = (f"Found {len(hits)} absolute claim(s) (brand-safety risk, possible false "
                  f"advertising): {shown}{more}"
                  f" — rewrite with a qualifier, e.g. \"typically holds up for X years "
                  f"under normal use\"")
    return CheckResult("forbidden_claims", ok, detail, metric=[h[2] for h in hits])


# ── 对外唯一入口 ──────────────────────────────────────────────────────


def check_channel_content(
    headline_fields: dict[str, str],
    body_fields: dict[str, str],
    *,
    brand_terms: Sequence[str] | None = None,
    catalog: dict | None = None,
    allow_emoji: bool = False,
) -> DeterministicReport:
    """校验**非博客**的渠道内容（邮件主题/社媒文案/短信…）。

    ## 为什么单独开一个入口，而不是复用 check_seo
    `check_seo` 是**博客形状**的：它要数 h2、查图片占位符、比内外链条数、卡摘要 150-160。
    这些对一条 Instagram caption 全都没有意义。硬塞会得到一堆"跳过"，
    而"跳过"太多的报告等于没有报告 —— 人会开始忽略它。

    ## 但检查项本身是**共享**的，这才是重点
    真正渠道无关的是这三条 —— 它们查的是**事实和品牌安全**，与载体无关：
        品牌禁词      body-negative 措辞在邮件里和在博客里一样不能出现
        绝对化承诺    "100% 保证"在哪个渠道都是合规风险
        价格接地      编造的价格在短信里危害**更大**（更难撤回）
    所以这三条直接复用博客那边同一个函数、同一张严重度表。

        ⭐ 这就是"内容原子全渠道"能成立的前提：**校验层对渠道无感知，
           渠道只决定哪些检查适用，不决定检查怎么做。**

    ## allow_emoji：同一个检查项的严重度是**渠道相关**的
    emoji 在博客里是 hard 禁（破坏 WordPress 渲染 + 多语言 TTS，SEO 十条第 10 条），
    但在 Instagram caption 里是**正常表达**，禁掉反而不对。
    所以它不进"渠道无关"那一组，而是由调用方按渠道开关 ——
    这是分级判据在渠道维度上的自然延伸，不是例外。

    Args:
        headline_fields: 门面字段 {名字: 文本} —— 邮件主题行 / caption 首句。
                         品牌禁词在这里是 **hard**（进收件箱预览、进信息流第一行）。
        body_fields:     正文字段 —— 品牌禁词在这里是 **soft**（可能有合法用法）。
        brand_terms:     租户级品牌禁词，同 check_seo
        catalog:         商品目录，价格白名单来源，同 check_seo
        allow_emoji:     True = 不查 emoji（社媒）；False = 查（邮件正文/短信）
    """
    all_fields = {**headline_fields, **body_fields}

    _products = (catalog or {}).get("products") or []
    _allowed_prices: set[float] = {
        float(p["price_usd"]) for p in _products
        if isinstance(p.get("price_usd"), (int, float))
    }

    checks: list[CheckResult] = [
        # 三条渠道无关的：和博客走**同一个函数**，不是复制一份
        _check_brand_voice("headline", headline_fields, brand_terms or ()),
        _check_brand_voice("body", body_fields, brand_terms or ()),
        _check_forbidden_claims(all_fields),
        _check_catalog_grounding(all_fields, _allowed_prices, bool(_products)),
    ]
    if not allow_emoji:
        checks.append(_check_no_emoji(all_fields))

    for c in checks:
        c.severity = _SEVERITY.get(c.name, "hard")

    passed_checks = [c.detail for c in checks if c.ok]
    failures = [c.detail for c in checks if not c.ok]
    hard_failures = [c.detail for c in checks if not c.ok and c.severity == "hard"]
    soft_failures = [c.detail for c in checks if not c.ok and c.severity != "hard"]

    return DeterministicReport(
        hard_ok=not hard_failures,
        passed_checks=passed_checks,
        failures=failures,
        hard_failures=hard_failures,
        soft_failures=soft_failures,
        metrics={c.name: c.metric for c in checks},
        checks=checks,
    )


def check_seo(
    blog_data: dict,
    topic: dict,
    *,
    website: str = "",
    expected_slots: Sequence[str] | None = None,
    brand_terms: Sequence[str] | None = None,
    catalog: dict | None = None,
) -> DeterministicReport:
    """跑全部确定性检查，返回一份报告。零 LLM、零网络、零副作用。

    Args:
        blog_data:      copywriter 输出的稿件 dict。★只读，绝不修改★
                        （review_loop 里 blog_data 是被原地改的，校验器再动它就会出鬼）
        topic:          选题 dict，用 primary_keyword / secondary_keywords
        website:        商家官网 URL（区分内链外链用）。不传 → 两项链接检查报"跳过"
        expected_slots: 本篇应有的图片槽位，如 ("hero","mid","end")。
                        不传 → 占位符检查报"跳过"
        brand_terms:    该商家的品牌禁词列表（merchant.json → brand_voice.forbidden_terms）。
                        **刻意做成参数而不是在这里读配置**：本模块必须保持纯函数，
                        而且禁词是**租户级政策**（服装电商禁 body-negative 措辞，Harbor Auto Spa 没这条），
                        所以它属于"外部事实"，由调用方注入。不传 → 两项品牌检查报"跳过"。
        catalog:        该商家的商品目录（retrieval.catalog.load_catalog 的产物）。
                        同样是**租户级可选能力**（电商租户有，harbor_auto_spa 服务业没有），
                        同样由调用方注入以保持本模块纯函数。不传 → 接地校验报"跳过"。
    Returns:
        DeterministicReport

    执行分五步，产物一层层往上传：
    """
    # ── Step 1: 解析 HTML，拿到后面所有检查共用的中间产物 ─────────────
    # 用 bs4 + stdlib 的 html.parser 后端（不引 lxml，少一个 C 扩展依赖）。
    # 产物有两个：
    #   soup       DOM 树 —— 数标签（h1/h2/img/a）靠它
    #   body_text  纯文本 —— 数关键词靠它
    # ⚠️ 关键细节：bs4 的 get_text() **不会**包含 HTML 注释的内容，所以
    #    <!-- BLOG_IMAGE:hero --> 里的 "BLOG_IMAGE" 不会污染关键词统计。
    #    这个行为有测试兜着（test_placeholder_not_counted_as_body_text），
    #    因为它是个隐含假设，哪天 bs4 改了行为必须立刻知道。
    content_html = blog_data.get("content_html") or ""
    soup = BeautifulSoup(content_html, "html.parser")
    body_text = soup.get_text(" ", strip=True)

    # ── Step 2: 预处理各检查需要的输入 ────────────────────────────────
    # 2a) 链接分三堆（内/外/不计）—— 两项链接检查共用这一次遍历
    site_host = _host_of(website)
    internal, external, _skipped = _classify_links(soup, site_host)

    # 2b) 汇总"所有会被渲染出去的文本字段" —— emoji 和硬禁词要扫的是这一整套，
    #     不能只扫正文：标题、摘要、图片 alt 任何一处出问题，用户都看得见。
    #     image_alts 的值可能不是字符串（LLM 偶尔给 dict/None），所以统一 str() 兜一层。
    alts = blog_data.get("image_alts")
    alt_fields = (
        {f"image_alts.{k}": str(v) for k, v in alts.items()} if isinstance(alts, dict) else {}
    )
    text_fields: dict[str, str] = {
        "title": str(blog_data.get("title") or ""),
        "excerpt": str(blog_data.get("excerpt") or ""),
        "content_html": content_html,
        **alt_fields,
    }

    # 2b-2) 商品目录 → 价格白名单
    # 为什么在这里算而不是让 check 函数自己去读 catalog：
    # 保持"每个 check 函数只做判断、不碰数据准备"的分工 —— 数据准备集中在 Step 2，
    # 出错时只有一个地方要看。产物是 float 集合，所以 $32 和 $32.00 天然等价。
    _products = (catalog or {}).get("products") or []
    _allowed_prices: set[float] = {
        float(p["price_usd"]) for p in _products
        if isinstance(p.get("price_usd"), (int, float))
    }

    # 2c) 给品牌禁词检查切出「门面」和「正文」两个面
    # 为什么要切开：同一个词在标题里和在正文里的性质不同（详见 _check_brand_voice）。
    #   门面 = 标题 + 摘要 + 所有小标题（h2/h3）—— 这些会进搜索结果和社交分享卡
    #   正文 = 去掉小标题之后剩下的散文
    # 实现上先取小标题文本，再在**副本** soup 上把小标题拆掉得到纯正文 ——
    # 用副本是因为主 soup 后面还要数 h1/h2/img，不能被破坏性操作污染。
    heading_fields = {
        f"heading[{i}]": h.get_text(" ", strip=True)
        for i, h in enumerate(soup.find_all(["h2", "h3"]), 1)
    }
    headline_fields: dict[str, str] = {
        "title": text_fields["title"],
        "excerpt": text_fields["excerpt"],
        **heading_fields,
    }
    body_soup = BeautifulSoup(content_html, "html.parser")   # 中间产物：可破坏的副本
    for h in body_soup.find_all(["h1", "h2", "h3", "h4"]):
        h.decompose()
    body_only_text = body_soup.get_text(" ", strip=True)     # 中间产物：不含小标题的正文

    # ── Step 3: 逐项跑检查 ────────────────────────────────────────────
    # 清单式排列 = 加新检查只在这里加一行。顺序按「SEO 十条」的编号走，
    # 这样出错信息的顺序和 copywriter prompt 里的规则顺序对得上，便于人肉核对。
    checks: list[CheckResult] = [
        _check_no_h1(soup),                                        # 第 1 条
        _check_primary_keyword_early(body_text, str(topic.get("primary_keyword") or "")),  # 第 2 条
        _check_h2_count(soup),                                     # 第 3 条（数量）
        _check_h2_keyword_stuffing(soup),                          # 第 3 条（质量：别换皮重复）
        _check_secondary_keywords(body_text, topic.get("secondary_keywords") or []),       # 第 5 条
        _check_excerpt_length(blog_data.get("excerpt") or ""),      # 第 6 条
        _check_internal_links(internal, site_host),                 # 第 7 条
        _check_external_links(external, site_host),                 # 第 8 条
        _check_no_emoji(text_fields),                               # 第 10 条
        _check_no_img_tags(soup),                                   # 图片契约（prompt 里的 CRITICAL 那条）
        _check_placeholders(content_html, expected_slots),           # pipeline 层不变式
        _check_forbidden_claims(text_fields),                       # A 类硬禁词（品牌安全，全局）
        # 事实性幻觉的**出口校验** —— grounding 三件套里唯一提供保证的那一件
        _check_catalog_grounding(text_fields, _allowed_prices, bool(_products)),
        # 品牌禁词按位置分两级 —— 租户级政策，词表来自 merchant.json
        _check_brand_voice("headline", headline_fields, brand_terms or ()),
        _check_brand_voice("body", {"content_html": body_only_text}, brand_terms or ()),
    ]

    # ── Step 4: 打上严重度 ────────────────────────────────────────────
    # 严重度不由各个 check 函数自己决定，而是在这里统一从 _SEVERITY 查表贴上去。
    # 这样"我们到底拦什么"只有一张表，改分级不用碰任何检查逻辑。
    for c in checks:
        c.severity = _SEVERITY.get(c.name, "hard")   # 查不到 → 兜底 hard（宁可误拦不可误放）

    # ── Step 5: 汇总成报告 ────────────────────────────────────────────
    # hard_ok 只看 hard 项：soft 项全炸也照样放行，只是会被报告出来。
    # 之所以不把所有项做成加权评分，是因为这些都是**是非题**不是程度题 ——
    # "摘要 138 字"没有"扣 5 分"的说法，它就是没落进区间。打分是 LLM 裁判那一层的事。
    passed_checks = [c.detail for c in checks if c.ok]
    failures = [c.detail for c in checks if not c.ok]                       # 全集
    hard_failures = [c.detail for c in checks if not c.ok and c.severity == "hard"]
    soft_failures = [c.detail for c in checks if not c.ok and c.severity != "hard"]
    metrics = {c.name: c.metric for c in checks}
    # 顺手把两个高频排查用的原始数字也塞进 metrics（golden test 会直接断言它们）
    metrics["body_word_count"] = len(body_text.split())
    metrics["excerpt_chars"] = len((blog_data.get("excerpt") or "").strip())

    return DeterministicReport(
        hard_ok=not hard_failures,        # ← 只看 hard，这是唯一的闸门依据
        passed_checks=passed_checks,
        failures=failures,
        hard_failures=hard_failures,
        soft_failures=soft_failures,
        metrics=metrics,
        checks=checks,
    )
