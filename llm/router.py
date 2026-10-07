"""LLM 路由器 — 「按任务选模型、按模型前缀选 provider」的唯一入口

这是整个 llm/ 地基的门面。上层 agent 只需要：

    from llm import route
    client = route("write", merchant_cfg)          # 我要为这个商家做「写稿」
    result = client.complete(system, user)         # 发请求，拿归一化结果

router 内部决策链（含中间产物）：

    task="write" + merchant_cfg
      │
      ├─ 1) 查模型名：merchant_cfg["routing"]["write"]      （商家覆盖，优先）
      │                └ 没有则 cfg.DEFAULT_ROUTING["write"]（全局默认）
      │                └ 再没有则报错（未知任务）
      │      产物：model = "gpt-4.1"
      │
      ├─ 2) 按前缀定 provider：model.startswith("claude") → anthropic，否则 openai
      │      产物：provider = "openai"
      │
      └─ 3) new 对应的 provider 客户端（绑定 task/model/merchant_id/cfg）
             产物：一个还没发请求的 LLMClient 实例

以后要接新厂商（Gemini / 本地 …）：
  只在 _pick_provider 里加一条前缀规则 + 加一个 providers/xxx_provider.py，
  上层 agent 一行都不用改——这就是「router 是接新模型的唯一缝隙」。
"""

from __future__ import annotations

import logging

import config as cfg
from llm.base import LLMClient
from llm.providers.openai_provider import OpenAIClient
from llm.providers.anthropic_provider import AnthropicClient

log = logging.getLogger(__name__)


def _resolve_model(task: str, merchant_cfg: dict) -> str:
    """查表得到该任务用哪个模型：商家 routing 覆盖 > 全局默认 > 报错。"""
    routing = (merchant_cfg or {}).get("routing") or {}
    model = routing.get(task) or cfg.DEFAULT_ROUTING.get(task)
    if not model:
        raise ValueError(
            f"未知任务 '{task}'：merchant.json 的 routing 和 config.DEFAULT_ROUTING 里都没有它。"
            f"（已知任务：{sorted(cfg.DEFAULT_ROUTING)}）"
        )
    return model


def _pick_provider(model: str) -> str:
    """按模型名前缀决定走哪个 provider。加新厂商就在这里加一条规则。"""
    name = model.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    # 兜底：默认当 OpenAI（多数自建/兼容端点也走 OpenAI 协议）
    log.warning("模型 %s 前缀无法识别 provider，默认按 openai 处理", model)
    return "openai"


# provider 名 → 客户端类
_PROVIDERS: dict[str, type[LLMClient]] = {
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
}


def route(task: str, merchant_cfg: dict) -> LLMClient:
    """按任务拿到一个配置好的 LLM 客户端（还没发请求）。

    Args:
        task:         任务 key，如 "write" / "judge" / "topic" / "chat" / …
        merchant_cfg: 该商家的完整配置（含 merchant_id / routing / eval.budget_usd）

    Returns:
        LLMClient 子类实例，调 .complete(system, user) 发请求。

    示例：
        client = route("judge", cfg)     # cfg["routing"]["judge"] 缺省 → claude-sonnet-5
        # → provider=anthropic，返回 AnthropicClient
        r = client.complete(system="你是严格的审稿人…", user="判这篇稿子…", json_mode=True)
        print(r.text, r.cost)
    """
    model = _resolve_model(task, merchant_cfg)
    provider = _pick_provider(model)
    merchant_id = (merchant_cfg or {}).get("merchant_id", "unknown")

    client_cls = _PROVIDERS[provider]
    return client_cls(
        task=task,
        model=model,
        merchant_id=merchant_id,
        merchant_cfg=merchant_cfg,
    )
