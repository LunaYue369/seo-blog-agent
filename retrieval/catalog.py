"""商品目录检索 —— 关键词重叠打分取 top-N，注入 copywriter 的 prompt

## 为什么是关键词打分而不是向量检索
十几条结构化商品数据引入 embedding + 向量索引是**过度工程**：索引比数据还大，
多一个要同步的副本、多一个会静默失效的组件（embedding 模型换版本 → 索引全部失效，
而且不会报错，只会悄悄变差）。而这个场景的特征恰好都站在关键词一侧：
  · 规模小（十几到几百条）
  · 字段结构化（名称/面料/版型都是短文本）
  · 查询词和商品名**字面重合度高**（"t shirt size" ↔ "Classic Crew Neck Tee"）
  · 需要可解释（能说清"为什么选了这三条"，向量相似度说不清）
等 SKU 到几千条、且真的开始需要"显瘦的深色上衣"这种语义查询时再换 ——
那时候换也只动这一个文件，上层完全不用改（这就是把它单独成包的收益）。

## 两个产物，别混
    load_catalog(merchant_id)  → dict：整份目录（含 products 列表）。**校验层用它**
    build_catalog_brief(...)   → str：塞进 prompt 的文本块。**生成层用它**
同一份数据的两种形态：一个给代码核对，一个给模型阅读。
故意不合并成一个函数，因为校验层**绝不能**依赖"prompt 里放了什么"——
它要核对的是全量目录，而 prompt 里只有 top-N。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import config as cfg

log = logging.getLogger(__name__)

# 打分时忽略的词：纯功能词 + 在服装目录里**区分度为零**的词。
# "shirt" / "tee" 之所以也进这个表：几乎每个商品名里都有它，
# 留着的话所有商品的得分会被同时抬高，等于没有区分度（相当于一个常数项）。
_STOPWORDS = frozenset(
    "a an the of for to in on at and or but with your you my it that this is are do does"
    " how what why when which best guide men mens man shirt shirts tee tees".split()
)


def _tokens(text: str) -> set[str]:
    """把任意文本切成用于打分的词集合（小写、去标点、去停用词、去单字符）。"""
    words = re.split(r"[^A-Za-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 1 and w not in _STOPWORDS}


def load_catalog(merchant_id: str) -> dict[str, Any]:
    """读 `merchants/{merchant_id}/catalog.json`。没有这个文件就返回空 dict。

    **返回空 dict 而不是抛异常**是刻意的：商品目录是**租户级可选能力** ——
    电商租户有，harbor_auto_spa（汽车贴膜服务）没有。
    没有目录的租户，接地校验会明确报"跳过 + 原因"，而不是判过、也不是崩掉。
    """
    path = cfg.MERCHANTS_DIR / merchant_id / "catalog.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        # 目录读不动是**配置错误**，不是运行时意外 —— 大声报错但不阻断出稿。
        # 后果是接地校验退化成"跳过"，所以这条日志必须是 error 级，不能是 warning：
        # 静默失去一道 hard 闸门比出稿失败更危险。
        log.error("[%s] catalog.json 解析失败（接地校验将退化为跳过）: %s", merchant_id, e)
        return {}
    if not isinstance(data.get("products"), list):
        log.error("[%s] catalog.json 缺少 products 数组（接地校验将退化为跳过）", merchant_id)
        return {}
    return data


def catalog_prices(catalog: dict) -> set[float]:
    """抽出目录里所有合法价格 —— 校验层的白名单。

    产物是 float 集合而不是字符串：`$32` 和 `$32.00` 必须算同一个价，
    做数值比较就天然解决了格式问题，不用在校验层里解析字面量。
    """
    prices: set[float] = set()
    for p in catalog.get("products") or []:
        v = p.get("price_usd")
        if isinstance(v, (int, float)):
            prices.add(float(v))
    return prices


def retrieve(topic: dict, catalog: dict, top_n: int = 3) -> list[dict]:
    """按关键词重叠给商品打分，返回最相关的 top_n 条。

    Args:
        topic:   选题 dict（用 title / primary_keyword / secondary_keywords / angle）
        catalog: load_catalog 的产物
        top_n:   返回几条

    打分方式（简单到能一句话说清，这本身是优点）：
        查询词集合 ∩ 商品词集合 的大小，name 命中权重加倍。
    name 加倍的理由：面料/版型描述里的词（cotton / tapered）在整份目录里高度重复，
    区分度低；商品名才是真正区分"用户在问哪类商品"的字段。

    中间产物 scored：[(得分, 商品), ...]，便于日志里打出"为什么选了这三条"——
    可解释性是选关键词打分而不是向量检索的核心理由之一，这里要兑现它。
    """
    products = catalog.get("products") or []
    if not products:
        return []

    # 查询词 = 选题的各字段拼起来切词
    query_parts = [
        str(topic.get("title") or ""),
        str(topic.get("primary_keyword") or ""),
        " ".join(str(k) for k in (topic.get("secondary_keywords") or [])),
        str(topic.get("angle") or ""),
    ]
    query = _tokens(" ".join(query_parts))
    if not query:
        # 没有查询词（比如改稿路径没有选题）→ 退化成"给前 top_n 条"，
        # 而不是返回空：有目录总比没目录好，模型至少能看到真实价格区间。
        log.info("检索查询词为空，退化为取目录前 %d 条", top_n)
        return products[:top_n]

    scored: list[tuple[int, dict]] = []
    for p in products:
        name_hits = len(query & _tokens(p.get("name", "")))
        other_hits = len(query & _tokens(
            f"{p.get('fabric', '')} {p.get('fit', '')} {p.get('sku', '')}"
        ))
        score = name_hits * 2 + other_hits          # name 权重加倍，理由见上
        scored.append((score, p))

    scored.sort(key=lambda kv: -kv[0])
    picked = [p for score, p in scored[:top_n]]
    log.info("目录检索：命中 %s",
             ", ".join(f"{p.get('name')}(得分{s})" for s, p in scored[:top_n]))

    # 全都是 0 分（查询词和目录完全不相关）也照样返回 top_n。
    # 理由同上：模型看到真实商品总比凭空编造好，而且校验层还会兜一道。
    return picked


def build_catalog_brief(topic: dict, catalog: dict, top_n: int = 3) -> str:
    """产出塞进 copywriter prompt 的文本块。没有目录时返回空串（调用方按空处理）。

    ⚠️ 措辞上刻意做了两件事：
      ① 明确说"这是唯一合法来源"，并**指名会有自动校验** ——
         模型知道有人会核对时，编造率会下降（这是 prompt 层的预防）。
      ② 明确说"不确定就别写数字，写定性描述" —— 给它一条**合法的退路**。
         只说"不许编"而不给退路，模型倾向于硬凑一个（很多幻觉是"必须回答"的压力造成的）。
    """
    picked = retrieve(topic, catalog, top_n=top_n)
    if not picked:
        return ""

    currency = catalog.get("currency", "USD")
    lines = [
        "## Product Catalog (the ONLY source of truth for prices and product facts)",
        "",
        "These are the real products relevant to this topic. An automated check runs after "
        "you write: every price that appears in your article must match this list exactly, "
        "or the draft is rejected and sent back.",
        "",
    ]
    for p in picked:
        lines.append(
            f"- **{p.get('name')}** ({p.get('sku')}) — {currency} {p.get('price_usd')}\n"
            f"  fabric: {p.get('fabric')}\n"
            f"  fit: {p.get('fit')}\n"
            f"  sizes: {', '.join(p.get('sizes') or [])}\n"
            f"  page: {p.get('url')}"
        )
    lines += [
        "",
        "Rules for using this:",
        "- Quote a price ONLY if it is in the list above, and quote it exactly.",
        "- If you want to talk about cost but the number you need is not here, "
        "describe it qualitatively instead (\"priced for buying several at once\") — "
        "do NOT estimate, average, or round a number into existence.",
        "- Never invent a SKU, a fabric spec, or a size that is not listed.",
        "- Link to the product pages above when you mention a product.",
    ]
    return "\n".join(lines)
