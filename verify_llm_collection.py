"""Live 验证：agent → route() 收口是否真跑通（真发 OpenAI 请求，便宜档）

这是个一次性验证脚本，验证「把 agents 的 OpenAI 调用收口到 llm.route」这次改动。
跑法（在 repo 根目录）：
    ./venv/Scripts/python.exe verify_llm_collection.py
成本约 $0.002（两次 gpt-4.1-mini 便宜调用）。验证完可删。

验证覆盖：
  1) 真跑迁移后的 researcher.analyze_and_pick_topics（route→OpenAI→complete→JSON→自动记账）
  2) 真跑 complete_messages 多轮路径（conversation 用的那条）
  3) 读回全局 usage 台账，确认「记账已由 complete() 自动完成」（不再手动 record_usage）
"""

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from core import merchant_config
from services import usage_tracker
from agents import researcher
from llm import route

SESSION = "live-collect-test"


def main() -> None:
    # ── 加载商家（含 souls，researcher 的人格 prompt 要用）──
    merchant_config.load_all_merchants()
    merchants = merchant_config.get_all_merchants()
    mid = "harbor_auto_spa" if "harbor_auto_spa" in merchants else next(iter(merchants))
    print(f"\n测试商家: {mid}\n" + "=" * 60)

    # ── 设一个 session，让自动记账累加到这里，方便最后读回核对 ──
    usage_tracker.set_current_session(SESSION)

    # ── 1) 真跑 researcher 选题（迁移后走 route('topic') → gpt-4.1-mini）──
    trending = [
        {"keyword": "ceramic coating cost worth it", "source": "google_suggest", "context": "autocomplete"},
        {"keyword": "paint protection film tesla model 3", "source": "google_suggest", "context": "autocomplete"},
        {"keyword": "best window tint for tesla", "source": "google_suggest", "context": "autocomplete"},
    ]
    topics, usage = researcher.analyze_and_pick_topics(mid, trending, recent_titles=[], count=2)
    print("\n[1] researcher.analyze_and_pick_topics")
    print("    选出主题:", [t.get("title") for t in topics])
    print("    返回 usage:", usage)
    assert topics, "researcher 没选出主题 —— 收口可能有问题"

    # ── 2) 真跑 complete_messages 多轮路径（conversation 用的那条）──
    mcfg = merchant_config.get_merchant(mid)
    r = route("chat", mcfg).complete_messages(
        messages=[
            {"role": "system", "content": 'Reply ONLY with a JSON object exactly like {"ok": true}.'},
            {"role": "user", "content": "say ok"},
        ],
        max_tokens=50,
        json_mode=True,
        label="live-chat-test",
    )
    print("\n[2] route('chat').complete_messages")
    print(f"    text={r.text.strip()!r}  model={r.model}  provider={r.provider}  cost=${r.cost:.5f}")

    # ── 3) 读回全局台账，确认自动记账生效 ──
    summ = usage_tracker.get_session_summary(SESSION)
    print("\n[3] 全局 usage 台账（本次 session 自动记账）")
    for rec in summ["records"]:
        print(f"    {rec['agent']:22s} {rec['model']:14s} "
              f"pt={rec['prompt_tokens']:5d} ct={rec['completion_tokens']:4d} ${rec['cost']:.5f}")
    print(f"    合计: ${summ['total_cost']:.5f}")
    assert summ["total_cost"] > 0, "台账没记到成本 —— 自动记账没生效"
    # researcher 用 label='researcher'，chat 用 label='live-chat-test' —— 都应出现在台账里
    agents_seen = {rec["agent"] for rec in summ["records"]}
    assert "researcher" in agents_seen, "researcher 的记账丢了"
    assert "live-chat-test" in agents_seen, "complete_messages 的记账丢了"

    print("\n" + "=" * 60)
    # ⚠️ 这里原本是一个 ✅ —— GBK 编不出这个字符，在 Windows 默认控制台（cp936）下
    # 会让这行 print 直接抛 UnicodeEncodeError，脚本在**最后一行**崩掉，
    # 看上去像"验证失败"，其实前面全过了。改成纯 ASCII 标记。
    # （同一个坑家族：logging 里用 ✗ 会静默丢日志记录，见 pipeline/review_loop.py 的注释。）
    print("LIVE 收口验证通过 [OK]  （route->OpenAI->complete->JSON->自动记账 全链路真跑通）")


if __name__ == "__main__":
    main()
