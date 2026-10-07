"""博客预览服务 — 轻量 HTTP 静态文件服务 + HTML 渲染

功能：
1. 将博客数据 + 图片渲染为酷炫的 HTML 文件
2. 启动一个轻量 HTTP 服务供用户在浏览器中预览
3. 返回预览链接到 Slack
"""

import base64
import logging
import math
import os
import re
import threading
from datetime import date
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote

import config as cfg

log = logging.getLogger(__name__)

# 预览服务是否已启动
_server_started = False
_server_lock = threading.Lock()


def _image_to_data_uri(image_path: Path) -> str:
    """将本地图片转为 data URI，嵌入到 HTML 中（避免跨域问题）"""
    if not image_path or not image_path.exists():
        return ""
    suffix = image_path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/png")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def render_blog_html(
    blog_data: dict,
    image_paths: dict[str, Path],
    merchant_cfg: dict,
    output_path: Path,
    template_file: str = "",
) -> str:
    """将博客数据渲染为完整的 HTML 文件

    Args:
        blog_data: 博客内容 {title, content_html, excerpt, tags, seo_slug, image_prompts}
        image_paths: 图片路径 {"hero": Path, "mid": Path, "end": Path}
        merchant_cfg: 商家配置
        output_path: HTML 输出路径
        template_file: 指定使用的模板文件名（留空则用默认模板）

    Returns:
        预览 URL
    """
    title = blog_data.get("title", "Untitled Blog Post")
    content_html = blog_data.get("content_html", "")
    excerpt = blog_data.get("excerpt", "")
    tags = blog_data.get("tags", [])

    # 商家品牌配色
    primary_color = merchant_cfg.get("brand_color_primary", "#1a1a2e")
    accent_color = merchant_cfg.get("brand_color_accent", "#e94560")
    store_name = merchant_cfg.get("store_name", "Blog")
    website = merchant_cfg.get("website", "#")

    # 图片处理 — 遍历所有槽位（兼容 hero/mid/end 和 img_1/img_2/.../img_N）
    image_alts = blog_data.get("image_alts", {})

    for slot, img_path in image_paths.items():
        placeholder = f"<!-- BLOG_IMAGE:{slot} -->"
        if img_path and Path(img_path).exists():
            data_uri = _image_to_data_uri(Path(img_path))
            alt = image_alts.get(slot, f"{title} - {slot}")
            # 第一张图用 eager loading，其余用 lazy
            loading = "eager" if slot in ("hero", "img_1") else "lazy"
            css_class = f"blog-image {slot}-image"
            replacement = f'<div class="{css_class}"><img src="{data_uri}" alt="{alt}" loading="{loading}"></div>'
            # 只替换第一个占位符（防止 copywriter 重复放同一个占位符导致图片出现两次）
            content_html = content_html.replace(placeholder, replacement, 1)
            # 清掉同一 slot 的多余占位符
            content_html = content_html.replace(placeholder, "")
        else:
            content_html = content_html.replace(placeholder, "")

    # 清理未被替换的占位符（图片缺失时）
    content_html = re.sub(r"<!-- BLOG_IMAGE:\w+ -->", "", content_html)

    # ── hero 背景图 ────────────────────────────────────────────────────
    # 历史：这里曾经写死 `hero_uri = ""`（注释是"hero 背景改为品牌渐变"），于是
    # 三个模板的 hero 全是纯 CSS 渐变 —— 对深色品牌（primary 接近 #14161a 那种）
    # 就是一大块近黑，很闷。2026-07-27 重新接回真图，但解决了当初两个可能的痛点：
    #
    #   ① **标题看不清** → 模板侧加了一层 scrim（自下而上的黑色渐变遮罩），
    #      文字永远压在暗部上，不管配图明暗都可读。
    #   ② **页面变重** → ★这里用相对文件名而不是 data URI★。
    #      正文里的图已经内嵌了一份（约 1MB/张），hero 再内嵌同一张就是白白翻倍。
    #      改成引用同目录的 PNG 兄弟文件：字节增量≈0，浏览器可并行拉取、可缓存。
    #      代价是预览 HTML 不再"单文件自包含"（单独拷走 html 会丢 hero 底图，
    #      正文图仍在）—— 这个交换在本地预览和内网预览场景下都划算。
    #
    # 产物 hero_bg_style：要么是完整的 `background-image: url('xxx.png');`，
    # 要么是**空字符串**。空串是关键 —— 模板里那一层的 style 属性变成空的，
    # 于是只剩下面的品牌渐变兜底。绝不能产出 `url('')`：那是非法值，
    # 浏览器会把整条 background 声明丢掉，连兜底渐变一起没了。
    # ⭐ 不变式：**hero 底图 ≠ 正文第一张图**。
    # 第一版直接拿 hero/img_1 当底图，结果同一张照片在第一屏出现两次（banner 一张、
    # 正文开头又一张），看着像出 bug 了。所以选图规则分三步：
    hero_uri = ""            # 旧 token 保留不动（模板已不使用），避免动到其它租户
    hero_bg_style = ""

    # Step A: 找出「正文第一张图」是哪个槽位 —— 它是**要被排除**的那个。
    # 判定顺序和上面正文替换的顺序一致：hero / img_1 是约定的首图名，
    # 都没有的话就取 image_paths 的第一个键（dict 从 3.7 起保序）。
    _existing = [s for s, p in image_paths.items() if p and Path(p).exists()]
    _first_body_slot = next((s for s in ("hero", "img_1") if s in _existing),
                            _existing[0] if _existing else None)

    # Step B: 候选池 = 除首图之外的所有图，且**从后往前**挑。
    # 为什么优先最后一张：它在文章底部，和顶部 banner 在屏幕上永远不会同时出现，
    # 视觉重复的概率最低。（artist 人格里也写了末图偏"平静、有人味"，适合当门面。）
    _candidates = [s for s in reversed(_existing) if s != _first_body_slot]

    # Step C: 只有一张图时候选池是空的 → **不铺 hero 底图，回落品牌渐变**。
    # 刻意不退让成"那就复用首图吧"：那正是这次要修掉的问题。
    # 宁可 hero 是一块干净的品牌渐变，也不要同一张照片在第一屏出现两次。
    if not _candidates:
        if _existing:
            log.info("只有 %d 张配图且它就是正文首图 → hero 保持品牌渐变（避免同图重复）",
                     len(_existing))
    else:
        _slot = _candidates[0]
        try:
            # 相对 HTML 所在目录 —— 生产上两者都在 output/{merchant}/ 下，所以就是个文件名
            _rel = os.path.relpath(Path(image_paths[_slot]), output_path.parent).replace("\\", "/")
            hero_bg_style = f"background-image: url('{quote(_rel)}');"
            log.info("hero 底图用槽位 '%s'（正文首图是 '%s'，已排除）", _slot, _first_body_slot)
        except ValueError:
            # Windows 上跨盘符时 relpath 会抛 ValueError（比如图片在 D: 而 HTML 在 C:）。
            # 这种情况没有合法的相对路径可用 → 放弃 hero 底图，回落到品牌渐变。
            log.warning("hero 底图与 HTML 不在同一盘符，无法生成相对路径，回落品牌渐变")

    # 安全网：Copywriter 偶尔编造 <img src="/images/xxx.jpg"> 假标签，直接删掉
    # （真图已通过占位符嵌入，不需要重复）
    fake_img_pattern = re.compile(r'<img\s+[^>]*src="(/images/[^"]+)"[^>]*/?\s*>')
    content_html, fake_count = fake_img_pattern.subn("", content_html)
    if fake_count:
        log.warning("安全网触发：删除了 %d 个 Copywriter 编造的假 <img> 标签", fake_count)

    # 安全措施：移除 HTML 标题中的 emoji/特殊 Unicode 字符（GPT 偶尔仍会生成）
    def _strip_emoji_from_headings(html: str) -> str:
        """移除 <h2>/<h3> 标签内的 emoji 和特殊 Unicode 符号"""
        import re as _re
        # 匹配常见 emoji 范围（包括 supplementary 和组合字符）
        emoji_pattern = _re.compile(
            "[\U0001F300-\U0001F9FF"   # 杂项符号和表情
            "\U00002702-\U000027B0"     # 装饰符号
            "\U0000FE00-\U0000FE0F"     # 变体选择符
            "\U0000200D"                # 零宽连接符
            "\U000024C2-\U0001F251"     # 封闭字母数字补充
            "]+", flags=_re.UNICODE
        )
        def _clean_heading(m):
            tag = m.group(1)
            attrs = m.group(2)
            content = emoji_pattern.sub("", m.group(3)).strip()
            return f"<{tag}{attrs}>{content}</{tag}>"
        html = _re.sub(r"<(h[23])([^>]*)>(.*?)</\1>", _clean_heading, html)
        return html

    content_html = _strip_emoji_from_headings(content_html)

    # 生成标签 HTML
    tags_html = "".join(f'<span class="tag">{tag}</span>' for tag in tags)

    # 读取 HTML 模板 — 从商家 templates/ 目录加载
    merchant_id = merchant_cfg.get("merchant_id", "")
    if template_file:
        from services.template_selector import get_template_path
        template_path = get_template_path(template_file, merchant_id=merchant_id)
    else:
        # 兼容旧逻辑：未指定时用默认模板（优先商家目录）
        merchant_tpl = cfg.MERCHANTS_DIR / merchant_id / "templates" / "blog_template.html"
        template_path = merchant_tpl if merchant_tpl.exists() else cfg.TEMPLATES_DIR / "blog_template.html"

    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        log.warning("HTML 模板不存在: %s，使用内置模板", template_path)
        template = _FALLBACK_TEMPLATE

    # ── SEO 变量计算 ──
    seo_slug = blog_data.get("seo_slug", "blog-post")
    canonical_url = f"{website.rstrip('/')}/blog/{seo_slug}/"
    today = date.today()
    date_iso = today.isoformat()                      # 2026-03-31
    date_display = today.strftime("%B %d, %Y")        # March 31, 2026
    # 阅读时间：按英文 238 wpm 计算（取整，至少 1 分钟）
    word_count = len(re.sub(r"<[^>]+>", "", content_html).split())
    reading_time = str(max(1, math.ceil(word_count / 238)))

    # 填充模板变量
    html = template.replace("{{TITLE}}", title)
    html = html.replace("{{EXCERPT}}", excerpt)
    html = html.replace("{{CONTENT}}", content_html)
    html = html.replace("{{TAGS}}", tags_html)
    html = html.replace("{{HERO_IMAGE}}", hero_uri)
    # 新 token：hero 底图的 inline style。对**没用到这个 token 的旧模板**（其它两个租户）
    # 这一行是无害的 no-op —— str.replace 找不到目标就什么都不做，所以加 token 是纯增量改动。
    html = html.replace("{{HERO_BG_STYLE}}", hero_bg_style)
    html = html.replace("{{STORE_NAME}}", store_name)
    html = html.replace("{{WEBSITE}}", website)
    html = html.replace("{{PRIMARY_COLOR}}", primary_color)
    html = html.replace("{{ACCENT_COLOR}}", accent_color)
    # SEO 新增变量
    html = html.replace("{{CANONICAL_URL}}", canonical_url)
    html = html.replace("{{DATE_ISO}}", date_iso)
    html = html.replace("{{DATE_DISPLAY}}", date_display)
    html = html.replace("{{READING_TIME}}", reading_time)

    # 写入文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 构建预览 URL
    base_url = cfg.PREVIEW_BASE_URL.rstrip("/")
    # 路径格式: /merchant_id/filename.html
    merchant_id = merchant_cfg.get("merchant_id", "")
    relative_path = f"/{merchant_id}/{output_path.name}"
    preview_url = f"{base_url}{relative_path}"

    log.info("HTML 已保存: %s → %s", output_path, preview_url)
    return preview_url


def start_preview_server() -> None:
    """启动轻量 HTTP 静态文件预览服务（后台线程）只启动一次，后续调用自动跳过。
    每篇博客生成后就是一个独立的完整 HTML 文件，存在 output/{merchant_id}/ 下。
    打开效果就是一个完整的网页预览服务 http://localhost:8900/harbor_auto_spa/harbor_auto_spa_1711526400_a1b2.html
    """
    global _server_started

    with _server_lock:
        if _server_started:
            return
        _server_started = True

    host = cfg.PREVIEW_HOST
    port = cfg.PREVIEW_PORT
    serve_dir = str(cfg.OUTPUT_DIR)

    handler = partial(SimpleHTTPRequestHandler, directory=serve_dir)

    def _run():
        try:
            server = HTTPServer((host, port), handler)
            log.info("预览服务已启动: http://%s:%d (serving %s)", host, port, serve_dir)
            server.serve_forever()
        except Exception as exc:
            log.error("预览服务启动失败: %s", exc)

    thread = threading.Thread(target=_run, daemon=True, name="preview-server")
    thread.start()


# ── 内置 fallback 模板（仅在 templates/blog_template.html 不存在时使用）──
_FALLBACK_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{TITLE}} — {{STORE_NAME}}</title>
    <meta name="description" content="{{EXCERPT}}">
    <style>
        body { font-family: system-ui, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
        h1 { font-size: 2em; }
        img { max-width: 100%; border-radius: 8px; }
        .tag { background: #eee; padding: 4px 12px; border-radius: 20px; margin-right: 8px; font-size: 0.85em; }
    </style>
</head>
<body>
    <h1>{{TITLE}}</h1>
    <p><em>{{EXCERPT}}</em></p>
    <div>{{CONTENT}}</div>
    <div style="margin-top:20px">{{TAGS}}</div>
</body>
</html>"""
