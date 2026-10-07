"""审稿-重写循环 — auto / chat 两条 pipeline 共用的唯一实现

## 为什么单独抽一个模块
原先这段循环在两处各写了一遍：
  - `pipeline/blog_generator.py`（auto 流程，内联在 _generate_single_inner 里）
  - `pipeline/chat_generator.py`（chat 流程，私有函数 _run_review_loop）
两份实现的行为**并不完全一致**，改一处必漏另一处。收口到这里之后，
「审几轮、多少分算过、不过怎么重写」只有一个事实来源。
（这次接确定性校验层收到了这笔红利：**接线点只有这一个文件**，两条 pipeline 同时生效。）

## ⚠️ 收口时顺手修掉的一个真 bug（auto 流程丢 image_alts）
旧 auto 版写的是：
    blog_data, _ = rewrite_blog(...)          # ← 整体替换引用
而 `rewrite_blog` 的输出格式（agents/copywriter.py:619-628）**只包含**
title / content_html / excerpt / tags / seo_slug / image_prompts，
**没有 image_alts**。所以 auto 流程只要触发过一次重写，
`blog_data["image_alts"]` 就整体消失 →
`preview_server.render_blog_html`（:78）取不到 alt，退化成
`f"{title} - {slot}"` 这种垃圾 alt，`wordpress_publisher` 也拿不到 alt。
SEO 要求第 9 条（图片 alt 带关键词）直接失效，而且**静默无日志**。

本模块统一改成原地合并：
    blog_data.update(revised)                 # ← 新键覆盖旧键，旧键保留
这样 image_alts 这类「重写不返回但必须保留」的字段不会被抹掉。
chat 流程原本就是 `.update()`，行为不变。

## 每一轮的顺序：先修 → 再校验 → 最后才花钱（cascaded evaluation + early rejection）
    ┌─ 第 N 轮 ──────────────────────────────────────────────────────┐
    │ ① eval.repair_all()   确定性修复，零成本，★原地改 blog_data★   │
    │      能机械修好的当场修掉（目前：摘要超长裁到词边界）           │
    │ ② eval.check_seo()    确定性校验，零成本，★只读★              │
    │      ├─ hard 项失败 → 【不调 reviewer】把 failures 当 issues    │
    │      │                打回 rewrite（省一次调用）               │
    │      └─ hard 全过    → 往下走（soft 失败不阻断，只记录）       │
    │ ③ reviewer.review_blog()  花钱的 LLM 裁判                       │
    │ ④ 若判不过 → 把 soft 失败并进同一份 issues（搭车，零额外成本）  │
    │ ⑤ 通过条件：hard_ok AND passed AND score >= 阈值                │
    │      不过且还有轮次 → rewrite_blog() 打回重写                   │
    └────────────────────────────────────────────────────────────────┘

为什么是这个顺序（三条都是实测代价换来的）：
  · **修在校验前** —— 否则会出现"摘要超 1 个字符 → 花 $0.028 整篇重写"的赔本操作。
    判据不对称：删字不需要新信息（代码能做），补字需要（只能问模型）。
  · **只为 hard 拦停** —— soft 项（摘要区间/链接条数/h2 数量/关键词覆盖）是"不够好"，
    不是"坏的"。拿启发式去触发整篇重写，性价比是负的。
  · **soft 搭车** —— 一旦因为别的原因已经要重写了，就把软问题一起塞进 issues，
    免费修掉。软问题从不单独触发重写。
  · 反馈质量 —— 确定性检查给的是「摘要 138 字，要求 150-160」这种带数字的定点指令，
    LLM 反馈常常是「摘要可以更吸引人」，没法定点修。
  · 少一条分支 —— 硬性打回复用同一个 rewrite 路径（靠 report.as_feedback() 做成
    与 reviewer 输出同构的 dict），不为它另写分支。

## 输入 / 输出
    run_review_loop(merchant_id, blog_data, topic, merchant_cfg=..., mode=...,
                    image_count=..., progress_cb=...)
      输入 blog_data:    copywriter 的输出 dict —— ★会被原地修改★（重写后的内容写回同一个 dict）
      输入 topic:        {primary_keyword, secondary_keywords, ...}，给 reviewer 和校验器做对标
      输入 merchant_cfg: 商家配置（必填），取 website（区分内外链）+ brand_voice.forbidden_terms
                         （租户级品牌禁词）。
                         **刻意不在模块内部全局查表** —— 理由见 run_review_loop 的 docstring。
      输入 mode:      "auto" → 占位符 hero/mid/end；"chat" → img_1..img_N
      输入 progress_cb(stage, extra): 可选，用于刷 Slack 进度条；只会收到 "review" / "rewrite"
      输出 (review_score, review_rounds, report) —— 三元组

⚠️ 契约变更（2026-07-27）：返回值由 `(score, rounds)` 变成 `(score, rounds, report)`。
两个调用处（blog_generator.py / chat_generator.py）**都已改死，不留兼容层** ——
内部 API 加字段就直接 required + fail-fast，加 Optional 兜底只会让漏改的地方静默降级。

## report 的两条不变式（调用方可以依赖）
① **report 永远描述"最终那一版稿子"**。因为检查发生在每轮开头，而重写只发生在
   「还有下一轮」的时候 —— 所以最后一次检查的对象，就是循环结束时 blog_data 的状态。
② **hard_ok=False 时返回的 score 一定是 0**。理由是诚实：这一版稿子从没被 reviewer
   评过分，卡片上不能挂着上一版（已经不存在的那版）的 85 分。
   宁可显示 0 + 一列红叉，也不要显示一个属于别人的分数。

阈值来自 config：REVIEWER_MAX_ROUNDS / REVIEWER_MIN_SCORE / EVAL_*（含闸门开关 EVAL_HARD_GATE）。
最终轮仍不过 = **不阻断**，用当前版本继续走后面的生图/渲染（只 log.warning）。
"""

import logging

import config as cfg
from agents.copywriter import rewrite_blog
from agents.heading_fixer import fix_stuffed_headings
from agents.reviewer import review_blog
from eval.deterministic import DeterministicReport, check_seo
from eval.repair import repair_all
from retrieval.catalog import load_catalog

log = logging.getLogger(__name__)


class _TargetedFix:
    """模型辅助的定点修复记录 —— 和 eval.repair.RepairRecord 同形状（都有 .note）。

    为什么需要这个壳子：`all_repairs` 收尾时统一读 `r.note`，混进裸字符串会 AttributeError。
    单独一个类而不是复用 RepairRecord，是为了让两类修复在类型上就能区分：
      RepairRecord  确定性修复，零成本、结果确定（删字、裁剪）
      _TargetedFix  模型辅助修复，花钱、结果不完全确定（换措辞）
    """

    __slots__ = ("note",)

    def __init__(self, note: str) -> None:
        self.note = note



def _expected_slots(mode: str, image_count: int) -> tuple[str, ...]:
    """推导本篇**应该**有哪几个图片槽位，交给校验器做集合比对。

    这是 pipeline 层不变式「占位符集合 == 槽位集合」的左半边来源。
    两种模式的槽位命名规则不同（和 agents/copywriter.py:_build_image_instructions 一致）：
      auto → 固定三张 hero / mid / end
      chat → img_1 .. img_N，N 由用户在对话里定

    ⚠️ 这里是**按规则推导**而不是从 blog_data 里读现成的键 —— 故意的：
    如果拿 blog_data["image_alts"].keys() 当"应该有的槽位"，那就是拿 LLM 的输出
    去验证 LLM 的输出，它写错了也自洽，检查等于没做。校验的参照物必须来自代码。
    """
    if mode == "chat":
        return tuple(f"img_{i}" for i in range(1, max(1, image_count) + 1))
    return ("hero", "mid", "end")


def run_review_loop(
    merchant_id: str,
    blog_data: dict,
    topic: dict,
    *,
    merchant_cfg: dict,
    mode: str = "auto",
    image_count: int = 3,
    progress_cb=None,
) -> tuple[int, int, DeterministicReport]:
    """跑「确定性校验 → 审稿 → 不过就重写」的循环。

    Args:
        merchant_id:  商家标识，如 "harbor_auto_spa" / "summit_hvac"（只用于日志和记账）
        blog_data:    当前稿件 dict，★原地修改★（调用方继续用同一个对象即可拿到最新稿）
        topic:        选题字典，校验器和 reviewer 都用它的 primary/secondary_keywords
        merchant_cfg: 商家配置 dict（**必填**）。取两样东西：
                        · website                        —— 区分内链/外链
                        · brand_voice.forbidden_terms    —— 租户级品牌禁词（电商租户有，Harbor Auto Spa 没有）
                      为什么必填而不是自己去 merchant_config 查：
                        ① 两个调用方手里都已经有这个 dict 了（它们就是靠它跑起来的），
                           模块内再全局查一遍 = **同一个事实有两个来源**，两边可能不一致；
                        ② `merchant_config._merchant_map` 是 main.py 启动时才填的全局状态。
                           依赖它意味着"从脚本或测试里调这条 pipeline"会静默拿到空配置 ——
                           链接检查悄悄变成"跳过"，而没人会注意到。（这个坑是真踩到的：
                           第一版写成全局查表，脚本里跑立刻暴露。）
                      按 [feedback_no-backward-compat-shims]：直接 required，不留 Optional 兜底。
        mode:         "auto" | "chat"，决定槽位命名和 rewrite 时的占位符提示
        image_count:  图片数量（chat 模式用来枚举 img_1..img_N）
        progress_cb:  进度回调 (stage: str, extra: str) -> None，异常会被吞掉不影响主流程

    Returns:
        (review_score, review_rounds, report)
        report 描述的是**最终这一版**稿子（见模块头两条不变式）
    """
    max_rounds = cfg.REVIEWER_MAX_ROUNDS
    min_score = cfg.REVIEWER_MIN_SCORE

    # ── Step 0: 准备两个「校验参照物」，整个循环里不变 ───────────────
    # 都是从**代码/配置**取的，不是从 LLM 输出取的 —— 参照物必须独立于被校验对象。
    #   website        商家官网域名，用来区分内链/外链（第 7、8 条）
    #   expected_slots 本篇应有的图片槽位集合（不变式的左半边）
    # 拿不到 website（配置里没写）→ 那两项检查会明确报"跳过 + 原因"，而不是默认判过。
    website = (merchant_cfg or {}).get("website") or (merchant_cfg or {}).get("wordpress_url") or ""
    expected_slots = _expected_slots(mode, image_count)
    # brand_terms：**租户级**品牌禁词（merchant.json → brand_voice.forbidden_terms）。
    # 没配就是空列表 → 两项品牌检查报"跳过"。这是刻意的：Harbor Auto Spa 那边没有这条政策，
    # 不该被别家租户的词表误伤 —— 品牌安全规则能按租户配置，多租户才是真的。
    brand_terms = ((merchant_cfg or {}).get("brand_voice") or {}).get("forbidden_terms") or []
    # catalog：商品目录，接地校验的价格白名单来源。同样是**租户级可选能力** ——
    # 电商租户有 catalog.json，harbor_auto_spa（汽车贴膜服务）没有，后者的接地校验报"跳过"。
    # 在循环外只读一次：目录在一次生成里不会变，每轮重读是白做 IO。
    catalog = load_catalog(merchant_id)

    review_score = 0          # 最后一次**真实** reviewer 评分（没评过就是 0）
    review_rounds = 0         # 实际跑了几轮
    report: DeterministicReport | None = None   # 最后一次确定性检查的结果

    def _emit(stage: str, extra: str = "") -> None:
        """安全触发进度回调 —— 刷 Slack 失败绝不能把生成流程带崩。"""
        if not progress_cb:
            return
        try:
            progress_cb(stage, extra)
        except Exception:
            log.warning("[%s] 进度回调失败 (stage=%s)，忽略", merchant_id, stage)

    # 累积所有轮次的确定性修复记录 —— 最后挂到 report 上给卡片显示
    all_repairs: list = []

    for round_num in range(1, max_rounds + 1):
        review_rounds = round_num

        # ── Step 1: 确定性修复（零成本，比校验还靠前）──────────────────
        # 顺序是刻意的：**先把能机械修好的修掉，再去校验**。否则会出现
        # "校验说摘要超 1 个字符 → 打回花 $0.028 整篇重写"这种赔本操作（2026-07-27 实测）。
        # 判据不对称：删字不需要新信息（代码能做），补字需要（只能问模型）。
        # ★ repair_all 会原地改 blog_data ★ —— 和 check_seo 的只读刚好是相反的分工。
        for r in repair_all(blog_data):
            all_repairs.append(r)
            log.info("[%s][%s] 确定性修复 (round %d): %s", merchant_id, mode, round_num, r.note)

        # ── Step 2: 确定性校验（零成本）─────────────────────────────
        # 产物 report：hard_ok / hard_failures / soft_failures / passed_checks / metrics。
        # ★只读 blog_data，不修改★（有测试断言这件事）。
        report = check_seo(
            blog_data, topic,
            website=website, expected_slots=expected_slots, brand_terms=brand_terms,
            catalog=catalog,
        )
        log.info(
            "[%s][%s] 确定性校验 (round %d/%d) — %s | 摘要 %s 字 / 正文 %s 词",
            merchant_id, mode, round_num, max_rounds, report.summary_line(),
            report.metrics.get("excerpt_chars"), report.metrics.get("body_word_count"),
        )
        # ⚠️ 日志前缀刻意只用 ASCII。第一版写的是 "✗"，结果在 Windows 控制台（GBK）上
        # logging 直接抛 UnicodeEncodeError，**11 条日志全被丢掉** —— 日志是排查工具，
        # 它自己不能有编码依赖。同一个坑家族：[feedback_tts-special-chars]。
        # HARD / soft 分开打：看日志立刻知道这轮会不会被拦，不用去翻 Slack 卡片。
        for f in report.hard_failures:
            log.warning("[%s][%s]   HARD FAIL: %s", merchant_id, mode, f)
        for f in report.soft_failures:
            log.info("[%s][%s]   soft: %s", merchant_id, mode, f)

        # ── Step 3: 闸门判定 —— 只有 hard 失败才值得拦停 ──────────────
        # soft 失败（摘要区间 / 链接条数 / h2 数量 / 关键词覆盖）不阻断：
        # 它们是"不够好"，不是"坏的"。为它们触发整篇重写的性价比是负的 ——
        # 实测一次 rewrite ≈ $0.028，而"内链 6 条而不是 5 条"的实际损失接近于零。
        # EVAL_HARD_GATE=False 时连 hard 也不拦（退化成纯报告模式）。
        hard_blocked = (not report.hard_ok) and cfg.EVAL_HARD_GATE

        if hard_blocked:
            # 【不调 reviewer】把确定性 failures 包装成与 reviewer 同构的 feedback，
            # 直接走下面同一条 rewrite 路径。省下的是一次 reviewer 调用的钱。
            # 注意 as_feedback() 里的 issues 是 **hard + soft 全集**：既然已经决定
            # 要花这笔重写的钱，顺手把软问题一起修掉是免费的。
            feedback = report.as_feedback()
            # review_score 保持 0 —— 这一版没被评分过，不能借用上一版的分数（不变式②）
            review_score = 0
            _emit("review", f"{len(report.hard_failures)} hard check(s) failed — skipping AI review")
            log.info(
                "[%s][%s] HARD 检查未通过（%d 项）→ 跳过 reviewer（省一次调用），直接打回重写",
                merchant_id, mode, len(report.hard_failures),
            )
            passed_all = False
        else:
            # ── Step 4: LLM 裁判（花钱）──────────────────────────────
            _emit("review", f"Review round {round_num}/{max_rounds}")
            log.info("[%s][%s] Reviewer 审核 (round %d/%d)...",
                     merchant_id, mode, round_num, max_rounds)

            feedback, _ = review_blog(merchant_id, blog_data, topic, round_num)
            review_score = feedback.get("score", 0)

            # ── Step 5: soft 失败搭车 ────────────────────────────────
            # reviewer 判不过、马上要重写时，把 soft 失败**并进同一份 issues**。
            # 这是 soft 分级的关键收益：软问题不单独触发重写（那是赔本），
            # 但只要因为别的原因已经要重写了，就顺路把它们一起修掉 —— **零额外成本**。
            # 只在"要重写"时并入，不在"已通过"时并入：已经通过的稿子不该被软问题拖回来。
            if report.soft_failures and not feedback.get("passed", False):
                feedback["issues"] = list(feedback.get("issues") or []) + report.soft_failures
                log.info("[%s][%s] 把 %d 项 soft 问题并进本次重写（不额外花钱）",
                         merchant_id, mode, len(report.soft_failures))

            # ── Step 6: 通过条件（三个条件全满足才算过）────────────
            #   hard_ok  代码算出来的 hard 项全过（soft 项不参与，它们不阻断出稿）
            #   passed   reviewer 自己说过了
            #   score    且分数达到配置阈值（双重把关，防 GPT 乱给 passed=true）
            passed_all = (
                report.hard_ok
                and feedback.get("passed", False)
                and review_score >= min_score
            )
            if passed_all:
                soft_note = f"（{len(report.soft_failures)} 项 soft 仅记录）" if report.soft_failures else ""
                log.info("[%s][%s] 审核通过 — %d/100 + HARD 全过 (round %d)%s",
                         merchant_id, mode, review_score, round_num, soft_note)
                break

        # ── Step 7: 没过 → 还有轮次就重写，最后一轮就放行 ────────────
        if round_num < max_rounds:
            _emit("rewrite", f"Score {review_score}/100 — revising (round {round_num})")
            log.info("[%s][%s] 未通过（%s），打回重写...", merchant_id, mode,
                     "HARD 检查" if hard_blocked else f"{review_score}/100")
            revised, _ = rewrite_blog(
                merchant_id, blog_data, feedback, round_num,
                mode=mode, image_count=image_count,
            )
            # ★ 原地合并而非整体替换：保住 rewrite 不返回的字段（image_alts 等）
            blog_data.update(revised)
            # 注意：这里**不重新跑 check_seo** —— 下一轮开头会跑。
            # 这正是"report 永远描述最终稿"这条不变式成立的原因（见模块头）。
        else:
            log.warning("[%s][%s] 最终轮仍未通过（%s），使用当前版本继续",
                        merchant_id, mode,
                        "HARD 检查未过" if hard_blocked else f"{review_score}/100")

    # ── Step 8: 收尾 —— 保证返回值诚实 ──────────────────────────────
    # report 理论上不可能是 None（max_rounds 至少 1），但万一被配成 0，
    # 这里必须给个明确的 report 而不是 None，否则调用方拿到 None 会 AttributeError。
    if report is None:
        log.error("[%s][%s] REVIEWER_MAX_ROUNDS=%s 导致循环一次都没跑，"
                  "补跑一次确定性校验以保证返回值可用", merchant_id, mode, max_rounds)
        report = check_seo(blog_data, topic, website=website,
                           expected_slots=expected_slots, brand_terms=brand_terms,
                           catalog=catalog)

    # 不变式②：最终稿 **hard** 项没过 → 分数归 0。
    # 只看 hard：soft 项没过不影响分数，因为它们本来就不阻断出稿 ——
    # 一篇「内链 6 条但内容 88 分」的稿子就该显示 88 分，把它归零反而是不诚实的。
    # 这一步会覆盖掉"某一轮曾经拿到过的分数"，是有意为之：卡片上的分数必须属于
    # 用户正在看的这一版稿子。
    if not report.hard_ok and review_score:
        log.warning("[%s][%s] 最终稿 HARD 检查未通过，评分由 %d 归零（分数必须属于当前这一版）",
                    merchant_id, mode, review_score)
        review_score = 0

    # ── Step 9: 定点修复（在循环**之后**跑，只跑一次）──────────────────
    # 为什么放在循环外而不是每轮都跑：
    #   ① 循环里的整篇重写本来就会重写标题 —— 在循环内改标题是白花钱，下一轮可能被冲掉；
    #   ② 定点修复不参与"过不过"的判定（它修的是 soft 问题），所以不该影响循环控制流；
    #   ③ 只跑一次 = 成本上界是确定的（一次 ≈ $0.0003），不会出现"修了又检了又修"。
    #
    # 补的缺口：h2 堆砌是 soft → 不阻断 → 只能等"因为别的原因要重写"时搭车。
    # 但实测 reviewer 很宽松（连续两篇 90 分直接通过），那条搭车路径几乎不触发，
    # 于是这类问题会无限累积一条都修不到。这里用一次很便宜的调用把它补上。
    if cfg.EVAL_TARGETED_FIX:
        # metrics 里那一项就是 {重复短语: [标题序号]}，正好告诉我们"问题范围"在哪几个标题
        stuffing = report.metrics.get("h2_keyword_stuffing")
        if stuffing:
            # 先留一份快照 —— 修不好要能回滚（理由见下面那段）
            _before_html = blog_data.get("content_html")
            _before_report = report

            changed, note = fix_stuffed_headings(merchant_id, blog_data, stuffing, topic)
            if changed:
                # ★ 改完必须重新校验 ★ —— 不变式①要求 report 描述"最终那一版稿子"，
                # 而标题刚被换过。重跑一次是零成本的，没有理由不跑。
                report = check_seo(blog_data, topic, website=website,
                                   expected_slots=expected_slots, brand_terms=brand_terms,
                                   catalog=catalog)

                # ⭐ 不变式：**修复必须真的修好，否则回滚。**
                # 2026-07-27 实测：第一版修完堆砌仍然存在（模型和它没看见的标题撞出了
                # 新的重复），而且顺手把 listicle 的编号弄丢了 —— 那一版稿子比修之前更糟。
                # 判据很简单：一个"修复"如果没消除它声称要修的问题，它就不是修复，
                # 是**多余的风险**。既然校验是零成本的，就没有理由不验证再决定留不留。
                still_stuffed = any(c.name == "h2_keyword_stuffing" and not c.ok
                                    for c in report.checks)
                if still_stuffed:
                    blog_data["content_html"] = _before_html      # 原样还回去
                    report = _before_report                        # 报告也回滚，保持自洽
                    log.warning("[%s][%s] 定点修复没能消除堆砌 → 已回滚标题"
                                "（那次调用的钱已经花了，但稿子不会变得更糟）",
                                merchant_id, mode)
                else:
                    all_repairs.append(_TargetedFix(note))
                    log.info("[%s][%s] 定点修复后重新校验 — %s",
                             merchant_id, mode, report.summary_line())

    # 把修复记录挂到 report 上一起交给调用方 —— 卡片要显示"代码在这里动过手"，
    # 不显示的话自动修复就是个黑箱（用户会疑惑"我写的摘要怎么变短了"）。
    report.repairs = [r.note for r in all_repairs]
    if all_repairs:
        log.info("[%s][%s] 本次共做了 %d 处确定性修复（零成本，未调用模型）",
                 merchant_id, mode, len(all_repairs))

    return review_score, review_rounds, report
