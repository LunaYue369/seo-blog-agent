"""LLM 统一接口层 — 把不同厂商（OpenAI / Anthropic / …）抹平成同一个 complete()

设计目标：上层所有 agent（researcher / copywriter / reviewer / …）只认这里的
`LLMClient.complete()`，完全不关心底下是 GPT 还是 Claude、调用方式和返回结构有啥不同。
这样才能做到「换族裁判」和「以后加任何模型 agent 一行都不用改」。

调用链上的中间产物（含状态）：

  route("judge", cfg)                          # router 决定 model + provider
    → 返回一个 LLMClient 子类实例（已绑定 task/model/merchant_id/provider）
    → 此时还没发任何请求，只是一个「配置好的客户端」

  client.complete(system, user)                # 真正发请求
    ├─ 1) 预算检查：cost.check_budget(cfg)      # 超支则抛 BudgetExceeded，请求根本不发
    ├─ 2) _raw_complete(...)                    # 子类实现，调各家 SDK
    │      产物 = (text, prompt_tokens, completion_tokens)  ← 已归一化的三元组
    ├─ 3) usage_tracker.record_usage(...)       # 自动记账（成本 + token），不用 agent 手动记
    └─ 4) 组装并返回 LLMResult                   ← 最终产物，含 text + 用量 + 溯源信息

失败态：
  - 预算超支      → 抛 cost.BudgetExceeded（在 _raw_complete 之前，不烧钱）
  - SDK 调用异常  → 由 _raw_complete 内部抛出（各 provider 各自处理/包装）
  - key 没配      → provider 构造时抛 RuntimeError（fail-fast，不静默降级）
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Any

from services import usage_tracker
from llm import cost

log = logging.getLogger(__name__)


@dataclass
class LLMResult:
    """一次 complete() 的最终产物 —— 已经和厂商无关的归一化结果。

    示例（judge 任务，Claude Sonnet 5 返回）:
        LLMResult(
            text='{"ai_slop": 0.02, "verdict": "human-like", ...}',
            prompt_tokens=1820,
            completion_tokens=240,
            model="claude-sonnet-5",
            provider="anthropic",
            task="judge",
            cost=0.0091,           # 由 usage_tracker 按价格表算好回填
        )
    """
    text: str                      # 模型输出的纯文本（已从各家响应结构里抽出来）
    prompt_tokens: int             # 输入 token（各家字段名不同，这里统一）
    completion_tokens: int         # 输出 token
    model: str                     # 实际使用的模型名，如 "claude-sonnet-5"
    provider: str                  # "openai" | "anthropic"
    task: str                      # 触发本次调用的任务 key，如 "judge"
    cost: float = 0.0              # 本次调用预估成本（美元），complete() 里回填
    raw: Any = None                # 原始响应对象，仅供调试；上层正常不该碰


class LLMClient(abc.ABC):
    """所有 provider 客户端的基类（模板方法模式）。

    子类只需实现 `_raw_complete()`（各家 SDK 的具体调用 + 抽取文本/用量）；
    预算检查、记账、组装 LLMResult 这些「公共动作」都在这里做一遍，
    保证所有 provider 行为一致，agent 侧零差别。
    """

    def __init__(self, task: str, model: str, merchant_id: str,
                 provider: str, merchant_cfg: dict) -> None:
        """
        Args:
            task:         任务 key（"write"/"judge"/…），用于记账的 agent 维度 + 预算
            model:        router 解析出的模型名，如 "gpt-4.1" / "claude-sonnet-5"
            merchant_id:  商家 id，用于用量归属
            provider:     "openai" | "anthropic"
            merchant_cfg: 该商家的完整配置（含 routing / eval.budget_usd 等）
        """
        self.task = task
        self.model = model
        self.merchant_id = merchant_id
        self.provider = provider
        self.merchant_cfg = merchant_cfg

    # ── 公共入口：所有 agent 都调这个，不碰 _raw_complete ──────────
    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4000,
        temperature: float = 0.7,
        json_mode: bool = False,
        thinking: bool = False,
        label: str = "",
    ) -> LLMResult:
        """发一次「系统提示 + 用户提示 → 文本」的补全请求。

        Args:
            system:      系统提示（人格/规则）。Anthropic 走独立的 system 字段，
                         OpenAI 拼进 messages[0]，provider 内部处理，调用方不用管。
            user:        用户提示（本轮真正的任务内容）。
            max_tokens:  输出上限。JSON 结构化输出建议 ≥1200（防截断）。
            temperature: 采样温度。⚠️ 只对 OpenAI 生效；
                         Anthropic 的 Sonnet 5 传 temperature 会 400，provider 内部会忽略它。
            json_mode:   是否要求返回严格 JSON。
                         OpenAI → response_format=json_object（需 prompt 里也写明返回 JSON）；
                         Anthropic → 暂靠 prompt 约束（不强制 schema），provider 内部按需处理。
            thinking:    是否开启 Claude 的 adaptive thinking（仅 Anthropic 有意义）。
                         默认 False = 关闭，判稿这种要便宜+干净 JSON 的场景更合适。
            label:       记账时用的 agent 名（覆盖默认的 task）。
                         用于保留细粒度报表，如 "reviewer_r1" / "copywriter_rewrite_r2"；
                         不传则用 self.task（如 "write" / "judge"）。

        Returns:
            LLMResult（见上）。

        Raises:
            cost.BudgetExceeded: 该商家本次生成已达预算上限（在发请求之前抛，不烧钱）。
        """
        # ── 1) 预算闸门：先查不发，超了直接抛 ──────────────────
        cost.check_budget(self.merchant_cfg, about_to_call=f"{self.task}:{self.model}")

        # ── 2) 交给子类真正调 SDK；产物 = 归一化三元组 ─────────
        text, pt, ct = self._raw_complete(
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
            thinking=thinking,
        )

        # ── 3+4) 记账 + 组装（与 complete_messages 共用）──────────
        return self._finish(text, pt, ct, label=label)

    def complete_messages(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 4000,
        temperature: float = 0.7,
        json_mode: bool = False,
        thinking: bool = False,
        label: str = "",
    ) -> LLMResult:
        """多轮对话入口 —— 接收完整 messages 数组（chat 模式用）。

        与 complete() 的区别：complete() 是「一条 system + 一条 user」的单轮；
        这个接收 OpenAI 风格的 messages 列表（可含多条 system + user/assistant 历史）。

        各 provider 内部适配（调用方无感）：
          - OpenAI：messages 原样传。
          - Anthropic：把开头连续的 system 消息合并成顶层 system 参数，
            其余 user/assistant 作为 messages（因为 Anthropic 的 system 是独立顶层字段）。

        Args:
            messages: [{"role": "system"/"user"/"assistant", "content": "..."}, ...]
            其余参数同 complete()。
        """
        cost.check_budget(self.merchant_cfg, about_to_call=f"{self.task}:{self.model}")
        text, pt, ct = self._raw_complete_messages(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
            thinking=thinking,
        )
        return self._finish(text, pt, ct, label=label)

    # ── 记账 + 组装 LLMResult（两个入口共用）──────────────────
    def _finish(self, text: str, pt: int, ct: int, *, label: str) -> LLMResult:
        # record_usage 内部会按 _PRICE_TABLE 算成本并累加到当前 session
        rec = usage_tracker.record_usage(
            merchant_id=self.merchant_id,
            agent=label or self.task,  # label 优先（保留 reviewer_r1 这种细粒度），否则用任务 key
            model=self.model,
            prompt_tokens=pt,
            completion_tokens=ct,
        )
        log.info("[%s] llm task=%s model=%s pt=%d ct=%d cost=$%.4f",
                 self.merchant_id, self.task, self.model, pt, ct, rec.get("cost", 0.0))
        return LLMResult(
            text=text,
            prompt_tokens=pt,
            completion_tokens=ct,
            model=self.model,
            provider=self.provider,
            task=self.task,
            cost=rec.get("cost", 0.0),
        )

    # ── 子类必须实现：各家 SDK 的具体调用 ──────────────────────
    @abc.abstractmethod
    def _raw_complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        thinking: bool,
    ) -> tuple[str, int, int]:
        """调用具体厂商 SDK，返回归一化的 (text, prompt_tokens, completion_tokens)。

        约定（每个 provider 都必须遵守，这样上层才能一视同仁）：
          - text:  从各家响应结构里抽出来的纯文本（OpenAI 的 choices[0].message.content /
                   Anthropic 的 content 里第一个 text block）
          - prompt_tokens / completion_tokens: 各家 usage 字段名不同，在这里统一
            （OpenAI: usage.prompt_tokens / completion_tokens；
              Anthropic: usage.input_tokens / output_tokens）
        """
        raise NotImplementedError

    @abc.abstractmethod
    def _raw_complete_messages(
        self,
        *,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        thinking: bool,
    ) -> tuple[str, int, int]:
        """多轮 messages 版的具体调用（complete_messages 用）。

        返回同 _raw_complete 的归一化三元组 (text, prompt_tokens, completion_tokens)。
        provider 负责把 messages 适配成各家所需结构（见 complete_messages 文档）。
        """
        raise NotImplementedError
