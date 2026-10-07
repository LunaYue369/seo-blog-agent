"""Token 用量追踪器 — 记录所有 AI 模型调用的 token 消耗和成本

追踪维度：(merchant_id, agent_name, model_name)
支持 OpenAI 文本模型和 Seedream 图片模型的用量记录。
"""

import json
import logging
import threading
import time
from pathlib import Path

import config as cfg

log = logging.getLogger(__name__)

# 线程安全锁
_lock = threading.Lock()

# 内存中的用量记录（按 session_id 分组）
_sessions: dict[str, dict] = {}

# 线程局部存储 — 让 agent 调用 record_usage 时自动关联 session_id
_thread_local = threading.local()


def set_current_session(session_id: str) -> None:
    """设置当前线程的活跃 session_id（由 blog_generator 在生成开始时调用）"""
    _thread_local.session_id = session_id


def get_current_session() -> str:
    """获取当前线程的活跃 session_id"""
    return getattr(_thread_local, "session_id", "")

# ── 模型价格表（每百万 token / 每张图片，美元）─────────────
# ⚠️ 新增模型必须在这里登记价格，否则 _estimate_cost 查不到会算成 $0（成本静默漏记）。
_PRICE_TABLE = {
    # OpenAI 文本模型
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-5.2": {"input": 2.50, "output": 15.00},          # 写稿升级备用（若切 write→gpt-5.2）
    # Anthropic 文本模型（换族裁判用；价格取官方标准价，非引入优惠价，偏保守）
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},  # ⭐ 默认裁判模型
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},  # 更强裁判（贵，按需）
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},  # 便宜档 Claude
    # Seedream 图片模型（按张计费，每张约 $0.02）
    "doubao-seedream-4-5-251128": {"per_image": 0.02},
}


def _estimate_cost(model: str, prompt_tokens: int = 0,
                   completion_tokens: int = 0, image_count: int = 0) -> float:
    """估算单次调用的成本（美元）

    Args:
        model: 模型名称
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        image_count: 生成的图片数量（仅图片模型）

    Returns:
        估算成本（美元）
    """
    prices = _PRICE_TABLE.get(model, {})

    if "per_image" in prices:
        return prices["per_image"] * image_count

    input_cost = (prompt_tokens / 1_000_000) * prices.get("input", 0)
    output_cost = (completion_tokens / 1_000_000) * prices.get("output", 0)
    return input_cost + output_cost


def record_usage(
    merchant_id: str,
    agent: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    image_count: int = 0,
    session_id: str = "",
) -> dict:
    """记录一次 AI 调用的 token 用量

    Args:
        merchant_id: 商家标识
        agent: Agent 名称（researcher / copywriter / reviewer / artist / seedream）
        model: 模型名称
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        image_count: 图片数量（仅 Seedream）
        session_id: 会话 ID（用于分组同一次生成的所有用量）

    Returns:
        本次记录的用量字典
    """
    # 如果调用方没传 session_id，从线程局部存储获取（blog_generator 会在生成开始时设置）
    if not session_id:
        session_id = get_current_session()

    cost = _estimate_cost(model, prompt_tokens, completion_tokens, image_count)

    record = {
        "merchant_id": merchant_id,
        "agent": agent,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "image_count": image_count,
        "cost": round(cost, 6),
        "timestamp": time.time(),
    }

    with _lock:
        if session_id:
            if session_id not in _sessions:
                _sessions[session_id] = {
                    "merchant_id": merchant_id,
                    "records": [],
                    "total_cost": 0.0,
                    "created_at": time.time(),
                }
            _sessions[session_id]["records"].append(record)
            _sessions[session_id]["total_cost"] += cost

    log.info("[%s] 用量记录: agent=%s model=%s pt=%d ct=%d imgs=%d cost=$%.4f",
             merchant_id, agent, model, prompt_tokens, completion_tokens, image_count, cost)

    return record


def get_session_summary(session_id: str) -> dict:
    """获取某次生成会话的用量汇总

    Returns:
        {merchant_id, records: [...], total_cost, total_prompt_tokens, total_completion_tokens, total_images}
    """
    with _lock:
        session = _sessions.get(session_id, {})

    if not session:
        return {"records": [], "total_cost": 0.0}

    records = session.get("records", [])
    total_pt = sum(r["prompt_tokens"] for r in records)
    total_ct = sum(r["completion_tokens"] for r in records)
    total_imgs = sum(r["image_count"] for r in records)
    total_cost = session.get("total_cost", 0.0)

    return {
        "merchant_id": session.get("merchant_id", ""),
        "records": records,
        "total_prompt_tokens": total_pt,
        "total_completion_tokens": total_ct,
        "total_images": total_imgs,
        "total_cost": round(total_cost, 4),
    }


def format_usage_report(session_id: str) -> str:
    """格式化用量报告为 Slack 友好的文本

    Returns:
        格式化后的用量报告文本
    """
    summary = get_session_summary(session_id)
    if not summary["records"]:
        return "No usage data recorded."

    lines = ["*Token Usage Breakdown:*"]
    for r in summary["records"]:
        agent = r["agent"]
        model = r["model"]
        if r["image_count"] > 0:
            lines.append(f"  • `{agent}` ({model}): {r['image_count']} images — ${r['cost']:.4f}")
        else:
            total_tokens = r["prompt_tokens"] + r["completion_tokens"]
            lines.append(f"  • `{agent}` ({model}): {total_tokens:,} tokens — ${r['cost']:.4f}")

    lines.append(f"\n*Total Cost: ${summary['total_cost']:.4f}*")
    lines.append(f"  Tokens: {summary['total_prompt_tokens']:,} in + {summary['total_completion_tokens']:,} out")
    if summary["total_images"] > 0:
        lines.append(f"  Images: {summary['total_images']}")

    return "\n".join(lines)


def save_to_disk() -> None:
    """将内存中的用量记录持久化到磁盘"""
    store_path = cfg.STORE_DIR / "usage.json"

    with _lock:
        data = dict(_sessions)

    try:
        # 读取已有数据并合并
        existing = {}
        if store_path.exists():
            with open(store_path, "r", encoding="utf-8") as f:
                existing = json.load(f)

        existing.update(data)

        with open(store_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

        log.info("用量数据已保存: %d 个会话", len(data))
    except Exception as e:
        log.error("保存用量数据失败: %s", e)
