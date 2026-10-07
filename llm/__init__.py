"""llm/ — 模型路由控制平面（地基层）

对上层只暴露这几个名字，agent 侧只需要 `from llm import route`：

    route(task, merchant_cfg) -> LLMClient   # 按任务选模型/厂商，拿客户端
    LLMClient.complete(system, user) -> LLMResult
    LLMResult                                 # 归一化结果（text + 用量 + 溯源）
    BudgetExceeded                            # 预算超限异常（供上层优雅收尾）

内部结构：
    base.py                 统一接口 + 记账/预算模板（模板方法）
    cost.py                 预算闸门（复用 usage_tracker 记账）
    router.py               按任务选模型、按前缀选 provider
    providers/openai_provider.py     OpenAI SDK 适配
    providers/anthropic_provider.py  Anthropic SDK 适配（换族裁判）
"""

from llm.base import LLMClient, LLMResult
from llm.cost import BudgetExceeded
from llm.router import route

__all__ = ["route", "LLMClient", "LLMResult", "BudgetExceeded"]
