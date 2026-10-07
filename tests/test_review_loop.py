"""pipeline/review_loop.py 的闸门逻辑测试 —— 同样零 API key、零网络。

## 这些测试在测什么
不是测"稿子写得好不好"（那是 LLM 的事，没法断言），而是测**控制流**：
  · 硬性不过时，reviewer 到底有没有被调用（= 有没有真省下那笔钱）
  · 打回 rewrite 时，issues 里装的是不是确定性检查的那几条
  · 返回的 report 描述的是不是"最终那一版"稿子（不变式①）
  · 硬性没过时分数是不是被归零（不变式②）
  · 闸门开关关掉后，行为是不是退化成"只报告不阻断"

## 做法：把两个花钱的 agent 换成计数器
`review_blog` / `rewrite_blog` 被 monkeypatch 成本地假函数并记录调用次数。
这样测试跑得飞快、完全确定，而且**能断言"某次调用没有发生"** ——
"reviewer 没被调用"这件事，用真 API 是没法可靠断言的（只能看账单）。
"""

import config as cfg
import pytest
from test_deterministic import SLOTS, TOPIC, _good_blog  # noqa: E402
# ↑ 直接复用 test_deterministic 里那份"全过"的基准稿。
#   测试之间互相 import 略有异味，但比把同一份 60 行的 fixture 抄两遍好 ——
#   抄两遍的真实后果是：改了一处忘改另一处，然后其中一个测试开始测一份过期的稿子。

from pipeline import review_loop

# 商家配置现在是 run_review_loop 的必填参数（不再全局查表）——
# 测试因此不需要动任何全局状态，只要传一个 dict 进去。
MCFG = {"merchant_id": "harbor_auto_spa", "website": "https://harborautospa.example/"}


def _hard_broken() -> dict:
    """在基准稿里制造**唯一一处 hard 失败**：混进一个 <img> 标签。

    为什么用 <img> 而不是"摘要太短"（这些用例的第一版就是那么写的）：
    2026-07-27 把摘要长度降级成 soft 之后，摘要不合规**不再阻断出稿** ——
    再拿它当"硬失败"的素材，测的就不是闸门了。<img> 是实打实的 hard：
    编造的图片路径会渲染成碎图，用户直接看到坏东西。

    只加一个 <p><img></p>：不引入新的 h2、链接或关键词，所以其余检查项不受影响，
    断言可以写成"恰好这一项失败"。
    """
    blog = _good_blog()
    blog["content_html"] += '<p><img src="/images/made-up-hero.jpg" alt="hero"></p>'
    return blog


@pytest.fixture
def stub_agents(monkeypatch):
    """把 reviewer / rewrite 换成假的，返回一个记录本。

    calls["review"]  每次 reviewer 调用的轮次列表，如 [1, 2]
    calls["rewrite"] 每次 rewrite 调用收到的 feedback（用来断言 issues 内容和来源）
    review_result    测试可以改它来控制"裁判怎么判"
    """
    calls = {"review": [], "rewrite": [], "review_result": {"score": 92, "passed": True}}

    def fake_review(merchant_id, blog_data, topic, round_num=1):
        calls["review"].append(round_num)
        return dict(calls["review_result"]), {"cost": 0.0}

    def fake_rewrite(merchant_id, blog_data, feedback, round_num, mode="auto", image_count=3):
        calls["rewrite"].append(feedback)
        # 模拟真实 rewrite 的契约：**只返回部分字段**（尤其没有 image_alts）——
        # review_loop 必须用 .update() 合并而不是整体替换，这个假实现正好能验证那件事。
        return {"content_html": blog_data["content_html"], "excerpt": "still too short"}, {}

    monkeypatch.setattr(review_loop, "review_blog", fake_review)
    monkeypatch.setattr(review_loop, "rewrite_blog", fake_rewrite)
    return calls


def test_clean_draft_passes_in_one_round(stub_agents):
    """稿子本身合规 + 裁判给 92 分 → 一轮就过，不该有任何 rewrite。"""
    blog = _good_blog()
    score, rounds, report = review_loop.run_review_loop(
        "harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG, mode="auto", image_count=3
    )
    assert (score, rounds) == (92, 1)
    assert report.hard_ok
    assert stub_agents["review"] == [1]      # reviewer 调了 1 次
    assert stub_agents["rewrite"] == []      # 没打回


def test_hard_fail_skips_reviewer_entirely(stub_agents, monkeypatch):
    """★核心断言★ 硬性不过 → reviewer 一次都不调（省钱），直接打回 rewrite。"""
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 2)
    blog = _hard_broken()   # 混进一个 <img> 标签 —— hard 失败（碎图）
    score, rounds, report = review_loop.run_review_loop(
        "harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG, mode="auto", image_count=3
    )

    # ① 花钱的那个没被调用 —— 这就是这一层的直接收益
    assert stub_agents["review"] == []
    # ② 但确实打回重写了（第 1 轮打回，第 2 轮是最终轮不再打回）
    assert len(stub_agents["rewrite"]) == 1
    # ③ 打回时带的是确定性检查的定点指令，且标了来源
    fb = stub_agents["rewrite"][0]
    assert fb["source"] == "deterministic"
    assert fb["passed"] is False
    assert any("<img>" in i for i in fb["issues"])   # 定点指令：指名是 img 标签的问题
    # ④ 不变式②：没被评分过 → 分数是 0，不是编出来的数
    assert score == 0
    assert rounds == 2
    assert not report.hard_ok


def test_gate_off_still_calls_reviewer(stub_agents, monkeypatch):
    """闸门关掉（EVAL_HARD_GATE=False）→ 退化成"只报告不阻断"，reviewer 照常跑。

    这是留给运维的逃生口：某条阈值反复误伤某个商家时，先关闸门保住出稿，
    再慢慢调阈值 —— 而不是被迫去注释代码。
    """
    monkeypatch.setattr(cfg, "EVAL_HARD_GATE", False)
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 1)
    blog = _hard_broken()
    score, rounds, report = review_loop.run_review_loop(
        "harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG, mode="auto", image_count=3
    )
    assert stub_agents["review"] == [1]          # 闸门关了 → 照样花钱审
    assert not report.hard_ok                    # 报告仍然如实记录未通过
    # 通过条件里仍然带着 hard_ok，所以这一版不算"过"；分数按不变式②归零
    assert score == 0


def test_report_describes_the_final_draft(stub_agents, monkeypatch):
    """不变式①：返回的 report 描述的是最终这一版稿子，不是第一版。

    构造方式：第一版混进 <img>（hard 失败）→ 打回 → 假 rewrite 把 content_html 换成
    干净的那份 → 第二轮开头重新检查 → 应该变成 hard_ok=True。
    """
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 3)
    good = _good_blog()

    def fixing_rewrite(merchant_id, blog_data, feedback, round_num, mode="auto", image_count=3):
        stub_agents["rewrite"].append(feedback)
        # 只把 <img> 那段删掉（模拟模型按 issues 定点修复），其余字段保持不动
        return {"content_html": good["content_html"]}, {}

    monkeypatch.setattr(review_loop, "rewrite_blog", fixing_rewrite)

    blog = _hard_broken()
    score, rounds, report = review_loop.run_review_loop(
        "harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG, mode="auto", image_count=3
    )
    # 第 1 轮硬性失败（不调 reviewer）→ 修好 → 第 2 轮硬性通过 → 调 reviewer → 92 分过
    assert stub_agents["review"] == [2]
    assert (score, rounds) == (92, 2)
    assert report.hard_ok, report.failures       # report 反映的是修好之后那版
    assert "<img" not in blog["content_html"]    # blog_data 被原地改成了最新稿


def test_rewrite_merge_preserves_image_alts(stub_agents, monkeypatch):
    """回归测试：rewrite 的输出里没有 image_alts，合并后它必须还在。

    这就是 2026-07-25 修掉的那个真 bug（auto 流程重写一次就丢 image_alts →
    图片 alt 全变成 "标题 - 槽位" 这种垃圾，SEO 第 9 条静默失效）。
    假 rewrite 刻意只返回 content_html + excerpt（没有 image_alts），正好复现当时的契约。
    """
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 2)
    blog = _hard_broken()
    original_alts = dict(blog["image_alts"])

    review_loop.run_review_loop("harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG,
                                mode="auto", image_count=3)

    assert stub_agents["rewrite"], "这个用例的前提是至少打回一次"
    assert blog["image_alts"] == original_alts   # 没被 rewrite 的返回值冲掉


def test_chat_mode_expects_img_slots(stub_agents, monkeypatch):
    """chat 模式的槽位是 img_1..img_N —— 用 auto 的 hero/mid/end 稿子跑 chat 模式，
    占位符集合必然对不上，应该被不变式检查抓住。

    这条同时在验证 _expected_slots() 的推导：参照物来自代码（mode + image_count），
    不是从 blog_data 里读现成的键。
    """
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 1)
    blog = _good_blog()                    # 占位符是 hero/mid/end
    score, rounds, report = review_loop.run_review_loop(
        "harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG, mode="chat", image_count=2   # 期望 img_1/img_2
    )
    assert stub_agents["review"] == []     # 硬性就没过，没花钱
    failed = {c.name for c in report.checks if not c.ok}
    assert failed == {"image_placeholders"}
    assert "img_1" in report.failures[0]


def test_soft_failure_alone_does_not_block_and_does_not_rewrite(stub_agents, monkeypatch):
    """★ 分级的核心收益 ★ 只有 soft 失败时：照常调 reviewer，通过就直接放行。

    构造：内链多加两条（6 条 > 上限 5）—— 纯 soft。
    期望：reviewer 调 1 次、**零次 rewrite**、稿子照常出。
    改级之前这里会白花 2 × $0.028 去重写一篇本来可以发的文章（首篇实测）。
    """
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 3)
    blog = _good_blog()
    # 基准稿已有 3 条内链，上限是 5 —— 要加 3 条才真的超（加 2 条正好卡在上限，不失败）
    blog["content_html"] += ('<p><a href="/blog/">more guides</a>'
                            '<a href="/reviews/">customer reviews</a>'
                            '<a href="/faq/">frequently asked questions</a></p>')

    score, rounds, report = review_loop.run_review_loop(
        "harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG, mode="auto", image_count=3
    )

    assert report.hard_ok                       # soft 失败不影响闸门
    assert report.soft_failures                 # 但如实记录下来了
    assert "internal_links" in {c.name for c in report.checks if not c.ok}
    assert stub_agents["review"] == [1]         # 照常审一次
    assert stub_agents["rewrite"] == []         # ★ 一次都没重写 ★
    assert (score, rounds) == (92, 1)           # 分数如实返回，不因 soft 归零


def test_soft_failures_ride_along_when_a_rewrite_happens_anyway(stub_agents, monkeypatch):
    """soft 问题不单独触发重写，但一旦因为别的原因要重写了，就搭车一起修（零额外成本）。

    构造：内链 6 条（soft）+ reviewer 判 55 分不通过（这才是重写的原因）。
    期望：rewrite 收到的 issues 里**既有 reviewer 的意见，也有那条 soft**。
    """
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 2)
    stub_agents["review_result"] = {"score": 55, "passed": False,
                                    "issues": ["Opening is generic"], "suggestions": []}
    blog = _good_blog()
    # 基准稿已有 3 条内链，上限是 5 —— 要加 3 条才真的超（加 2 条正好卡在上限，不失败）
    blog["content_html"] += ('<p><a href="/blog/">more guides</a>'
                            '<a href="/reviews/">customer reviews</a>'
                            '<a href="/faq/">frequently asked questions</a></p>')

    review_loop.run_review_loop("harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG,
                                mode="auto", image_count=3)

    assert len(stub_agents["rewrite"]) == 1
    issues = stub_agents["rewrite"][0]["issues"]
    assert "Opening is generic" in issues                     # reviewer 的意见还在
    assert any("internal links" in i for i in issues)                    # soft 问题搭上了车


def test_over_long_excerpt_is_repaired_not_rewritten(stub_agents, monkeypatch):
    """★ 修复层的端到端收益 ★ 摘要超长 → 代码当场裁掉，**不触发任何重写**。

    这一条把 2026-07-27 那次赔本操作钉死了：首篇实测稿为「摘要 161 字符」重写两遍、
    烧掉 $0.055。现在同样的输入应该零重写、零 reviewer 额外开销。
    """
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 3)
    good = _good_blog()
    blog = _good_blog(excerpt=good["excerpt"] + " And a few extra words pushing it over the limit.")
    assert len(blog["excerpt"]) > cfg.EVAL_EXCERPT_MAX_CHARS   # 前提：确实超长

    score, rounds, report = review_loop.run_review_loop(
        "harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG, mode="auto", image_count=3
    )

    after = len(blog["excerpt"])
    assert cfg.EVAL_EXCERPT_MIN_CHARS <= after <= cfg.EVAL_EXCERPT_MAX_CHARS
    assert stub_agents["rewrite"] == []                 # ★ 零重写 ★
    assert (score, rounds) == (92, 1)
    assert report.repairs and "Excerpt" in report.repairs[0]  # 卡片上能看到"代码动过手"
    assert "excerpt_length" not in {c.name for c in report.checks if not c.ok}


def test_reviewer_passed_but_low_score_does_not_pass(stub_agents, monkeypatch):
    """双重把关：reviewer 自己说 passed=True 但分数低于阈值 → 仍然不算过。

    防的是 GPT 乱给 passed（json_mode 只保证格式合法，不保证判断合理）。
    """
    monkeypatch.setattr(cfg, "REVIEWER_MAX_ROUNDS", 1)
    stub_agents["review_result"] = {"score": 61, "passed": True}
    blog = _good_blog()
    score, rounds, report = review_loop.run_review_loop(
        "harbor_auto_spa", blog, TOPIC, merchant_cfg=MCFG, mode="auto", image_count=3
    )
    assert stub_agents["review"] == [1]
    assert score == 61          # 分数如实返回（硬性是过的，所以不归零）
    assert report.hard_ok
    # 最终轮不通过 = 不阻断，用当前版本继续 —— 所以这里不抛异常，只是没 break
