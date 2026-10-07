"""eval/repair.py 的测试 —— 确定性修复层，零 API key、零网络。

## 这一层存在的理由（一次实测教训的固化）
2026-07-27 跑真链路：摘要连续三轮不合规（160 → 251 → 168），改了 prompt 之后收到
**161 字符 —— 就差 1 个**。为这 1 个字符触发了两次整篇重写，烧掉 $0.055。

    ⭐ 数字符是代码的活，不是模型的活。

所以这些测试盯的核心不变式有三条：
  ① 超长 → 一定被裁进区间（不花钱）
  ② 过短 → 一定**不动**（补字需要新信息，代码编不出来）
  ③ 裁不干净（会掉到下限以下）→ 宁可不裁，交给模型
"""

import config as cfg
import pytest

from eval.repair import repair_all, repair_excerpt


def _blog(excerpt: str) -> dict:
    """只关心 excerpt 的最小稿件 —— 修复层不碰 content_html，所以不需要造正文。"""
    return {"excerpt": excerpt, "content_html": "<p>body</p>", "title": "T"}


def _text(n: int) -> str:
    """造一段恰好 n 个字符、**带正常空格**的英文（保证有词边界可切）。

    ⚠️ 尾部空格要换成句点：裁剪点可能正好落在空格上，而修复层和校验器数的都是
    strip() 之后的长度 —— 不处理的话 `_text(400)` 实际是 399 个字符，
    测试会因为一个空格而红，而被测代码其实是对的（这个坑踩过两次）。
    """
    base = "Ceramic coating protects the paint and keeps the finish glossy for years on end. "
    s = (base * 6)[:n]
    return s[:-1] + "." if s.endswith(" ") else s


# ── 不变式①：超长 → 裁进区间 ──────────────────────────────────────


@pytest.mark.parametrize("length", [161, 175, 240, 400])
def test_over_long_excerpt_is_trimmed_into_range(length):
    blog = _blog(_text(length))
    rec = repair_excerpt(blog)

    assert rec is not None, "超长就该被修"
    after = len(blog["excerpt"])
    assert cfg.EVAL_EXCERPT_MIN_CHARS <= after <= cfg.EVAL_EXCERPT_MAX_CHARS, \
        f"裁完应落在区间内，实际 {after}"
    assert rec.before_len == length
    assert rec.after_len == after
    assert str(length) in rec.note and str(after) in rec.note   # 记录里要能看出改了什么


def test_trim_happens_at_a_word_boundary():
    """裁剪必须切在词边界 —— 半个单词的摘要比超长更糟（用户会看到 "...prote"）。"""
    blog = _blog(_text(200))
    repair_excerpt(blog)
    tail = blog["excerpt"].split()[-1]
    # 裁完的最后一个词必须是完整单词：它应该出现在原文的词表里
    assert tail in _text(200).split(), f"尾词 {tail!r} 被切断了"


def test_dangling_punctuation_is_cleaned():
    """在逗号后切断会留下 "…the fit," 这种尾巴，要去掉。"""
    long_excerpt = ("Our ceramic coating keeps your paint glossy, protects against UV damage, "
                    "and resists water spots, swirl marks, and light scratches for years, "
                    "which is why owners in Lakeview keep coming back, again and again, truly.")
    blog = _blog(long_excerpt)
    rec = repair_excerpt(blog)
    assert rec is not None
    assert not blog["excerpt"].endswith((",", ";", ":", "-", " "))


# ── 不变式②：过短 → 绝不动 ────────────────────────────────────────


@pytest.mark.parametrize("length", [0, 20, 100, 149])
def test_short_excerpt_is_never_touched(length):
    """★ 关键不变式 ★ 补字需要新信息，代码变不出内容 —— 只能打回问模型。

    如果哪天有人给这一层加了"太短就补点通用句子"的逻辑，这条测试会红 ——
    那种"补"出来的内容是胡话，比空着更糟。
    """
    original = _text(length)
    blog = _blog(original)
    rec = repair_excerpt(blog)
    assert rec is None
    assert blog["excerpt"] == original


def test_in_range_excerpt_is_untouched():
    original = _text(155)
    blog = _blog(original)
    assert repair_excerpt(blog) is None
    assert blog["excerpt"] == original


# ── 不变式③：裁不干净就不裁 ──────────────────────────────────────


def test_gives_up_when_trimming_would_fall_below_minimum():
    """一个超长但几乎没有词边界的摘要：切在唯一的空格上会掉到下限以下 → 放弃。

    宁可留一个超长摘要（soft 失败，会被报告出来）也不要产出一个 30 字符的残句。
    """
    blog = _blog("x" * 140 + " " + "y" * 60)     # 200 字符，唯一空格在第 141 位
    assert repair_excerpt(blog) is None
    assert len(blog["excerpt"]) == 201


def test_gives_up_when_there_is_no_word_boundary():
    """整段没有空格（被塞了一长串无空格文本）→ 不做词中间硬切。"""
    blog = _blog("x" * 300)
    assert repair_excerpt(blog) is None
    assert len(blog["excerpt"]) == 300


# ── 其它 ──────────────────────────────────────────────────────────


def test_whitespace_only_difference_is_normalized():
    """首尾空白会让"校验器说 160、人肉数出 162"对不上账，顺手规范化掉（零风险）。"""
    inner = _text(155)
    blog = _blog(f"   {inner}   ")
    rec = repair_excerpt(blog)
    assert rec is not None
    assert blog["excerpt"] == inner


def test_non_string_excerpt_does_not_crash():
    """LLM 偶尔会给 None / dict —— 修复层不能因此炸掉整条 pipeline。"""
    for bad in (None, 123, {"a": 1}, []):
        blog = {"excerpt": bad}
        assert repair_excerpt(blog) is None
        assert blog["excerpt"] == bad


def test_repair_all_returns_records_and_is_idempotent():
    """repair_all 是 review_loop 唯一的入口；连跑两次第二次应该没得修（幂等）。"""
    blog = _blog(_text(300))
    first = repair_all(blog)
    assert len(first) == 1
    second = repair_all(blog)
    assert second == []
