"""渠道派生 + 渠道无关校验的测试 —— 零 API key、零网络。

## 这一层要证明的四件事
① **同一套 eval**：邮件/社媒走的是博客那边同一个检查函数、同一张严重度表，
   不是复制出来的"邮件版检查"
② **严重度是渠道相关的**：emoji 在博客里 hard 禁，在 Instagram 里是正常表达
③ **失败不阻断文章**：某条渠道原子编了价格 → 那条标 blocked，文章不受影响
④ **成本形状**：prompt 里**没有正文** —— 派生一条邮件主题不需要读完 1200 词

第 ① 条是"内容原子全渠道"这个主张的全部依据：如果每加一个渠道就要新写一套检查，
那说明检查层设计错了。所以有一条测试直接断言"用的是同一个函数"。
"""

import json

import pytest

from channels import derive
from eval.deterministic import check_channel_content

BRAND = ("belly", "flattering for", "slimming")
CATALOG = {
    "currency": "USD",
    "products": [
        {"sku": "DS-TEE", "name": "Classic Crew Neck Tee", "price_usd": 32.0,
         "fabric": "cotton blend", "fit": "tapered", "sizes": ["S", "M"],
         "url": "https://example.test/tee"},
    ],
}
MCFG = {
    "merchant_id": "demo_shop",
    "brand_voice": {"forbidden_terms": list(BRAND)},
    "channels": ["email", "social"],
}
BLOG = {
    "title": "How to Find Your T-Shirt Size",
    "excerpt": "x" * 155,
    "content_html": "<p>THIS_IS_THE_ARTICLE_BODY_AND_MUST_NEVER_REACH_THE_PROMPT</p>",
}
TOPIC = {"primary_keyword": "t shirt size", "secondary_keywords": []}


# ── ①② 渠道无关校验：共享检查项 + 渠道相关的严重度 ────────────────────


def test_channel_checks_reuse_the_blog_check_functions():
    """★ 全渠道成立的依据 ★ 三条渠道无关的检查必须是博客那边**同一批**检查项。

    断言的是**检查项 ID 相同** —— 也就是同一个函数、同一张 _SEVERITY 表。
    如果哪天有人为渠道另写一套 `_check_email_brand_voice`，这条会红。
    """
    rep = check_channel_content(
        {"subject": "Find your size"}, {"body": "Plain body."},
        brand_terms=BRAND, catalog=CATALOG,
    )
    names = {c.name for c in rep.checks}
    assert names == {
        "brand_voice_headline",   # 和博客共享
        "brand_voice_body",       # 和博客共享
        "forbidden_claims",       # 和博客共享
        "catalog_grounding",      # 和博客共享
        "no_emoji",               # 博客也有，但渠道可关（见下一条）
    }
    # 博客专属的那些**不该**出现 —— 数 h2、查占位符对一条 caption 没有意义
    for blog_only in ("h2_count", "image_placeholders", "internal_links", "excerpt_length"):
        assert blog_only not in names


def test_emoji_is_hard_in_email_but_allowed_in_social():
    """★ 同一个检查项的严重度是**渠道相关**的 ★

    emoji 在博客/邮件里是 hard 禁（渲染 + 企业网关），
    在 Instagram caption 里是正常表达 —— 禁掉反而不对。
    这不是例外，是分级判据（"坏的" vs "不够好"）在渠道维度上的延伸。
    """
    fields = ({"caption": "New drop 🔥"}, {})
    blocked = check_channel_content(*fields, brand_terms=BRAND, catalog=CATALOG,
                                    allow_emoji=False)
    allowed = check_channel_content(*fields, brand_terms=BRAND, catalog=CATALOG,
                                    allow_emoji=True)
    assert not blocked.hard_ok and "no_emoji" in {c.name for c in blocked.checks}
    assert allowed.hard_ok and "no_emoji" not in {c.name for c in allowed.checks}


def test_invented_price_blocks_a_channel_atom():
    """编造价格在渠道里危害**更大**（短信/邮件发出去就撤不回），所以照样 hard。"""
    rep = check_channel_content(
        {"subject": "Tees are just $19 today"}, {"body": "Body."},
        brand_terms=BRAND, catalog=CATALOG,
    )
    assert not rep.hard_ok
    assert any("$19" in f for f in rep.hard_failures)


def test_brand_term_in_subject_is_hard_but_in_body_is_soft():
    """门面（主题行/预览文）hard、正文 soft —— 和博客的标题/正文划分同一个逻辑。"""
    hard = check_channel_content({"subject": "Hide your belly"}, {"body": "Body."},
                                 brand_terms=BRAND, catalog=CATALOG)
    soft = check_channel_content({"subject": "Find your size"},
                                 {"body": "Measured above the belly button."},
                                 brand_terms=BRAND, catalog=CATALOG)
    assert not hard.hard_ok
    assert soft.hard_ok and soft.soft_failures


# ── 派生：假模型 ──────────────────────────────────────────────────────


class _R:
    def __init__(self, text):
        self.text, self.cost = text, 0.0005
        self.prompt_tokens, self.completion_tokens = 500, 200


@pytest.fixture
def fake_llm(monkeypatch):
    """把 route() 换成假 client；box 里记录每次发出的 prompt 和调用次数。"""
    box: dict = {"replies": [], "prompts": [], "tasks": []}

    class _C:
        def complete(self, *, system, user, **kw):
            box["prompts"].append(user)
            return _R(box["replies"].pop(0) if box["replies"] else "{}")

    monkeypatch.setattr(derive, "route", lambda task, cfg: (box["tasks"].append(task) or _C()))
    monkeypatch.setattr(derive.merchant_config, "get_merchant", lambda m: {"merchant_id": m})
    monkeypatch.setattr(derive, "load_catalog", lambda m: CATALOG)
    return box


def _email_reply(subject="Your size in two minutes", preview="No tape measure needed",
                 body="Short body copy that fits the limit."):
    return json.dumps({"subject": subject, "preview_text": preview, "body": body})


# ── ④ 成本形状：正文绝不进 prompt ─────────────────────────────────────


def test_article_body_never_reaches_the_prompt(fake_llm):
    """★ 这个模块的成本承诺 ★ 派生只需要标题 + 摘要，不需要读完 1200 词正文。

    和 heading_fixer 是同一条判据：把最小必要输入塞进 prompt，不多给。
    哪天有人"顺手"把 content_html 加进去想让派生更准，成本会静默上涨 ——
    这条测试拦那一手。
    """
    fake_llm["replies"] = [_email_reply()]
    derive.derive_channel("demo_shop", BLOG, TOPIC, "email", merchant_cfg=MCFG)
    sent = fake_llm["prompts"][0]
    assert "THIS_IS_THE_ARTICLE_BODY_AND_MUST_NEVER_REACH_THE_PROMPT" not in sent
    assert BLOG["title"] in sent          # 该给的给了
    assert "Classic Crew Neck Tee" in sent  # 目录片段（真实价格）也给了


def test_uses_the_cheap_channel_routing_key(fake_llm):
    """走 channel 这个任务键（便宜档），不复用 write/rewrite。"""
    fake_llm["replies"] = [_email_reply()]
    derive.derive_channel("demo_shop", BLOG, TOPIC, "email", merchant_cfg=MCFG)
    assert fake_llm["tasks"] == ["channel"]
    import config as cfg
    assert cfg.DEFAULT_ROUTING["channel"] == "gpt-4.1-mini"


# ── ③ 失败语义：重派一次，不阻断文章 ─────────────────────────────────


def test_hard_failure_triggers_one_targeted_retry(fake_llm):
    """第一次编了价格 → 把失败当**定点指令**重派一次 → 第二次通过。

    和博客那条 rewrite 路径同一个思路：反馈精确到问题本身，不是"再写一遍"。
    """
    fake_llm["replies"] = [
        _email_reply(subject="Tees for just $19"),   # 目录里没有 $19
        _email_reply(),                              # 修好了
    ]
    atom = derive.derive_channel("demo_shop", BLOG, TOPIC, "email", merchant_cfg=MCFG)
    assert atom.attempts == 2 and not atom.blocked
    # 第二次的 prompt 里要带着上一轮的失败原因
    assert "previous attempt was rejected" in fake_llm["prompts"][1]
    assert "$19" in fake_llm["prompts"][1]
    assert atom.cost == pytest.approx(0.001)


def test_still_failing_marks_blocked_without_affecting_the_article(fake_llm):
    """★ 关键的失败语义 ★ 两次都不过 → 这条原子 blocked，**文章本身不受影响**。

    渠道派生是**附加产物**：一条编了价格的 caption 不该让一篇已经过闸门的文章作废。
    所以这里返回"部分成功"，而不是抛异常。
    """
    fake_llm["replies"] = [_email_reply(subject="Only $19"), _email_reply(subject="Still $19")]
    atom = derive.derive_channel("demo_shop", BLOG, TOPIC, "email", merchant_cfg=MCFG)
    assert atom.blocked and atom.attempts == 2
    assert atom.report and not atom.report.hard_ok
    assert BLOG["content_html"].startswith("<p>THIS_IS_THE_ARTICLE_BODY")   # 原文没被动过


def test_over_long_subject_is_sent_back_not_truncated(fake_llm):
    """超长字段**只报告不截断** —— 和博客摘要的自动裁剪刻意不同。

    邮件主题行截断会把句子切断（"Save 20% on the tee tha"），语义直接坏掉；
    博客摘要是完整句尾裁剪，损失小。**能不能安全机械修，取决于字段的语义结构。**
    """
    fake_llm["replies"] = [_email_reply(subject="x" * 120), _email_reply()]
    atom = derive.derive_channel("demo_shop", BLOG, TOPIC, "email", merchant_cfg=MCFG)
    assert atom.attempts == 2 and not atom.blocked
    assert "超过 50 上限" in fake_llm["prompts"][1]


@pytest.mark.parametrize("bad", ["not json", "[1,2]", '{"subject": "only this"}'])
def test_malformed_reply_retries_then_blocks(fake_llm, bad):
    """畸形返回（非 JSON / 非对象 / 缺字段）→ 重派，仍不行就 blocked。"""
    fake_llm["replies"] = [bad, bad]
    atom = derive.derive_channel("demo_shop", BLOG, TOPIC, "email", merchant_cfg=MCFG)
    assert atom.blocked


def test_llm_exception_does_not_propagate(fake_llm, monkeypatch):
    """预算闸门抛异常也不能让文章失败 —— 派生是附加产物。"""
    def _boom(task, cfg):
        raise RuntimeError("budget exceeded")
    monkeypatch.setattr(derive, "route", _boom)
    atom = derive.derive_channel("demo_shop", BLOG, TOPIC, "email", merchant_cfg=MCFG)
    assert atom.blocked and atom.content == {}


# ── 租户级开关 ────────────────────────────────────────────────────────


def test_no_channels_configured_means_no_calls(fake_llm):
    """没配 channels 的租户（汽车贴膜那家）→ 一行代码都不会跑到。"""
    atoms = derive.derive_all("harbor_auto_spa", BLOG, TOPIC,
                              merchant_cfg={"merchant_id": "harbor_auto_spa"})
    assert atoms == []
    assert fake_llm["tasks"] == []


def test_derive_all_runs_every_configured_channel(fake_llm):
    fake_llm["replies"] = [
        _email_reply(),
        json.dumps({"caption": "Fit that actually works 👌", "hashtags": "#fit #tees #mensstyle #basics"}),
    ]
    atoms = derive.derive_all("demo_shop", BLOG, TOPIC, merchant_cfg=MCFG)
    assert [a.channel for a in atoms] == ["email", "social"]
    assert all(not a.blocked for a in atoms), [a.report.hard_failures for a in atoms if a.blocked]
    # 社媒那条带 emoji 也照样通过（allow_emoji 从 spec 读）
    assert "👌" in atoms[1].content["caption"]


def test_unknown_channel_is_skipped_not_crashed(fake_llm):
    atom = derive.derive_channel("demo_shop", BLOG, TOPIC, "carrier_pigeon",
                                 merchant_cfg=MCFG)
    assert atom.blocked and fake_llm["tasks"] == []
