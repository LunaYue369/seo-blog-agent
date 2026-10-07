"""成本归因报表 —— 从 store/usage.json 出一篇文章（或全部 session）的成本分解

## 为什么这个东西存在
「这篇文章花了多少钱、花在哪」是运营和面试都会问的第一个问题。而它能被回答，
靠的不是这个脚本，是**记账点选对了**：`llm/base.complete()` 在每次调用后
自动落一条事实记录（merchant / agent / model / tokens / cost / session / 时间戳），
所以任何维度的归因都只是对同一批事实做 group by。

    ⭐ 这批数据的形状已经是 **warehouse-ready** 的：一次 LLM 调用 = 一行事实。
       接 Daasity → BigQuery → dbt → Omni 是**配置工作，不是设计工作** ——
       真正难的部分（在哪打点、按什么维度归因）已经做完了。
    本脚本只是那张事实表的一个本地视图，不是数据管道。

## 刻意不做的事
- **不做趋势图/看板**：那是 BI 层的活，在这里重造一个只会得到一个假 Omni。
- **不估算**：所有数字都来自真实台账。缺失的维度就显示缺失，不补默认值
  （"缺失指标绝不能当 0" —— 这条铁律的由来见 botsrd 那个 bounce_rate=0.0 的 bug）。

用法：
    ./venv/Scripts/python.exe tools/cost_report.py              # 最近一个 session
    ./venv/Scripts/python.exe tools/cost_report.py --all        # 全部 session 汇总
    ./venv/Scripts/python.exe tools/cost_report.py --session <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

USAGE_PATH = ROOT / "store" / "usage.json"


def _load() -> dict:
    if not USAGE_PATH.exists():
        print(f"没有找到台账文件：{USAGE_PATH}\n（跑过一次生成之后才会有）")
        raise SystemExit(1)
    return json.loads(USAGE_PATH.read_text(encoding="utf-8"))


def _bar(share: float, width: int = 28) -> str:
    """纯 ASCII 条形图 —— 刻意不用方块字符：GBK 控制台编不出来会丢输出
    （今天已经在 logging 里踩过一次同样的坑）。"""
    filled = int(round(share * width))
    return "#" * filled + "." * (width - filled)


def _report_records(title: str, records: list[dict]) -> None:
    """对一批调用记录做两种归因：按 agent（钱花在哪个环节）+ 按 model（花在哪个档位）。"""
    if not records:
        print(f"\n{title}：没有记录")
        return

    total = sum(r.get("cost", 0.0) for r in records)
    tok_in = sum(r.get("prompt_tokens", 0) for r in records)
    tok_out = sum(r.get("completion_tokens", 0) for r in records)
    imgs = sum(r.get("image_count", 0) for r in records)

    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)
    print(f"总成本 ${total:.4f}   |   token {tok_in:,} in / {tok_out:,} out   |   图片 {imgs} 张")

    # ── 归因维度 1：按 agent（= 钱花在哪个环节）──
    # 这是最有用的一维：它直接回答"要降成本该动哪一步"。
    by_agent: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0, "model": set()})
    for r in records:
        a = by_agent[r.get("agent", "?")]
        a["cost"] += r.get("cost", 0.0)
        a["calls"] += 1
        a["model"].add(r.get("model", "?"))

    print("\n按环节（agent）：")
    print(f"  {'环节':22s} {'次数':>4s} {'成本':>9s} {'占比':>7s}  {'模型'}")
    for name, d in sorted(by_agent.items(), key=lambda kv: -kv[1]["cost"]):
        share = d["cost"] / total if total else 0
        models = ",".join(sorted(d["model"]))
        print(f"  {name:22s} {d['calls']:>4d} ${d['cost']:>8.4f} {share*100:>6.1f}%  {models}")
        print(f"  {'':22s} {'':>4s} {'':>9s} {'':>7s}  {_bar(share)}")

    # ── 归因维度 2：按模型档位（= 成本路由有没有生效）──
    # 这一维验证的是"写稿留强模型、杂活用便宜档"这条路由策略是否真的落地了。
    by_model: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    for r in records:
        m = by_model[r.get("model", "?")]
        m["cost"] += r.get("cost", 0.0)
        m["calls"] += 1
    print("\n按模型档位：")
    for name, d in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
        share = d["cost"] / total if total else 0
        print(f"  {name:32s} {d['calls']:>3d} 次  ${d['cost']:>8.4f}  {share*100:>5.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="成本归因报表（数据来自 store/usage.json）")
    ap.add_argument("--all", action="store_true", help="汇总全部 session")
    ap.add_argument("--session", help="指定 session id")
    args = ap.parse_args()

    data = _load()
    if not isinstance(data, dict) or not data:
        print("台账为空")
        return

    if args.all:
        records = [r for s in data.values() for r in (s.get("records") or [])]
        _report_records(f"全部 session 汇总（{len(data)} 个 session）", records)
        return

    # 默认：最近一个 session。用记录里的最大时间戳排序，而不是 dict 顺序 ——
    # dict 顺序取决于写入顺序，恢复过盘的文件不保证还是时间序。
    def _last_ts(s: dict) -> float:
        return max((r.get("timestamp", 0) for r in (s.get("records") or [])), default=0)

    key = args.session or max(data, key=lambda k: _last_ts(data[k]))
    if key not in data:
        print(f"找不到 session {key}\n已有：{', '.join(list(data)[-5:])}")
        raise SystemExit(1)
    sess = data[key]
    _report_records(
        f"session {key}   商家 {sess.get('merchant_id', '?')}",
        sess.get("records") or [],
    )


if __name__ == "__main__":
    main()
