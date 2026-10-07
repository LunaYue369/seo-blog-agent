"""LLM 成本预算闸门 — 复用现有 usage_tracker 的记账，只加「预算上限」这一层

职责边界（不重复造轮子）：
  - 「算钱 + 记账」= 已有的 services/usage_tracker（价格表 + record_usage + session 汇总）
  - 「超预算就拦」= 本模块。只做一件事：查当前 session 已花多少，超了就抛异常。

为什么放在发请求之前查：
  预算是「硬上限」，先查不发才能真正防止烧钱；发完再查已经晚了。
  参考 [[feedback_rate-limit-order]] 的思路：先查不增、成后才增。

中间状态：
  预算来源     = merchant_cfg["eval"]["budget_usd"]（每篇生成的美元上限）
  已花金额     = usage_tracker.get_session_summary(当前session)["total_cost"]
  当前 session = usage_tracker.get_current_session()（blog_generator 生成开始时 set 的）
  判定         = 已花 >= 预算  → 抛 BudgetExceeded；否则放行
"""

from __future__ import annotations

import logging

from services import usage_tracker

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """该商家本次生成已达/超过预算上限。

    complete() 在真正调 SDK 之前抛这个，所以抛出时「这一笔钱还没花」。
    上层可以捕获它来优雅收尾（比如「已达预算，用当前草稿发布/停止重写循环」）。
    """


def _resolve_budget(merchant_cfg: dict) -> float | None:
    """从商家配置里取本次生成的预算上限（美元）。没配 = 不限（返回 None）。

    读取顺序（容忍两种写法）：
        merchant_cfg["eval"]["budget_usd"]   （推荐，跟 eval 配置放一起）
        merchant_cfg["budget_usd"]           （顶层简写）
    """
    eval_cfg = merchant_cfg.get("eval") or {}
    budget = eval_cfg.get("budget_usd", merchant_cfg.get("budget_usd"))
    if budget in (None, "", 0):
        return None
    try:
        return float(budget)
    except (TypeError, ValueError):
        log.warning("预算值无法解析，按不限处理: %r", budget)
        return None


def check_budget(merchant_cfg: dict, about_to_call: str = "") -> None:
    """预算闸门：若当前 session 已花 ≥ 预算，抛 BudgetExceeded；否则静默放行。

    Args:
        merchant_cfg:  商家配置（用于取预算）
        about_to_call: 即将发起的调用描述（如 "judge:claude-sonnet-5"），仅用于日志/异常信息

    Raises:
        BudgetExceeded: 已达预算上限。

    注意：没设预算、或没有活跃 session 时，直接放行（不拦）。
    """
    budget = _resolve_budget(merchant_cfg)
    if budget is None:
        return  # 未设预算 → 不限

    session_id = usage_tracker.get_current_session()
    if not session_id:
        return  # 没有活跃生成会话（例如单元测试/临时调用）→ 不拦

    spent = usage_tracker.get_session_summary(session_id).get("total_cost", 0.0)
    if spent >= budget:
        raise BudgetExceeded(
            f"本次生成已花 ${spent:.4f} ≥ 预算上限 ${budget:.4f}"
            f"（阻止调用 {about_to_call or '下一次 LLM'}）"
        )
    # 未超 → 放行（这里不记账，记账在 complete() 成功后由 usage_tracker 做）
