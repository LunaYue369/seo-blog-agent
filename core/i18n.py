"""多语言支持 — 根据用户语言返回对应的 UI 文本

所有面向用户的 Slack 消息文本集中在此管理。
语言检测基于 Unicode 字符范围判断（中文字符 → zh，其他 → en）。

使用：
    from core.i18n import t, resolve_language

    # 更新会话语言（推荐）：双向切换 + 迟滞，不会被一句 "ok" 掀翻
    sess["language"] = resolve_language(sess.get("language", "en"), user_text)
    t("generating_for", sess["language"], name="Harbor Auto Spa")   # → "正在为 Harbor Auto Spa 生成博客"

    # detect_language 只做单条消息的瞬时判断，不要用它直接覆盖会话语言

## ⚠️ 2026-07-27 起：UI 语言由 config.SLACK_UI_LANG 统一钉死（默认 "en"）
t() 会**忽略**传进来的 lang 参数，一律返回 SLACK_UI_LANG 指定的语言。原因是展示一致性：
多个商家频道并排看时，不希望卡片一半中文一半英文。

为什么钉在 t() 里而不是去改每个调用点：
  · t() 是所有 UI 文案的**唯一出口** —— 钉在这里，卡片/按钮/进度/错误提示一次全覆盖，
    不会漏掉某个角落（几十个调用点逐个改必然漏，而且以后新增调用点又会漏回去）；
  · 调用点的 `lang=sess["language"]` 全部保持原样不用动 —— 把 SLACK_UI_LANG 改成空串
    就立刻退回"跟随用户语言"的旧行为，一行配置回滚，代码零改动。

**不受影响的两件事**（别搞混）：
  ① Bot 的对话回复语言 —— 由 conversation.py 的 prompt 控制，仍然跟随用户；
  ② session["language"] 仍在检测和记录（resolve_language 照常工作），只是 UI 不再消费它。
"""

import re

import config as cfg

# ── 翻译表 ─────────────────────────────────────────────────
_STRINGS = {
    # ── 进度阶段（Chat 模式） ──
    "stage_template":       {"en": "Selecting template & layout",       "zh": "选择模板和布局"},
    "stage_web":            {"en": "Researching competitor articles",    "zh": "调研竞品文章"},
    "stage_write":          {"en": "Writing blog content",              "zh": "撰写博客内容"},
    "stage_review":         {"en": "Reviewing content quality",         "zh": "审核内容质量"},
    "stage_rewrite":        {"en": "Revising based on feedback",        "zh": "根据反馈修改中"},
    "stage_image":          {"en": "Processing images",                 "zh": "处理图片"},
    "stage_render":         {"en": "Assembling final preview",          "zh": "组装最终预览"},
    "stage_done":           {"en": "Complete!",                         "zh": "完成！"},

    # ── 进度阶段（Auto 模式） ──
    "stage_scrape":         {"en": "Scraping trending keywords",        "zh": "抓取热门关键词"},
    "stage_research":       {"en": "Analyzing SEO opportunities",       "zh": "分析 SEO 机会"},
    "stage_artist":         {"en": "Enhancing image prompts",           "zh": "优化图片提示词"},
    "stage_image_gen":      {"en": "Generating images (Seedream)",      "zh": "生成图片 (Seedream)"},
    "stage_render_html":    {"en": "Assembling final HTML",             "zh": "组装最终 HTML"},
    "stage_publish":        {"en": "Publishing to WordPress",           "zh": "发布到 WordPress"},

    # ── 进度消息 ──
    "generating_for":       {"en": "Generating blog for {name}",        "zh": "正在为 {name} 生成博客"},

    # ── 结果卡片 ──
    "blog_ready":           {"en": "Blog Ready for Review",             "zh": "博客已生成，请审核"},
    "blog_failed":          {"en": "Blog Generation Failed",            "zh": "博客生成失败"},
    "review_score":         {"en": "Review Score",                      "zh": "审核评分"},
    # 确定性 SEO 校验（eval 第①层）在结果卡片上的文案。两处措辞是刻意的：
    #   ① 和上面那行 0/100 的**主观分**区分开 —— 这几条是可复现的客观指标；
    #   ② hard 说 "blocked"（真的阻断了出稿），soft 不另起标题、只在行首挂 :warning:，
    #      因为它没阻断任何东西。不区分的话用户会把"内链 6 条"当成和"图片碎了"一样严重。
    "seo_checks_ok":         {"en": "SEO checks passed",                  "zh": "SEO 检查通过"},
    "seo_checks_failed":     {"en": "SEO checks blocked publishing",      "zh": "SEO 硬性检查已阻断"},
    "seo_checks_more":       {"en": "and {n} more passed",                "zh": "另有 {n} 项通过"},
    # 渠道原子那一段的标题。措辞用 "Derived for" 而不是 "Also generated" ——
    # 要传达的是"同一篇文章的投影"，不是"另外又写了几篇东西"。
    "channel_atoms":         {"en": "Derived for other channels",         "zh": "派生的渠道内容"},
    "tags":                 {"en": "Tags",                              "zh": "标签"},
    "images":               {"en": "image",                             "zh": "张图片"},
    # 这条是 UI 语言收口时补的：blocks.py 里原来直接写 `'图片' if lang == 'zh' else 'Images'`，
    # 绕过了 t()，成了语言统一之后唯一的漏网之鱼。收进翻译表 = 语言决策只剩一个来源。
    "images_label":         {"en": "Images",                            "zh": "图片"},
    "images_plural":        {"en": "images",                            "zh": "张图片"},
    "template":             {"en": "Template",                          "zh": "模板"},
    "layout":               {"en": "Layout",                            "zh": "布局"},
    "time":                 {"en": "Time",                              "zh": "耗时"},
    "cost":                 {"en": "Cost",                              "zh": "费用"},
    "open_preview":         {"en": "Open Preview in Browser",           "zh": "在浏览器中预览"},
    "reply_hint":           {"en": "Reply in this thread to request changes, or use the buttons below.", "zh": "在此对话中回复修改意见，或使用下方按钮操作。"},
    "publish_wp":           {"en": "Publish to WordPress",              "zh": "发布到 WordPress"},
    "regenerate":           {"en": "Regenerate",                        "zh": "重新生成"},

    # ── 确认按钮 ──
    "confirm_generate":     {"en": "Confirm & Generate",                "zh": "确认并生成"},
    "confirm_edit":         {"en": "Let me adjust...",                  "zh": "我再调整一下..."},

    # ── 状态提示 ──
    "starting_generation":  {"en": "Starting blog generation, please wait...",        "zh": "开始生成博客，请稍候..."},
    "still_generating":     {"en": "Still generating, please wait...",                "zh": "还在生成中，请稍候..."},
    "confirm_or_adjust":    {"en": "Click *Confirm & Generate* to start, or tell me what to adjust.", "zh": "点击 *确认并生成* 开始，或告诉我需要调整的地方。"},
    "click_or_change":      {"en": "Click a button above, or tell me what you'd like to change.",     "zh": "点击上方按钮，或告诉我你想修改什么。"},
    "no_problem_adjust":    {"en": "No problem! Tell me what you'd like to change.",  "zh": "没问题！告诉我你想调整什么。"},
    "regenerating":         {"en": "Regenerating blog from scratch...",               "zh": "正在从头重新生成博客..."},
    "session_expired":      {"en": "Session expired. Please start a new conversation.", "zh": "会话已过期，请开始新的对话。"},
    "working_on_mods":      {"en": "Working on your modifications...",                "zh": "正在处理你的修改..."},
    "no_draft_to_modify":   {"en": "No existing draft to modify — generating a new blog instead.", "zh": "没有可修改的草稿，将生成新博客。"},
    "gen_failed":           {"en": "Blog generation failed: {error}\n\nPlease try again or adjust your request.", "zh": "博客生成失败：{error}\n\n请重试或调整你的要求。"},

    # ── 图片上传 ──
    "received_images":      {"en": "Received {count} image(s)",         "zh": "已收到 {count} 张图片"},
    "total_images":         {"en": "total: {total}",                    "zh": "共 {total} 张"},

    # ── 发布 ──
    "publishing":           {"en": "Publishing *{title}* to WordPress...",  "zh": "正在将 *{title}* 发布到 WordPress..."},
    "publish_failed":       {"en": "Publish failed:",                       "zh": "发布失败："},
    "publish_error":        {"en": "Publish error:",                        "zh": "发布出错："},

    # ── 其他 ──
    "round":                {"en": "round",                             "zh": "轮"},
    "rounds":               {"en": "rounds",                            "zh": "轮"},
}


def ui_lang(requested: str = "en") -> str:
    """UI 语言的**唯一决策点**。

    config.SLACK_UI_LANG 非空 → 用它（当前默认 "en"，即卡片一律英文）；
    为空 → 退回调用方传进来的会话语言（旧行为）。

    ⚠️ 在函数内部读 cfg 而不是模块顶层取快照 —— 顶层取值会在 load_dotenv 之前跑，
    读到的是默认值而不是 .env 里的配置（[feedback_python-dotenv-import-order] 那个坑）。

    暴露成公开函数是给 blocks.py 用的：那里有几处直接判 `lang == "zh"` 的分支
    （不走 t()），它们也必须用同一个决策结果，否则会出现"标签英文、旁边的字中文"。
    """
    return (cfg.SLACK_UI_LANG or "").strip() or requested


def t(key: str, lang: str = "en", **kwargs) -> str:
    """获取翻译文本

    Args:
        key:    翻译键名
        lang:   语言代码 ("en" 或 "zh")。⚠️ 当 config.SLACK_UI_LANG 非空时**这个参数被忽略**
                （见模块头）。调用点不用改，保留它是为了改配置就能一键回滚。
        kwargs: 格式化参数

    Returns:
        翻译后的字符串
    """
    lang = ui_lang(lang)          # ← UI 语言统一在这里收口
    entry = _STRINGS.get(key, {})
    text = entry.get(lang, entry.get("en", key))
    if kwargs:
        text = text.format(**kwargs)
    return text


# ── 中文字符范围 ──
_CJK_RE = re.compile(
    r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]'
)


def detect_language(text: str) -> str:
    """根据文本内容检测语言（单条消息的瞬时判断）

    简单策略：包含中文字符 → "zh"，否则 → "en"

    ⚠️ 不要直接用它去覆盖会话语言 —— 中文用户中途回一句 "ok" 就会被判成 en。
       更新会话语言请用 resolve_language()。

    Args:
        text: 用户消息文本

    Returns:
        "zh" 或 "en"
    """
    if _CJK_RE.search(text or ""):
        return "zh"
    return "en"


# 判定「这条消息确实在用英文」所需的最少拉丁字母数
# 太低会让中文用户的 "ok" / "go" 把会话语言掀翻，太高则纯英文短句无法切回英文。
_EN_MIN_LATIN_CHARS = 8

_LATIN_RE = re.compile(r"[A-Za-z]")


def resolve_language(current: str, text: str) -> str:
    """根据新消息更新会话语言（双向，但带迟滞，避免被单词误伤）

    旧实现是**单向棘轮**：main.py / conversation.py 里写的都是
        if sess["language"] == "en" and detect_language(text) == "zh":
            sess["language"] = "zh"
    也就是 en→zh 能升级、zh→en 永远回不去。英文用户如果被一句中文
    （比如粘贴了一段中文资料、或同事插一句「不错」）带成 zh，
    之后整个 thread 的进度条/按钮/结果卡就永久变中文，只能重开会话。

    新规则（三档，按优先级）：
      1. 这条消息含中文字符          → "zh"（中文信号最强，立即切）
      2. 无中文，且拉丁字母 ≥ 8 个    → "en"（够长才认为真在说英文）
      3. 其它（纯表情/纯数字/"ok"）   → 保持 current 不变

    Args:
        current: 会话当前语言（"zh" / "en"；空值按 "en" 处理）
        text:    用户本条消息文本

    Returns:
        更新后的语言码

    示例:
        resolve_language("en", "帮我写一篇 PPF 的文章")     → "zh"
        resolve_language("zh", "ok")                       → "zh"   （太短，不切）
        resolve_language("zh", "make the title shorter")    → "en"   （够长，切回）
        resolve_language("zh", "👍")                        → "zh"
    """
    current = current or "en"
    if not text:
        return current
    if _CJK_RE.search(text):
        return "zh"
    if len(_LATIN_RE.findall(text)) >= _EN_MIN_LATIN_CHARS:
        return "en"
    return current
