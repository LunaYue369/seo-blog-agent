"""商品目录检索 + 接地校验的测试 —— 零 API key、零网络。

## 这一层要证明的三件事
① **检索（预防）** 能按选题挑出相关商品，且可解释（不是向量黑箱）
② **校验（保证）** 能抓住目录里不存在的价格
③ **不误伤** 正文里大量无害的裸数字（40% cotton / 6-inch inseam / size 4XL）

第 ③ 条是这套检查能不能活下来的关键：假阳性一多，人会直接把检查关掉。
所以专门有一组用例围着"什么**不该**被当成价格"转。

## 幻觉治理里的定位
编造价格属于**事实性幻觉（extrinsic hallucination）**，三件套的分工是：
    grounding    retrieval/catalog.py 把真实价格摆进 prompt   → 预防
    attribution  prompt 里要求"只能引用给你的价格"            → 预防
    verification ★check_catalog_grounding★ 出口白名单校验     → **唯一的保证**
本文件测的是第一件和第三件（第二件是 prompt 文案，没法单测）。
"""

import json

import pytest

from eval.deterministic import check_seo
from retrieval.catalog import build_catalog_brief, load_catalog, retrieve

# 迷你目录：够测行为就行（生产该从店铺后台 API 同步，见 retrieval/catalog.py）
CATALOG = {
    "currency": "USD",
    "products": [
        {"sku": "DS-TEE-CREW", "name": "Classic Crew Neck Tee", "price_usd": 32.0,
         "fabric": "Peruvian cotton blend", "fit": "tapered through the waist",
         "sizes": ["S", "M", "L"], "url": "https://example.test/crew"},
        {"sku": "DS-POLO", "name": "Classic Polo", "price_usd": 56.0,
         "fabric": "pique cotton blend", "fit": "structured collar",
         "sizes": ["S", "M", "L"], "url": "https://example.test/polo"},
        {"sku": "DS-HOODIE", "name": "Pullover Hoodie", "price_usd": 78.0,
         "fabric": "brushed fleece", "fit": "relaxed body",
         "sizes": ["S", "M", "L"], "url": "https://example.test/hoodie"},
    ],
}


def _blog(body: str, **over) -> dict:
    d = {"title": "T", "excerpt": "x" * 155, "content_html": f"<p>{body}</p>"}
    d.update(over)
    return d


def _grounding(blog: dict, catalog=CATALOG):
    """只取接地那一项的结果（其它检查项这里不关心）。"""
    rep = check_seo(blog, {}, catalog=catalog)
    return next(c for c in rep.checks if c.name == "catalog_grounding")


# ── ② 校验：抓编造的价格 ──────────────────────────────────────────────


@pytest.mark.parametrize("price", ["$24.99", "$29", "$100", "$1,299", "USD 45"])
def test_invented_price_is_hard_blocked(price):
    """目录里没有的价格 → hard 阻断。这是整套检查里最该 hard 的一条：
    编造价格是客服问题/信任损失，不是"不够好"。"""
    hit = _grounding(_blog(f"Our tee is just {price} today."))
    assert not hit.ok
    assert hit.severity == "hard"
    # 失败文案必须同时给出「错的是什么」和「对的有哪些」，才是可执行的指令
    assert "$32" in hit.detail and "$56" in hit.detail


@pytest.mark.parametrize("price", ["$32", "$32.00", "$56", "$78", "USD 32"])
def test_catalog_price_passes(price):
    """目录里有的价格 → 通过。$32 和 $32.00 必须等价（所以白名单用 float 不用字符串）。"""
    hit = _grounding(_blog(f"The crew neck tee is {price}."))
    assert hit.ok, hit.detail


def test_price_in_title_or_excerpt_is_also_checked():
    """价格出现在标题/摘要里同样要核 —— 那两处进搜索结果，错了传播最广。"""
    hit = _grounding(_blog("Body text with no prices.",
                           title="The $19 Tee That Changed Everything"))
    assert not hit.ok
    assert "title" in hit.detail


def test_mixed_grounded_and_invented_only_reports_the_invented():
    """只报编造的那些，不把对的一起骂 —— 否则打回指令会让模型把对的也改掉。"""
    hit = _grounding(_blog("The tee is $32 and the hoodie is $99."))
    assert not hit.ok
    assert "$99" in hit.detail
    assert hit.metric == [99.0]        # 只有编造的进 metric


def test_no_prices_at_all_passes():
    """★ 刻意不要求文章必须提价格 ★

    「不提价格」是完全正当的写法（人格里也鼓励"说不准就定性描述"）。
    要求必须提反而会逼模型去编 —— 很多幻觉是"必须回答"的压力造成的。
    """
    hit = _grounding(_blog("Priced for buying several at once, without a number in sight."))
    assert hit.ok
    assert "No prices mentioned" in hit.detail


# ── ③ 不误伤：正文里的裸数字 ──────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Made from 40% cotton and 60% modal.",
    "The boxer brief has a 6-inch inseam.",
    "Sizes run from S to 4XL, and 42 is a common chest measurement.",
    "Expect 2-4 years of regular wear.",
    "Wash at 30 degrees, tumble dry on low for 20 minutes.",
])
def test_bare_numbers_are_not_treated_as_prices(text):
    """★ 假阳性防线 ★ 只认带货币符号的数字。

    正文里到处都是无害数字（成分比例/尺寸/年限/温度）。把它们当价格核对会制造
    大量假阳性，这个检查第一天就会被关掉 —— 那才是真正的失败。
    判据：**只在能确定语义的地方做确定性判断**，语义模糊的交给别人。
    """
    hit = _grounding(_blog(text))
    assert hit.ok, hit.detail


def test_no_catalog_means_skipped_not_passed():
    """没有 catalog.json 的租户（harbor_auto_spa 是服务业，没有 SKU）→ 明确跳过。

    缺失的检查不等于通过的检查 —— 这条铁律的由来是 botsrd 那个
    `bounce_rate=0.0` 被当成"跳出率 0% = 完美"的 bug。
    """
    hit = _grounding(_blog("Our detailing service is $199."), catalog=None)
    assert hit.ok
    assert "Skipped" in hit.detail and "catalog.json" in hit.detail


# ── ① 检索：挑得对 + 可解释 ───────────────────────────────────────────


def test_retrieval_ranks_by_keyword_overlap():
    """选题讲 polo → polo 应该排第一。name 命中权重加倍，所以名字里的词最有分量。"""
    picked = retrieve({"title": "How to wear a polo", "primary_keyword": "polo collar",
                       "secondary_keywords": []}, CATALOG, top_n=2)
    assert picked[0]["sku"] == "DS-POLO"


def test_retrieval_falls_back_when_query_is_empty():
    """没有选题（改稿路径）→ 退化成取前 N 条，而不是返回空。

    理由：有目录总比没目录好 —— 模型至少能看到真实价格区间，而不是凭空编。
    """
    picked = retrieve({}, CATALOG, top_n=2)
    assert len(picked) == 2


def test_retrieval_on_empty_catalog_returns_nothing():
    assert retrieve({"title": "anything"}, {}, top_n=3) == []


def test_brief_states_the_rules_and_gives_an_out():
    """prompt 片段必须做两件事：说清"有自动校验"，并**给一条合法退路**。

    只说"不许编"而不给退路，模型倾向于硬凑一个。所以文案里明确写了
    "说不准就定性描述"—— 允许弃答（abstention）是降低幻觉的标准手段。
    """
    brief = build_catalog_brief({"title": "polo guide", "primary_keyword": "polo"}, CATALOG)
    assert "ONLY source of truth" in brief
    assert "automated check" in brief          # 告知有校验（提高首次正确率）
    assert "qualitatively" in brief            # 给退路
    assert "$" not in brief or "USD 56" in brief  # 价格按 currency 字段渲染


# ── 真配置接线 ────────────────────────────────────────────────────────


def test_service_merchant_has_no_catalog():
    """harbor_auto_spa 是汽车贴膜服务，没有 SKU → 不该有 catalog.json。

    这条同时在守护"租户级可选能力"这个设计：如果哪天有人给所有租户都塞一份空目录，
    接地校验就会从"明确跳过"变成"检查了但白名单为空"，那样**任何价格都会被拦**。
    """
    assert load_catalog("harbor_auto_spa") == {}
