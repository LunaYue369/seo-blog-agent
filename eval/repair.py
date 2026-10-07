"""确定性修复层 —— 能用代码修好的，绝不花钱让模型重写一遍

## 这个模块的由来（一个实测出来的教训）
2026-07-27 跑真链路：摘要长度连续三轮不合规（160 → 251 → 168），第二篇修了 prompt
之后收到 **161 字符 —— 就差 1 个**。为这 1 个字符触发了两次整篇重写，烧掉 $0.055。

结论不是"再加 prompt 压力"，而是：

    ⭐ **数字符是代码的活，不是模型的活。**
       我不让模型数字符 —— 让它写一句话，代码把它裁进区间。

同一个道理的推广：**校验层发现的问题，先问"这能不能确定性地修好"**。
能 → 当场修，零成本、结果确定、不打扰模型；
不能 → 才打回让模型重写。

## 判据是不对称的（这点很关键）
    超长 → **代码能修**：删字不需要新信息，裁到词边界即可 → 确定性修复
    过短 → **代码修不了**：补字需要新信息，代码变不出内容 → 只能打回问模型

所以本模块只做"减法"，不做"加法"。凭空生成内容那是 LLM 的活，硬塞会造出胡话。

## 边界（故意不做的事）
- **不改 content_html**。正文里的问题（内链太多、h2 太少）虽然理论上也能机械删改，
  但删哪一条内链是语义判断，代码删错了比不删更糟。那些已经降级成 soft，交给 reviewer。
- **不补标点、不加句号**。meta description 不要求以句号结尾，自动加反而可能改变语气。
- **修不动就老实不修**。裁到词边界会掉到下限以下时直接放弃 —— 半个词的摘要比超长更糟。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import config as cfg

log = logging.getLogger(__name__)

# 裁剪后需要从尾部去掉的悬空标点：在词边界切断后常留下 "…the fit," 这种尾巴
_DANGLING_TAIL = " ,;:—–-·/&|"


@dataclass
class RepairRecord:
    """一次修复的记录 —— 修了什么、从多少改到多少。

    field    被修的字段名（"excerpt"）
    before   修之前的值（长度用，不存全文，避免日志爆炸）
    after    修之后的值
    note     人话说明，会进日志、也会进 Slack 卡片（让人知道"这里代码动过手"）
    """

    field: str
    before_len: int
    after_len: int
    note: str


def repair_excerpt(blog_data: dict) -> RepairRecord | None:
    """摘要超长时，就地裁到区间内。返回修复记录；没动就返回 None。

    ★ 会**原地修改** blog_data["excerpt"] ★
    这跟 `check_seo` 的"绝不修改输入"是刻意相反的分工：
      - check_seo   = 只读的裁判（有测试断言它不修改入参）
      - repair_*    = 明确的手术刀，就地改
    两者都在 review_loop 里跑，而 review_loop 本来就持有并原地更新 blog_data，
    所以在这里改是符合那条既有契约的，不会造成"两个地方各改一份"。

    算法（每一步都可预测，没有任何随机性）：
      1. strip 首尾空白 —— 校验器数的是 strip 后的长度，这里必须口径一致
      2. 已在区间内 / 过短 → 不动（过短要补字，代码补不出来，见模块头）
      3. 超长 → 在 max_chars 以内找**最后一个空格**，在那里切断
      4. 去掉切断后留下的悬空标点
      5. 结果仍 ≥ min_chars 才采用；否则放弃（半个词的摘要比超长更糟）
    """
    lo, hi = cfg.EVAL_EXCERPT_MIN_CHARS, cfg.EVAL_EXCERPT_MAX_CHARS

    raw = blog_data.get("excerpt")
    if not isinstance(raw, str):
        return None

    # Step 1: 口径对齐 —— 和 check_seo 一样数 strip 之后的长度
    s = raw.strip()
    before = len(s)

    # Step 2: 不该修的两种情况直接返回
    if before <= hi:
        # 注意：过短（< lo）也走这一支。**代码不补字**，让它去 soft 失败里被报告。
        if s != raw:
            # 唯一的例外：只有首尾空白的差异 → 顺手规范化（零风险，且能避免
            # "校验器说 160、人肉数出 162"这种对不上账的困惑）
            blog_data["excerpt"] = s
            return RepairRecord("excerpt", before_len=len(raw), after_len=before,
                                note=f"Excerpt whitespace trimmed ({len(raw)} → {before} chars)")
        return None

    # Step 3: 在上限内找最后一个词边界
    window = s[:hi]                       # 中间产物：允许的最长片段
    cut = window.rfind(" ")               # 中间产物：最后一个空格的位置
    if cut <= 0:
        # 整段没有空格（极少见，比如被塞了一长串无空格文本）→ 不做词中间硬切
        log.warning("摘要超长 %d 字符但找不到词边界，放弃自动裁剪，交给模型重写", before)
        return None

    # Step 4: 去掉悬空标点
    trimmed = window[:cut].rstrip(_DANGLING_TAIL)

    # Step 5: 裁完不能掉出下限
    if len(trimmed) < lo:
        log.warning("摘要超长 %d 字符，但裁到词边界只剩 %d 字符（低于下限 %d），"
                    "放弃自动裁剪，交给模型重写", before, len(trimmed), lo)
        return None

    blog_data["excerpt"] = trimmed
    return RepairRecord(
        "excerpt", before_len=before, after_len=len(trimmed),
        note=(f"Excerpt was too long, auto-trimmed: {before} → {len(trimmed)} chars "
              f"(target {lo}-{hi}, no model call)"),
    )


def repair_all(blog_data: dict) -> list[RepairRecord]:
    """跑全部确定性修复，返回所有修复记录（没修任何东西就是空列表）。

    这是 review_loop 唯一需要调的入口 —— 以后加新的修复项只在这里加一行，
    调用方不用改。顺序无关（每个修复只碰自己那个字段）。
    """
    records = [repair_excerpt(blog_data)]
    return [r for r in records if r is not None]
