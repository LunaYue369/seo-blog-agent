"""模板与布局风格选择器 — 按商家加载偏好模板和布局

数据来源：
  - HTML 视觉模板：merchants/{id}/templates/*.html（每个商家独立目录）
  - 内容布局风格：merchants/{id}/layouts/*.md（每个 .md 文件就是一种布局）

每次生成博客时随机选一套模板 + 一种布局，注入 Copywriter prompt。
批量生成时，通过 exclude 参数保证连续两篇不重复。
"""

import logging
import random
from pathlib import Path

import config as cfg

log = logging.getLogger(__name__)

# ── 缓存 — 避免每次生成都重新读文件 ──────────────────────────
# {merchant_id: [{"name": "how-to-guide", "label": "实操教程型", "prompt": "..."}]}
_layout_cache: dict[str, list[dict]] = {}

# {merchant_id: [{"name": "经典白", "file": "blog_template.html"}, ...]}
_template_cache: dict[str, list[dict]] = {}

# ── 模板文件名 → 默认显示名称（商家目录下无额外配置时使用）───
_TEMPLATE_NAMES: dict[str, str] = {
    "blog_template.html": "Classic",
    "blog_template_magazine.html": "Magazine",
    "blog_template_minimal.html": "Minimal",
}


def _load_layouts(merchant_id: str) -> list[dict]:
    """从商家 layouts/ 目录加载所有布局风格

    每个 .md 文件就是一种布局，文件名去掉 .md 就是 layout name。
    文件第一行 `# 标题` 会被提取为 label（中文显示名）。
    整个文件内容作为 prompt_injection 注入给 Copywriter。

    Args:
        merchant_id: 商家标识

    Returns:
        [{name, label, prompt}, ...]
    """
    # 先查缓存
    if merchant_id in _layout_cache:
        return _layout_cache[merchant_id]

    layouts_dir = cfg.MERCHANTS_DIR / merchant_id / "layouts"
    if not layouts_dir.is_dir():
        log.warning("[%s] 布局目录不存在: %s", merchant_id, layouts_dir)
        return []

    layouts = []
    for md_file in sorted(layouts_dir.glob("*.md")):
        content = md_file.read_text(encoding="utf-8").strip()
        if not content:
            continue

        name = md_file.stem  # 文件名去掉 .md，如 "how-to-guide"

        # 从第一行提取中文 label（格式: "# 实操教程型 (How-To Guide)"）
        first_line = content.split("\n")[0].strip()
        if first_line.startswith("# "):
            label = first_line[2:].strip()
        else:
            label = name  # fallback：用文件名

        layouts.append({
            "name": name,
            "label": label,
            "prompt": content,
        })
        log.info("[%s] 已加载布局: %s (%s)", merchant_id, label, name)

    # 写入缓存
    _layout_cache[merchant_id] = layouts
    log.info("[%s] 布局加载完成 — 共 %d 种", merchant_id, len(layouts))
    return layouts


def _load_merchant_templates(merchant_id: str) -> list[dict]:
    """从商家 templates/ 目录扫描可用的 HTML 模板文件

    Args:
        merchant_id: 商家标识

    Returns:
        [{name, file}, ...]
    """
    if merchant_id in _template_cache:
        return _template_cache[merchant_id]

    templates_dir = cfg.MERCHANTS_DIR / merchant_id / "templates"
    if not templates_dir.is_dir():
        log.warning("[%s] 模板目录不存在: %s", merchant_id, templates_dir)
        return []

    templates = []
    for html_file in sorted(templates_dir.glob("*.html")):
        filename = html_file.name
        name = _TEMPLATE_NAMES.get(filename, filename.replace(".html", "").replace("_", " "))
        templates.append({"name": name, "file": filename})
        log.info("[%s] 已加载模板: %s (%s)", merchant_id, name, filename)

    _template_cache[merchant_id] = templates
    log.info("[%s] 模板加载完成 — 共 %d 套", merchant_id, len(templates))
    return templates


def pick_template_and_layout(
    merchant_id: str,
    merchant_cfg: dict | None = None,
    exclude_template: str | None = None,
    exclude_layout: str | None = None,
) -> dict:
    """随机选择一套 HTML 模板和一种内容布局风格

    模板和布局都按商家维度加载：
    - 模板来自 merchants/{id}/templates/*.html
    - 布局来自 merchants/{id}/layouts/*.md

    Args:
        merchant_id: 商家标识
        merchant_cfg: 商家配置字典（不再需要 preferred_templates）
        exclude_template: 上一篇用的模板文件名，避免连续两篇相同
        exclude_layout: 上一篇用的布局名称，避免连续两篇相同

    Returns:
        {
            "template_file": "blog_template_magazine.html",
            "template_name": "杂志分栏",
            "layout_name": "comparison-review",
            "layout_label": "对比评测型 (Comparison & Review)",
            "layout_prompt": "..."
        }
    """
    # ── 获取该商家所有可用的 HTML 模板（从商家 templates/ 目录扫描）──
    all_templates = _load_merchant_templates(merchant_id)
    if not all_templates:
        log.warning("[%s] 没有模板文件，将使用默认模板", merchant_id)
        all_templates = [{"name": "经典白", "file": "blog_template.html"}]

    # 排除上一篇用过的模板（避免连续重复）
    available_templates = [
        t for t in all_templates if t["file"] != exclude_template
    ] or all_templates
    chosen_template = random.choice(available_templates)

    # ── 获取该商家的布局风格列表 ──
    all_layouts = _load_layouts(merchant_id)
    if not all_layouts:
        # 没有布局文件 — 返回空 prompt（copywriter 自由发挥）
        log.warning("[%s] 没有布局文件，Copywriter 将自由发挥", merchant_id)
        return {
            "template_file": chosen_template["file"],
            "template_name": chosen_template["name"],
            "layout_name": "",
            "layout_label": "自由格式",
            "layout_prompt": "",
        }

    # 排除上一篇用过的布局（避免连续重复）
    available_layouts = [
        l for l in all_layouts if l["name"] != exclude_layout
    ] or all_layouts
    chosen_layout = random.choice(available_layouts)

    result = {
        "template_file": chosen_template["file"],
        "template_name": chosen_template["name"],
        "layout_name": chosen_layout["name"],
        "layout_label": chosen_layout["label"],
        "layout_prompt": chosen_layout["prompt"],
    }

    log.info(
        "[%s] 选择模板: %s (%s) | 布局: %s (%s)",
        merchant_id,
        chosen_template["name"], chosen_template["file"],
        chosen_layout["label"], chosen_layout["name"],
    )

    return result


def get_template_path(template_file: str, merchant_id: str | None = None) -> Path:
    """获取模板文件的完整路径（优先从商家目录加载）

    Args:
        template_file: 模板文件名（如 "blog_template_magazine.html"）
        merchant_id: 商家标识；为 None 时回退到共享 templates/ 目录

    Returns:
        完整路径（如果文件不存在，逐级回退）
    """
    # 优先从商家目录加载
    if merchant_id:
        merchant_path = cfg.MERCHANTS_DIR / merchant_id / "templates" / template_file
        if merchant_path.exists():
            return merchant_path

    # 回退到共享 templates/ 目录
    shared_path = cfg.TEMPLATES_DIR / template_file
    if shared_path.exists():
        log.warning("[%s] 商家模板不存在，回退到共享模板: %s", merchant_id, template_file)
        return shared_path

    # 最终回退到默认模板
    log.warning("模板文件不存在: %s，回退到默认模板", template_file)
    default = cfg.MERCHANTS_DIR / (merchant_id or "") / "templates" / "blog_template.html"
    if default.exists():
        return default
    return cfg.TEMPLATES_DIR / "blog_template.html"


