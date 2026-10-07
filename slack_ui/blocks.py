"""Slack Block Kit 消息构建器 — 构建博客预览卡片和调度状态消息

所有发送到 Slack 的富消息都通过这里构建。
"""

import logging
import time

from core.i18n import t

log = logging.getLogger(__name__)


def _build_seo_checks_blocks(result: dict, lang: str = "en") -> list[dict]:
    """把确定性 SEO 校验报告渲成 Slack blocks（auto / chat 两张卡片共用）。

    这是 eval 第①层最直接的用户可见收益：把「SEO 十条」从"LLM 说它做到了"
    变成一列**带具体数字**的绿勾 / 红叉。红叉那几条是可执行的指令，
    用户看一眼就知道差多少（"摘要 138 字，要求 150-160"），不需要去读日志。

    输入 result["seo_checks"] = {
        "hard_ok": bool, "passed": [str], "hard_failures": [str], "soft_failures": [str],
        "repairs": [str],   # 可选：代码自动修过的地方
    }
    三档视觉区分（这是这张卡片最重要的信息设计）：
      :x:       hard 失败 —— 产出是坏的/有风险的，**已阻断出稿**
      :warning: soft 失败 —— 只是不够好，没有阻断，可以直接发
      :wrench:  自动修复 —— 代码当场改掉了，没花钱
    如果不做这个区分，用户会把"内链 6 条"当成和"图片碎了"一样严重的事。

    容错：**没有这个键就返回空列表**；旧格式（只有 "failures"）也认。
    这不是偷懒，是必要的 —— store/*.json 里存着这个功能上线之前的草稿，
    重启后读出来渲染时不能炸掉整张卡片。
    """
    checks = result.get("seo_checks")
    if not isinstance(checks, dict):
        return []

    passed = [str(p) for p in (checks.get("passed") or [])]
    hard_failures = [str(f) for f in (checks.get("hard_failures") or [])]
    soft_failures = [str(f) for f in (checks.get("soft_failures") or [])]
    repairs = [str(r) for r in (checks.get("repairs") or [])]
    # 兼容旧草稿：分级功能之前存的 result 只有扁平的 "failures"，一律当 hard 显示
    if not hard_failures and not soft_failures and checks.get("failures"):
        hard_failures = [str(f) for f in checks["failures"]]

    if not passed and not hard_failures and not soft_failures and not repairs:
        return []

    total = len(passed) + len(hard_failures) + len(soft_failures)
    lines: list[str] = []

    # ── 表头：一行说清过了几项、以及有没有被拦 ─────────────────────
    if checks.get("hard_ok", not hard_failures):
        lines.append(f":white_check_mark: *{t('seo_checks_ok', lang)}*  ({len(passed)}/{total})")
    else:
        lines.append(f":x: *{t('seo_checks_failed', lang)}*  ({len(hard_failures)}/{total})")

    # ── hard 先行：全部显示，一条不省（每条都是阻断原因）──────────
    lines.extend(f":x: {f}" for f in hard_failures)
    # ── soft 次之：显示但明确标成"仅提醒" ─────────────────────────
    lines.extend(f":warning: {f}" for f in soft_failures)
    # ── 自动修复：让人知道代码在这里动过手（不说的话就是黑箱）────
    lines.extend(f":wrench: {r}" for r in repairs)

    # ── 绿勾：截断显示（信息价值递减，且 Slack section 有 3000 字符上限）──
    _MAX_PASSED_SHOWN = 6
    lines.extend(f":white_check_mark: {p}" for p in passed[:_MAX_PASSED_SHOWN])
    if len(passed) > _MAX_PASSED_SHOWN:
        lines.append(f"_{t('seo_checks_more', lang, n=len(passed) - _MAX_PASSED_SHOWN)}_")

    text = "\n".join(lines)
    # Slack 的 mrkdwn section 硬上限 3000 字符，超了整条消息会被拒（API 报 invalid_blocks）。
    # 宁可截断也不能让卡片发不出去 —— 完整信息在日志里有。
    if len(text) > 2900:
        text = text[:2900] + "\n_(truncated — full list is in the logs)_"

    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _build_channel_atoms_blocks(result: dict, lang: str = "en") -> list[dict]:
    """把派生出的渠道原子渲成卡片（auto / chat 两张卡共用）。

    信息设计上要传达的一件事：**这些是同一篇文章的投影，不是另外写的东西**。
    所以放在文章信息之后、用同一张卡片承载，而不是各发一条消息。

    每条原子带自己的状态：
      :white_check_mark:  通过全部校验，可以用
      :x:                 被 hard 校验拦下（比如编了个价格）→ 这条不可用，
                          **但文章本身不受影响**（派生是附加产物）
    容错：没有这个键就返回空列表 —— 没配 channels 的租户、以及这个功能上线前
    存的旧草稿，都走这一支。
    """
    atoms = result.get("channel_atoms")
    if not isinstance(atoms, list) or not atoms:
        return []

    lines: list[str] = []
    total_cost = 0.0
    for a in atoms:
        if not isinstance(a, dict):
            continue
        total_cost += float(a.get("cost") or 0)
        icon = ":x:" if a.get("blocked") else ":white_check_mark:"
        label = a.get("label") or a.get("channel") or "channel"
        lines.append(f"{icon} *{label}*")
        if a.get("blocked"):
            # 被拦的要说清为什么 —— 否则用户只看到"少了一条"，不知道发生了什么
            for f in (a.get("hard_failures") or [])[:2]:
                lines.append(f"    :x: {f}")
            lines.append("    _(article itself is unaffected — this atom was dropped)_")
        else:
            for fname, val in (a.get("content") or {}).items():
                text = str(val)
                if len(text) > 220:
                    text = text[:220] + "…"
                lines.append(f"    `{fname}` {text}")
            for f in (a.get("soft_failures") or [])[:1]:
                lines.append(f"    :warning: {f}")

    if not lines:
        return []

    header = f":recycle: *{t('channel_atoms', lang)}*  (${total_cost:.4f})"
    text = header + "\n" + "\n".join(lines)
    # Slack section 硬上限 3000 字符，超了整条消息发不出去
    if len(text) > 2900:
        text = text[:2900] + "\n_(truncated — full content is in the logs)_"
    return [{"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def build_blog_result_blocks(
    result: dict,
    index: int = 1,
    total: int = 1,
) -> list[dict]:
    """构建单篇博客生成结果的 Slack Block Kit 消息"""
    if not result.get("success"):
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":x: *Blog #{index}/{total} — Generation Failed*\n\n"
                            f"Error: {result.get('error', 'Unknown error')}",
                },
            },
            {"type": "divider"},
        ]

    title = result.get("title", "Untitled")
    preview_url = result.get("preview_url", "")
    score = result.get("review_score", 0)
    rounds = result.get("review_rounds", 0)
    usage = result.get("usage_report", "")
    blog_data = result.get("blog_data", {})
    excerpt = blog_data.get("excerpt", "")
    tags = blog_data.get("tags", [])
    template_name = result.get("template_name", "N/A")
    layout_label = result.get("layout_label", "N/A")
    gen_time = result.get("generation_time", "N/A")
    wp_published = result.get("wp_published", False)
    wp_post_url = result.get("wp_post_url", "")
    wp_edit_url = result.get("wp_edit_url", "")

    # 标签格式化
    tags_text = "  ".join(f"`{t}`" for t in tags[:5]) if tags else "N/A"

    # 评分 emoji
    if score >= 90:
        score_emoji = ":star2:"
    elif score >= 80:
        score_emoji = ":white_check_mark:"
    else:
        score_emoji = ":warning:"

    blocks = [
        # ── 标题 ──
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":notebook_with_decorative_cover: Blog Post {index}/{total}",
            },
        },
        # ── 博客标题 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{title}*",
            },
        },
        # ── 摘要 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"_{excerpt}_",
            },
        },
        # ── SEO 关键词信息 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":mag: *Primary Keyword:*  {blog_data.get('seo_slug', 'N/A').replace('-', ' ')}\n"
                    f":link: *SEO Slug:*  `{blog_data.get('seo_slug', 'N/A')}`"
                ),
            },
        },
        {"type": "divider"},
        # ── 详细信息（纵向单列） ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{score_emoji} *Review Score:*  {score}/100  (round {rounds})\n"
                    f":label: *Tags:*  {tags_text}\n"
                    f":art: *Template:*  {template_name}\n"
                    f":page_facing_up: *Layout:*  {layout_label}\n"
                    f":stopwatch: *Generation Time:*  {gen_time}"
                ),
            },
        },
    ]

    # ── SEO 硬性检查（确定性校验层的产物）────────────────────────
    # 放在"审核评分"下面、预览链接上面：先说这稿子客观上合不合规，
    # 再让人点进去看。没有这个字段时 extend 一个空列表，卡片布局不变。
    blocks.extend(_build_seo_checks_blocks(result))
    # 渠道原子放在 SEO 检查之后：先确认文章本身没问题，再看它派生出了什么
    blocks.extend(_build_channel_atoms_blocks(result))

    blocks += [
        {"type": "divider"},
        # ── 预览链接（醒目） ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":eyes:  *<{preview_url}|:point_right: Open Preview in Browser>*",
            },
        },
    ]

    # ── WordPress 发布状态 / 发布按钮 ──
    if wp_published and wp_post_url:
        # 已发布 — 显示链接
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":outbox_tray: *WordPress:*  Published as `private`\n"
                    f":link: *Post:*  <{wp_post_url}|View on WordPress>\n"
                    f":pencil2: *Edit:*  <{wp_edit_url}|Open in WP Admin>"
                ),
            },
        })
    elif not wp_published and result.get("blog_data"):
        # 未发布 — 显示 Publish 按钮
        # action_id 里编码 merchant_id 和 session_id，按钮点击时用来找到对应草稿
        session_id = result.get("session_id", "")
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":outbox_tray: Publish to WordPress"},
                    "style": "primary",
                    "action_id": f"wp_publish_{session_id}",
                },
            ],
        })

    # ── 用量信息 ──
    if usage:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":bar_chart: *Cost Breakdown*\n{usage}",
            },
        })

    blocks.append({"type": "divider"})
    return blocks


def build_batch_summary_blocks(results: list[dict], merchant_name: str) -> list[dict]:
    """构建批量生成结果的汇总消息"""
    success_count = sum(1 for r in results if r.get("success"))
    total_count = len(results)

    # 汇总 emoji
    if success_count == total_count:
        status_emoji = ":tada:"
    elif success_count > 0:
        status_emoji = ":white_check_mark:"
    else:
        status_emoji = ":x:"

    # 收集成功文章的标题
    titles = [r.get("title", "Untitled") for r in results if r.get("success")]
    titles_text = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(titles))

    # 收集总用量
    total_cost = 0.0
    for r in results:
        usage_text = r.get("usage_report", "")
        # 从 usage_report 里提取 total cost（简单解析）
        if "Total Cost:" in usage_text:
            try:
                cost_str = usage_text.split("Total Cost: $")[1].split("*")[0].strip()
                total_cost += float(cost_str)
            except (IndexError, ValueError):
                pass

    summary_lines = [
        f":memo: Generated *{success_count}/{total_count}* blog post{'s' if total_count > 1 else ''} successfully.",
    ]
    if titles_text:
        summary_lines.append(f"\n:page_facing_up: *Articles:*\n{titles_text}")
    # 总耗时
    gen_times = [r.get("generation_time", "") for r in results if r.get("generation_time")]
    if gen_times:
        summary_lines.append(f"\n:stopwatch: *Generation Time:* {', '.join(gen_times)}")
    if total_cost > 0:
        summary_lines.append(f":moneybag: *Total Cost:* ${total_cost:.4f}")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{status_emoji} Generation Complete — {merchant_name}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(summary_lines),
            },
        },
        {"type": "divider"},
    ]

    for i, result in enumerate(results, 1):
        blocks.extend(build_blog_result_blocks(result, index=i, total=total_count))

    return blocks


def build_schedule_status_blocks(
    merchant_name: str,
    is_active: bool,
    times: list[str],
    recent_drafts: list[dict],
) -> list[dict]:
    """构建调度状态消息

    Args:
        merchant_name: 商家名称
        is_active: 调度是否激活
        times: 当前调度时间点列表
        recent_drafts: 最近的草稿列表

    Returns:
        Slack Block Kit blocks 列表
    """
    status_emoji = ":white_check_mark:" if is_active else ":no_entry_sign:"
    status_text = "Active" if is_active else "Inactive"
    times_text = ", ".join(times) if times else "None"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Schedule Status — {merchant_name}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{status_emoji} *Status:* {status_text}\n"
                    f":clock3: *Schedule:* {times_text}\n"
                    f":newspaper: *Posts per day:* {len(times)}"
                ),
            },
        },
    ]

    if recent_drafts:
        blocks.append({"type": "divider"})
        draft_lines = []
        for d in recent_drafts[:5]:
            title = d.get("title", "Untitled")
            score = d.get("review_score", 0)
            ts = d.get("created_at", 0)
            time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "N/A"
            draft_lines.append(f"• {title} (score: {score}) — {time_str}")

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Recent Drafts:*\n" + "\n".join(draft_lines),
            },
        })

    return blocks


def build_generating_message(merchant_name: str, count: int) -> list[dict]:
    """构建 "正在生成" 的初始消息"""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":rocket: *Starting blog generation for {merchant_name}*\n"
                    f"Generating {count} post{'s' if count > 1 else ''}. "
                    f"I'll update progress in real-time below.\n\n"
                    f":hourglass_flowing_sand: Preparing..."
                ),
            },
        },
    ]


# ── Chat 模式进度阶段 ──────────────────────────────────────
CHAT_PROGRESS_STAGES = [
    {"key": "template",  "emoji": ":art:",                   "i18n_key": "stage_template"},
    {"key": "web",       "emoji": ":globe_with_meridians:",  "i18n_key": "stage_web"},
    {"key": "write",     "emoji": ":pencil:",                "i18n_key": "stage_write"},
    {"key": "review",    "emoji": ":eyes:",                  "i18n_key": "stage_review"},
    {"key": "rewrite",   "emoji": ":memo:",                  "i18n_key": "stage_rewrite"},
    {"key": "image",     "emoji": ":camera:",                "i18n_key": "stage_image"},
    {"key": "render",    "emoji": ":package:",               "i18n_key": "stage_render"},
    {"key": "done",      "emoji": ":white_check_mark:",      "i18n_key": "stage_done"},
]

_CHAT_STAGE_INDEX = {s["key"]: i for i, s in enumerate(CHAT_PROGRESS_STAGES)}


def build_chat_progress_blocks(
    store_name: str,
    current_stage: str,
    extra_info: str = "",
    lang: str = "en",
) -> list[dict]:
    """构建 Chat 模式的实时进度消息（单条消息动态更新）"""
    current_idx = _CHAT_STAGE_INDEX.get(current_stage, 0)

    lines = []
    for i, stage in enumerate(CHAT_PROGRESS_STAGES):
        if stage["key"] == "rewrite" and current_stage != "rewrite":
            continue

        label = t(stage["i18n_key"], lang)
        if i < current_idx:
            lines.append(f":white_check_mark:  {label}")
        elif i == current_idx:
            lines.append(f"{stage['emoji']}  *{label}...*")
        else:
            lines.append(f":white_circle:  {label}")

    progress_text = "\n".join(lines)
    extra_line = f"\n\n:bulb: _{extra_info}_" if extra_info else ""
    header = t("generating_for", lang, name=store_name)

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":rocket: *{header}*\n\n{progress_text}{extra_line}",
            },
        },
    ]


# ── Auto 模式进度阶段 ─────────────────────────────────────
PROGRESS_STAGES = [
    {"key": "scrape",    "emoji": ":mag:",                   "i18n_key": "stage_scrape"},
    {"key": "research",  "emoji": ":brain:",                 "i18n_key": "stage_research"},
    {"key": "template",  "emoji": ":art:",                   "i18n_key": "stage_template"},
    {"key": "web",       "emoji": ":globe_with_meridians:",  "i18n_key": "stage_web"},
    {"key": "write",     "emoji": ":pencil:",                "i18n_key": "stage_write"},
    {"key": "review",    "emoji": ":eyes:",                  "i18n_key": "stage_review"},
    {"key": "rewrite",   "emoji": ":memo:",                  "i18n_key": "stage_rewrite"},
    {"key": "artist",    "emoji": ":lower_left_paintbrush:", "i18n_key": "stage_artist"},
    {"key": "image",     "emoji": ":camera:",                "i18n_key": "stage_image_gen"},
    {"key": "render",    "emoji": ":package:",               "i18n_key": "stage_render_html"},
    {"key": "publish",   "emoji": ":outbox_tray:",           "i18n_key": "stage_publish"},
    {"key": "done",      "emoji": ":white_check_mark:",      "i18n_key": "stage_done"},
]

_STAGE_INDEX = {s["key"]: i for i, s in enumerate(PROGRESS_STAGES)}


def build_progress_blocks(
    merchant_name: str,
    current_stage: str,
    post_index: int = 1,
    post_total: int = 1,
    extra_info: str = "",
    auto_publish: bool = True,
    lang: str = "en",
) -> list[dict]:
    """构建实时进度更新消息"""
    current_idx = _STAGE_INDEX.get(current_stage, 0)

    lines = []
    for i, stage in enumerate(PROGRESS_STAGES):
        if stage["key"] == "rewrite" and current_stage != "rewrite":
            continue
        if stage["key"] == "publish" and not auto_publish:
            continue

        label = t(stage["i18n_key"], lang)
        if i < current_idx:
            lines.append(f":white_check_mark:  {label}")
        elif i == current_idx:
            lines.append(f"{stage['emoji']}  *{label}...*")
        else:
            lines.append(f":white_circle:  {label}")

    progress_text = "\n".join(lines)
    gen_text = t("generating_for", lang, name=merchant_name)

    header = (
        f":rocket: *{gen_text}*"
        if post_total == 1
        else f":rocket: *{gen_text} ({post_index}/{post_total})*"
    )

    extra_line = f"\n\n:bulb: _{extra_info}_" if extra_info else ""

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{header}\n\n{progress_text}{extra_line}",
            },
        },
    ]


# ── Chat 对话模式专用消息构建 ────────────────────────────────


def build_chat_result_blocks(result: dict, lang: str = "en") -> list[dict]:
    """构建 Chat 对话模式的博客生成结果消息"""
    if not result.get("success"):
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":x: *{t('blog_failed', lang)}*\n\n"
                            f"Error: {result.get('error', 'Unknown error')}",
                },
            },
        ]

    title = result.get("title", "Untitled")
    preview_url = result.get("preview_url", "")
    score = result.get("review_score", 0)
    rounds = result.get("review_rounds", 0)
    usage = result.get("usage_report", "")
    blog_data = result.get("blog_data", {})
    excerpt = blog_data.get("excerpt", "")
    tags = blog_data.get("tags", [])
    template_name = result.get("template_name", "N/A")
    layout_label = result.get("layout_label", "N/A")
    gen_time = result.get("generation_time", "N/A")
    session_id = result.get("session_id", "")

    tags_text = "  ".join(f"`{tag}`" for tag in tags[:5]) if tags else "N/A"

    if score >= 90:
        score_emoji = ":star2:"
    elif score >= 80:
        score_emoji = ":white_check_mark:"
    else:
        score_emoji = ":warning:"

    image_count = len(result.get("image_paths", {}))
    img_unit = t("images_plural", lang) if image_count > 1 else t("images", lang)
    round_unit = t("rounds", lang) if rounds > 1 else t("round", lang)

    blocks = [
        # ── 标题 ──
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":sparkles: {t('blog_ready', lang)}",
            },
        },
        # ── 博客标题 + 摘要 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":memo: *{title}*\n\n_{excerpt}_",
            },
        },
        {"type": "divider"},
        # ── 审核评分 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{score_emoji} *{t('review_score', lang)}:*  *{score}/100*  ({rounds} {round_unit})",
            },
        },
        # ── 内容详情 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":label: *{t('tags', lang)}:*  {tags_text}\n"
                    # 这里原来是直接 `'图片' if lang == 'zh' else 'Images'` —— 绕过了 t()，
                    # 所以 UI 语言收口之后它会变成"漏网之鱼"（标签英文、这一项中文）。
                    # 改成走翻译表，语言决策只剩一个来源。
                    f":frame_with_picture: *{t('images_label', lang)}:*  {image_count} {img_unit}\n"
                    f":art: *{t('template', lang)}:*  {template_name}  |  :page_facing_up: *{t('layout', lang)}:*  {layout_label}\n"
                    f":stopwatch: *{t('time', lang)}:*  {gen_time}"
                ),
            },
        },
    ]

    # ── SEO 硬性检查（确定性校验层的产物）────────────────────────
    # 位置：评分 + 内容详情之后，成本之前。顺序 = 「质量 → 合规 → 花了多少钱」，
    # 和用户实际关心的顺序一致。
    blocks.extend(_build_seo_checks_blocks(result, lang=lang))
    blocks.extend(_build_channel_atoms_blocks(result, lang=lang))

    # ── 用量信息（紧跟详情） ──
    if usage:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":bar_chart: *{t('cost', lang)}:*  {usage}",
            },
        })

    blocks.append({"type": "divider"})

    # ── 预览 + 提示 + 按钮（底部一组） ──
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f":eyes:  *<{preview_url}|:point_right: {t('open_preview', lang)}>*",
        },
    })
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f":speech_balloon: *{t('reply_hint', lang)}*",
        },
    })
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": f":outbox_tray: {t('publish_wp', lang)}"},
                "style": "primary",
                "action_id": f"wp_publish_{session_id}",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": f":arrows_counterclockwise: {t('regenerate', lang)}"},
                "action_id": f"chat_regenerate_{session_id}",
            },
        ],
    })

    return blocks


def build_confirm_blocks(summary_text: str, lang: str = "en") -> list[dict]:
    """构建 Chat 模式的参数确认消息 — 用户确认后才开始生成"""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": summary_text,
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f":white_check_mark: {t('confirm_generate', lang)}"},
                    "style": "primary",
                    "action_id": "chat_confirm_generate",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f":pencil2: {t('confirm_edit', lang)}"},
                    "action_id": "chat_confirm_edit",
                },
            ],
        },
    ]
