"""定点修复（只重写小标题）的测试 —— 零 API key、零网络。

## 这一层要证明的四件事
① **只改该改的**：涉事标题被换，其它标题和**整篇正文**逐字不动
② **FAQ 标题永不被改**（布局强制它叫那个名字，改了会破坏 FAQ 结构）
③ **失败即放弃**：模型返回畸形（不是 JSON / 不是对象 / 数量不够 / 换了等于没换）
   → 保留原标题，绝不把文章改坏
④ **成本形状**：prompt 里**一个字正文都没有** —— 这是 $0.028 → $0.0003 的唯一原因

第 ④ 条是这个模块存在的全部理由，所以专门有一条测试盯着它：
哪天有人"顺手"把 content_html 塞进 prompt 想让模型改得更准，成本会静默涨 100 倍，
而这条测试会立刻红。

## 做法：把 route() 换成假 client
`agents.heading_fixer.route` 被 monkeypatch 掉，假 client 记录收到的 prompt
并返回可控的 JSON。这样能断言"prompt 里有什么/没有什么"—— 用真 API 做不到这件事。
"""

import json

import pytest

from agents import heading_fixer

TOPIC = {"primary_keyword": "slim fit vs regular fit t shirt"}

# 一篇有堆砌问题的稿子：4 个 h2 共享 "regular fit t"，外加一个 FAQ 标题
BODY = """
<p>Opening paragraph about fit.</p>
<h2>What is a slim fit vs regular fit t shirt</h2>
<p>Body one.</p>
<h2 class="blog-section">How to pick a regular fit t shirt</h2>
<p>Body two.</p>
<h2>Is a regular fit t shirt better for layering</h2>
<p>Body three.</p>
<h2>When to size up in a regular fit t shirt</h2>
<p>Body four.</p>
<div class="faq-section">
  <h2>Frequently Asked Questions</h2>
  <div class="faq-item"><h3>Does it shrink?</h3><p>A little.</p></div>
</div>
"""

# metric 的形状就是 check 那边产出的：{重复短语: [标题序号]}
# 序号 0-3 是四个内容标题，4 是 FAQ（这里刻意把 4 也塞进来，验证保护逻辑）
METRIC = {"regular fit t": [1, 2, 3, 4]}


class _FakeResult:
    def __init__(self, text):
        self.text = text
        self.cost = 0.0003
        self.prompt_tokens = 300
        self.completion_tokens = 100


class _FakeClient:
    """假 LLM client，记录收到的 prompt，返回预设的文本。"""

    def __init__(self, reply, box):
        self._reply = reply
        self._box = box

    def complete(self, *, system, user, **kw):
        self._box["system"] = system
        self._box["user"] = user
        self._box["kwargs"] = kw
        return _FakeResult(self._reply)


@pytest.fixture
def fake_llm(monkeypatch):
    """把 route() 换成返回假 client 的工厂。box 里能拿到实际发出去的 prompt。"""
    box: dict = {"reply": json.dumps({
        "1": "How a tapered cut changes the way a shirt hangs",
        "2": "Picking between two cuts when you shop online",
        "3": "Layering: which cut sits better under a jacket",
    })}

    def _fake_route(task, mcfg):
        box["task"] = task
        return _FakeClient(box["reply"], box)

    monkeypatch.setattr(heading_fixer, "route", _fake_route)
    monkeypatch.setattr(heading_fixer.merchant_config, "get_merchant",
                        lambda mid: {"merchant_id": mid})
    return box


def _blog():
    return {"title": "T", "content_html": BODY}


def _headings(html):
    return [t for _, t in heading_fixer._extract_headings(html)]


# ── ① 只改该改的 ─────────────────────────────────────────────────────


def test_all_headings_are_sent_as_a_set(fake_llm):
    """★ 2026-07-27 语义纠正 ★ 发**全部**标题，不是只发被点名的那几个。

    第一版只发 metric 里点名的标题，结果模型和它没看见的那些撞出了新的重复 ——
    修完堆砌还在。**重复是集合的属性，不是元素的属性**，所以模型必须看到全集。
    （范围仍然是"只有标题、没有正文"，成本不变。）
    """
    blog = _blog()
    before = _headings(blog["content_html"])
    changed, note = heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)

    assert changed
    after = _headings(blog["content_html"])
    assert len(after) == len(before), "标题数量不能变"
    # 四个内容标题（0-3）全部发给了模型 —— 编号 1./2./3./4. 对应下标 0/1/2/3
    sent = fake_llm["user"]
    for _, text in [(i, x) for i, x in enumerate(before) if i < 4]:
        assert text in sent, f"标题 {text!r} 没被发给模型（全集才能全局去重）"
    # 假 client 只回了 3 个（键 1/2/3 → 下标 0/1/2），所以那三个变、第四个不变
    assert after[0] != before[0] and after[1] != before[1] and after[2] != before[2]
    assert after[3] == before[3], "模型没回的那个应保持原样"
    assert "Targeted fix" in note and "body untouched" in note


def test_body_text_is_untouched(fake_llm):
    """★ 核心承诺 ★ 正文逐字不变 —— 这是"定点"二字的全部意义。"""
    blog = _blog()
    heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)
    for para in ("Body one.", "Body two.", "Body three.", "Body four.",
                 "Opening paragraph about fit.", "Does it shrink?"):
        assert para in blog["content_html"], f"正文片段 {para!r} 被改动了"


def test_h2_attributes_are_preserved(fake_llm):
    """开标签上的 class 必须原样保留 —— 模板和布局靠它做样式。"""
    blog = _blog()
    heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)
    assert '<h2 class="blog-section">' in blog["content_html"]


# ── ② FAQ 标题受保护 ─────────────────────────────────────────────────


def test_faq_heading_is_never_rewritten(fake_llm):
    """"Frequently Asked Questions" 是布局写死的段落标题（FAQ schema 认它），
    即使它被算进堆砌也必须剔除。"""
    blog = _blog()
    heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)
    assert "<h2>Frequently Asked Questions</h2>" in blog["content_html"]
    # 只发了 3 个标题给模型（序号 1/2/3），FAQ 那个（4）被剔掉了
    assert "Frequently Asked Questions" not in fake_llm["user"]


# ── ③ 失败即放弃（fail-safe，不是 fail-fast）───────────────────────────


@pytest.mark.parametrize("bad_reply", [
    "not json at all",                                  # 不是 JSON
    '["a", "b", "c"]',                                  # 是 JSON 但顶层不是对象
    '{"1": "Only one heading rewritten"}',              # 数量不够（<2）
    '{"1": "", "2": "  ", "3": null}',                  # 空值 / 非字符串
    # 「换了等于没换」：把**涉事标题（序号 1/2/3）的原文**原样返回。
    # ⚠️ 注意这里必须用 1/2/3 的原文，不能用 0/1/2 的 —— metric 说涉事的是 1-4，
    #    剔除 FAQ(4) 后 targets = [1,2,3]，而 prompt 里的编号 1./2./3. 对应的是
    #    这三个**下标**。第一版写错成 0/1/2 的原文，于是每个位置都"变了"，
    #    测试反而红了。序号口径对不齐是这类定点修复最容易出的错，所以留个注释。
    # 现在发全集，prompt 里的编号 1./2./3. 对应**下标 0/1/2**，所以要用那三个的原文。
    # （这个口径在 2026-07-27 从"只发涉事标题"改成"发全集"时变过一次，
    #   序号对不齐是这类定点修复最容易出的错，改动时务必重新核对。）
    '{"1": "What is a slim fit vs regular fit t shirt", "2": "How to pick a regular fit t shirt", "3": "Is a regular fit t shirt better for layering"}',
])
def test_malformed_reply_keeps_original_headings(fake_llm, bad_reply):
    """修标题是锦上添花，不是正确性要求。

    为了修一个 soft 问题而把文章搞坏是明显的亏本交易 ——
    所以这里的失败处理刻意选"什么都不做"，只记 warning。
    """
    fake_llm["reply"] = bad_reply
    blog = _blog()
    before = blog["content_html"]
    changed, note = heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)
    assert changed is False
    assert note == ""
    assert blog["content_html"] == before, "失败时正文和标题都必须原样"


def test_llm_exception_is_swallowed(fake_llm, monkeypatch):
    """预算闸门抛 BudgetExceeded 之类的异常也不能让整篇失败。"""
    def _boom(task, mcfg):
        raise RuntimeError("budget exceeded")
    monkeypatch.setattr(heading_fixer, "route", _boom)
    blog = _blog()
    before = blog["content_html"]
    changed, _ = heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)
    assert changed is False and blog["content_html"] == before


def test_too_long_reply_is_rejected(fake_llm):
    """异常长的"标题"= 模型开始写句子了，丢掉（h2 不该是一段话）。"""
    fake_llm["reply"] = json.dumps({str(i): "x" * 200 for i in (1, 2, 3)})
    blog = _blog()
    changed, _ = heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)
    assert changed is False


def test_nothing_to_do_when_fewer_than_two_targets(fake_llm):
    """整篇只有一个非保护标题 → 没有"重复"可谈，不值得花一次调用。

    ⚠️ 触发条件在改成"发全集"之后变了：以前看 metric 里点名了几个，
    现在看**文章里有几个非保护标题**。所以这条用例要造一篇只有 1 个内容标题的稿子。
    """
    blog = {"title": "T", "content_html":
            "<h2>Only one real heading</h2><p>Body.</p>"
            "<h2>Frequently Asked Questions</h2><p>Q.</p>"}
    changed, _ = heading_fixer.fix_stuffed_headings(
        "demo_shop", blog, {"phrase": [0, 1]}, TOPIC)
    assert changed is False
    assert "task" not in fake_llm, "不该发起任何模型调用"


# ── ④ 成本形状：正文绝不进 prompt ─────────────────────────────────────


def test_prompt_contains_headings_only_never_the_body(fake_llm):
    """★ 这个模块存在的全部理由 ★

    整篇重写 ≈ $0.028（全文进出 gpt-4.1）；只发标题 ≈ $0.0003。
    两个数量级的差价只来自一件事：**别把正文塞进 prompt**。
    哪天有人"顺手"把 content_html 加进去想让模型改得更准，成本会静默涨 100 倍 ——
    这条测试就是拦那一手的。
    """
    blog = _blog()
    heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)
    sent = fake_llm["user"]

    # 该有的：涉事标题 + 重复短语 + 主关键词
    assert "How to pick a regular fit t shirt" in sent
    assert "regular fit t" in sent
    assert TOPIC["primary_keyword"] in sent

    # 绝不该有的：任何正文片段
    for para in ("Body one.", "Body two.", "Opening paragraph about fit.", "Does it shrink?"):
        assert para not in sent, f"正文片段 {para!r} 泄漏进了 prompt —— 成本会涨两个数量级"
    assert "<p>" not in sent, "prompt 里不该出现正文 HTML"


def test_uses_the_cheap_routing_key(fake_llm):
    """必须走 headings 这个任务键（便宜档），不能复用 rewrite（gpt-4.1）。

    新能力配自己的路由键，成本档位才是显式的、且能按商家覆盖 ——
    复用 rewrite 就会悄悄用上贵 10 倍的模型。
    """
    blog = _blog()
    heading_fixer.fix_stuffed_headings("demo_shop", blog, METRIC, TOPIC)
    assert fake_llm["task"] == "headings"

    import config as cfg
    assert cfg.DEFAULT_ROUTING["headings"] == "gpt-4.1-mini"


# ── ⑤ listicle 编号必须保住（一次真实回归的回归测试）─────────────────


def test_listicle_numbering_is_preserved(fake_llm):
    """★ 回归测试 ★ listicle 布局的 `1. ` `2. ` 结构编号不能被改写弄丢。

    2026-07-27 实测：prompt 里写了 "no numbering"（本意是别让模型自己加列表标记），
    结果在 "7 Tips for…" 这种 listicle 下**把真正的结构编号删掉了** ——
    七条 tips 有三条没了序号，列表读起来就断了。
    修法不是去 prompt 里解释布局，而是代码把编号摘下来、改完再贴回去：
    **保留结构是机械操作**，属于"删/保是代码的活"里代码那一半。
    """
    # ⚠️ 刻意让**原编号（4/5/6）和 prompt 里的列表位置（1/2/3）不同** ——
    # 否则没法断言"发出去的标题是去过编号的"：prompt 自己也用 `1. 2. 3.` 编号，
    # 如果原编号正好也是 1/2/3，两者字面会撞上，断言就失去区分力（第一版就是这么误报的）。
    blog = {"title": "7 Tips", "content_html":
            "<h2>4. Pick the right fit for men</h2><p>A.</p>"
            "<h2>5. Check the fit for men again</h2><p>B.</p>"
            "<h2>6. Compare the fit for men</h2><p>C.</p>"}
    fake_llm["reply"] = json.dumps({
        "1": "Start with the cut that matches your build",
        "2": "Measure across the shoulders before you buy",
        "3": "Weigh fabric weight against how you will wear it",
    })
    changed, _ = heading_fixer.fix_stuffed_headings(
        "demo_shop", blog, {"fit for men": [0, 1, 2]}, TOPIC)

    assert changed
    heads = _headings(blog["content_html"])
    # 原编号 4/5/6 必须原样贴回（不是被 prompt 的列表位置 1/2/3 覆盖）
    assert heads[0].startswith("4. "), heads[0]
    assert heads[1].startswith("5. "), heads[1]
    assert heads[2].startswith("6. "), heads[2]
    assert "Start with the cut" in heads[0]
    # 发给模型的标题里**不该**带原编号（编号是结构不是内容，给它看只会干扰）
    sent = fake_llm["user"]
    assert "4. Pick" not in sent and "5. Check" not in sent and "6. Compare" not in sent
    assert "Pick the right fit for men" in sent


@pytest.mark.parametrize("prefix", [
    "1. ", "2) ", "3、", "Step 1: ", "Step 2 - ", "STEP 3: ", "Part 4: ", "Tip 5. ", "#6: ",
])
def test_all_structural_prefix_shapes_are_preserved(fake_llm, prefix):
    """★ 第二次回归的回归测试 ★ 序号前缀有很多写法，都必须保住。

    第一版正则只认裸编号 `1. `，而 **how-to 布局用的是 `Step 1: `** ——
    实测五个步骤的序号全丢，"分步指南"退化成一堆平行小标题。
    教训不是"再补一种格式"，而是：**这类前缀由布局决定，而我们有 6 种布局**，
    所以要按"序数词 + 数字 + 分隔符"的通用形状匹配，而不是穷举具体写法。
    """
    blog = {"title": "T", "content_html":
            f"<h2>{prefix}Alpha fit for men</h2><p>A.</p>"
            f"<h2>{prefix}Beta fit for men</h2><p>B.</p>"}
    fake_llm["reply"] = json.dumps({"1": "Rewritten alpha", "2": "Rewritten beta"})
    changed, _ = heading_fixer.fix_stuffed_headings(
        "demo_shop", blog, {"fit for men": [0, 1]}, TOPIC)
    assert changed
    heads = _headings(blog["content_html"])
    assert heads[0] == f"{prefix}Rewritten alpha", heads[0]
    assert heads[1] == f"{prefix}Rewritten beta", heads[1]


@pytest.mark.parametrize("heading", [
    "5 Ways to Save Money on Basics",   # 数字后面是空格+字母，不是分隔符 → 不是序号
    "2026 in Review: What Sold",        # 四位数年份 → 不是序号
    "How to Wash a Tee for Men",        # 没有数字
])
def test_non_prefix_numbers_are_not_mistaken_for_structure(fake_llm, heading):
    """★ 假阳性防线 ★ 标题**以数字开头**不等于它有结构序号。

    误判的后果很难看：会把 "5 Ways…" 的 "5 " 当序号摘掉，改完再贴回去，
    结果标题变成 "5 <模型写的新标题>"，语义直接坏掉。
    """
    blog = {"title": "T", "content_html":
            f"<h2>{heading}</h2><p>A.</p><h2>Another fit for men heading</h2><p>B.</p>"}
    fake_llm["reply"] = json.dumps({"1": "Clean rewrite one", "2": "Clean rewrite two"})
    changed, _ = heading_fixer.fix_stuffed_headings(
        "demo_shop", blog, {"fit for men": [0, 1]}, TOPIC)
    assert changed
    heads = _headings(blog["content_html"])
    assert heads[0] == "Clean rewrite one", f"不该贴任何前缀，实际: {heads[0]!r}"


def test_model_added_numbering_is_stripped_before_reattaching(fake_llm):
    """模型自己又加了 "1. " → 先剥掉它加的，再贴回原来的，避免 "2. 1. Foo" 双重编号。"""
    blog = {"title": "3 Tips", "content_html":
            "<h2>1. Alpha fit for men</h2><p>A.</p>"
            "<h2>2. Beta fit for men</h2><p>B.</p>"}
    fake_llm["reply"] = json.dumps({"1": "1. Rewritten alpha", "2": "2) Rewritten beta"})
    changed, _ = heading_fixer.fix_stuffed_headings(
        "demo_shop", blog, {"fit for men": [0, 1]}, TOPIC)
    assert changed
    heads = _headings(blog["content_html"])
    assert heads[0] == "1. Rewritten alpha", heads[0]
    assert heads[1] == "2. Rewritten beta", heads[1]
