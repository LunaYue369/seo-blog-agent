"""pipeline/preview_server.py 的 hero 底图选择测试 —— 零 API key、零网络。

## 为什么这几条值得写成测试
hero 底图的选择规则里有**三条肉眼很难复查的不变式**，而且它们都是"错了也不报错、
只是页面看着怪"的类型 —— 正是最需要测试盯着的那种：

  ① **hero 底图 ≠ 正文第一张图**（否则同一张照片在第一屏出现两次，像出了 bug）
  ② **只有一张图时不铺底图**（宁可回落品牌渐变，也不复用首图）
  ③ **没有图时产出空 style，绝不产出 `url('')`**
     —— `url('')` 是非法值，浏览器会把整条 background 声明丢掉，
     连兜底的品牌渐变一起没了，hero 会变成一块白。

## 做法：假图片 + tmp_path
用 tmp_path 造几个真实存在但内容是占位字节的 PNG 文件（选图逻辑只 stat 存在性，
不解析像素），所以测试不需要真图、不写用户目录、跑得飞快。
"""

import re
from pathlib import Path

import pytest

from pipeline.preview_server import render_blog_html

MCFG = {
    "merchant_id": "harbor_auto_spa",
    "store_name": "Harbor Auto Spa",
    "website": "https://harborautospa.example/",
    "brand_color_primary": "#1a1a2e",
    "brand_color_accent": "#e94560",
}

# 最小 PNG 文件头 —— 只要文件存在就够（选图逻辑不解码图片内容）
_FAKE_PNG = bytes.fromhex("89504e470d0a1a0a")


def _blog(slots: list[str]) -> dict:
    """造一份稿件，正文里按顺序放好各槽位的占位符。"""
    body = "<p>Opening paragraph.</p>"
    for s in slots:
        body += f"<!-- BLOG_IMAGE:{s} --><p>Section text for {s}.</p>"
    return {
        "title": "Hero Selection Test",
        "content_html": body,
        "excerpt": "x" * 155,
        "tags": ["a"],
        "seo_slug": "hero-selection-test",
        "image_alts": {s: f"alt for {s}" for s in slots},
    }


def _make_images(tmp_path: Path, slots: list[str]) -> dict[str, Path]:
    """在 tmp_path 里造出对应的假图片文件，返回 {槽位: 路径}。"""
    out = {}
    for s in slots:
        p = tmp_path / f"img_test_{s}.png"
        p.write_bytes(_FAKE_PNG)
        out[s] = p
    return out


def _render(tmp_path: Path, slots: list[str], template: str = "blog_template_minimal.html") -> str:
    """渲染一次，返回 HTML 字符串。图片和 HTML 都放在 tmp_path（模拟生产的同目录布局）。"""
    imgs = _make_images(tmp_path, slots)
    out = tmp_path / "preview.html"
    render_blog_html(
        blog_data=_blog(slots), image_paths=imgs, merchant_cfg=MCFG,
        output_path=out, template_file=template,
    )
    return out.read_text(encoding="utf-8")


def _hero_bg(html: str) -> str:
    """抽出 .hero-photo 那一层的 inline style（没有这个元素就返回 None）。"""
    m = re.search(r'class="hero-photo" style="([^"]*)"', html)
    return m.group(1) if m else None


def _body_image_order(html: str) -> list[str]:
    """按出现顺序抽出正文里的图片 alt —— 用来判断"正文第一张图"是哪一个。"""
    return re.findall(r'<div class="blog-image [^"]*"><img src="[^"]*" alt="([^"]*)"', html)


# ── 不变式①：hero 底图 ≠ 正文第一张图 ────────────────────────────────


def test_hero_bg_is_not_the_first_body_image(tmp_path):
    """★ 核心不变式 ★ 三张图时，hero 底图不能是正文开头那张。

    第一版就是直接拿 hero 槽位当底图，结果同一张照片在第一屏出现两次
    （banner 一张 + 正文第一张），看着像渲染出 bug 了。
    """
    html = _render(tmp_path, ["hero", "mid", "end"])
    style = _hero_bg(html)
    assert style, "三张图时应该铺上 hero 底图"
    assert "img_test_hero.png" not in style, "hero 底图不能用正文首图那张"
    # 规则是"从后往前挑"，所以应该落在最后一张
    assert "img_test_end.png" in style

    # 同时确认正文首图确实还是 hero（底图只是额外的装饰层，没动正文顺序）
    assert _body_image_order(html)[0] == "alt for hero"


def test_hero_bg_prefers_the_last_image(tmp_path):
    """从后往前挑：最后一张在文章底部，和顶部 banner 永远不会同屏出现。"""
    html = _render(tmp_path, ["img_1", "img_2", "img_3", "img_4"])
    style = _hero_bg(html)
    assert "img_test_img_4.png" in style
    assert "img_test_img_1.png" not in style


def test_two_images_uses_the_second_one(tmp_path):
    """两张图：首图排除，只剩第二张可用。"""
    html = _render(tmp_path, ["hero", "end"])
    style = _hero_bg(html)
    assert "img_test_end.png" in style
    assert "img_test_hero.png" not in style


# ── 不变式②：只有一张图时不铺底图 ────────────────────────────────────


def test_single_image_falls_back_to_gradient(tmp_path):
    """★ 刻意不退让 ★ 只有一张图时**不复用它**，回落品牌渐变。

    "那就复用首图吧"正是这次要修掉的问题 —— 宁可 hero 是一块干净的品牌渐变，
    也不要同一张照片在第一屏出现两次。
    """
    html = _render(tmp_path, ["hero"])
    assert _hero_bg(html) == "", "只有一张图时 hero 底图应为空（回落渐变）"
    # 兜底渐变必须还在，否则 hero 会变成一块白
    hero_bg_rule = re.search(r"\.hero-bg \{[^}]*\}", html).group(0)
    assert "linear-gradient" in hero_bg_rule
    # 那张图仍然正常出现在正文里
    assert _body_image_order(html) == ["alt for hero"]


# ── 不变式③：没有图时产出空 style，不能是 url('') ─────────────────────


def test_no_images_produces_empty_style_not_empty_url(tmp_path):
    """`url('')` 是非法值：浏览器会丢掉整条 background 声明，连兜底渐变一起没。"""
    out = tmp_path / "preview.html"
    render_blog_html(blog_data=_blog([]), image_paths={}, merchant_cfg=MCFG,
                     output_path=out, template_file="blog_template_minimal.html")
    html = out.read_text(encoding="utf-8")
    assert _hero_bg(html) == ""
    # 去掉注释再查 —— 模板注释里**故意**写了 url('') 当反例说明，
    # 不排除注释的话这条断言会被自己的文档骗过去（第一次就是这么误报的）
    stripped = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    stripped = re.sub(r"<!--.*?-->", "", stripped, flags=re.S)
    assert "url('')" not in stripped


def test_missing_image_file_is_ignored(tmp_path):
    """image_paths 里给了路径但文件不存在（生图失败）→ 当它不存在，不能崩。"""
    imgs = _make_images(tmp_path, ["hero", "end"])
    imgs["mid"] = tmp_path / "never_created.png"        # 故意不创建
    out = tmp_path / "preview.html"
    render_blog_html(blog_data=_blog(["hero", "mid", "end"]), image_paths=imgs,
                     merchant_cfg=MCFG, output_path=out,
                     template_file="blog_template_minimal.html")
    html = out.read_text(encoding="utf-8")
    assert "never_created.png" not in html
    assert "img_test_end.png" in _hero_bg(html)
    assert "BLOG_IMAGE" not in html, "缺图的占位符必须被清掉，不能裸露在正文里"


# ── 三套模板都要接上 ──────────────────────────────────────────────────


@pytest.mark.parametrize("template", [
    "blog_template.html",
    "blog_template_minimal.html",
    "blog_template_magazine.html",
])
def test_all_three_templates_wire_the_hero_photo(tmp_path, template):
    """三套模板都必须有 .hero-photo 这一层并接上 token。

    防的是"改了两套忘了第三套" —— 那种漏改不会报错，只是某个模板的 hero
    悄悄还是纯渐变，而模板是随机轮换的，人工复查很容易碰不到那一套。
    """
    html = _render(tmp_path, ["hero", "mid", "end"], template=template)
    assert 'class="hero-photo"' in html, f"{template} 没有照片层"
    assert "img_test_end.png" in _hero_bg(html), f"{template} 没接上 token"
    assert "{{HERO_BG_STYLE}}" not in html, f"{template} 的 token 没被替换"
