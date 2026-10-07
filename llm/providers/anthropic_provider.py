"""Anthropic provider — 把 Anthropic SDK 抹平成 base.LLMClient 接口

这是「换族裁判」的关键：写稿走 GPT，判稿走这里的 Claude，避免同模型自评偏差。

⚠️ 与 OpenAI 的三个关键差异（都在这里内部抹平，上层无感）：
  1) system 是独立顶层参数，不是 messages 里的一条（OpenAI 是拼进 messages[0]）。
  2) 采样参数 temperature/top_p/top_k 在 Sonnet 5 上传了会直接 400 → 这里绝不传 temperature。
  3) 响应是 content「块列表」，文本要从第一个 type=="text" 的块里抽；
     用量字段叫 input_tokens / output_tokens（不是 prompt/completion_tokens）。

响应结构映射（Anthropic → 归一化）：
  text              ← 第一个 type=="text" 的 content block 的 .text
  prompt_tokens     ← resp.usage.input_tokens
  completion_tokens ← resp.usage.output_tokens

thinking（adaptive thinking）：
  - thinking=False（默认）→ 传 {"type": "disabled"}：判稿要便宜 + 干净 JSON，关掉更合适。
    （Sonnet 5 若省略 thinking 会默认跑 adaptive，所以「不想思考」必须显式 disabled。）
  - thinking=True → 传 {"type": "adaptive"}：难任务让模型自己决定思考深度，质量更高但更贵。
"""

from __future__ import annotations

import logging

import config as cfg
from llm.base import LLMClient

log = logging.getLogger(__name__)


class AnthropicClient(LLMClient):
    """Anthropic 系模型（claude-sonnet-5 / claude-opus-4-8 …）的客户端。"""

    def __init__(self, task, model, merchant_id, merchant_cfg):
        super().__init__(task, model, merchant_id, provider="anthropic",
                         merchant_cfg=merchant_cfg)
        # 延迟导入 anthropic：没装这个包也不影响纯 OpenAI 流程（from llm import route 照常可用），
        # 只有真的路由到 Claude 判稿、走到这里时才要求装。
        try:
            import anthropic
        except ModuleNotFoundError as e:
            raise RuntimeError(
                f"路由到 Anthropic 模型 {model}（任务 {task}），但未安装 anthropic 包。"
                f"请在 venv 里 `pip install anthropic`。"
            ) from e
        if not cfg.ANTHROPIC_API_KEY:
            raise RuntimeError(
                f"路由到 Anthropic 模型 {model}（任务 {task}），但 ANTHROPIC_API_KEY 未配置。"
                f"（换族裁判需要它；不想用 Claude 判稿可把 merchant.json 的 routing.judge 改成 gpt-* 模型）"
            )
        self._client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

    def _raw_complete(self, *, system, user, max_tokens, temperature,
                      json_mode, thinking):
        # 单轮：system 走独立顶层字段，user 作为唯一 messages 条目。
        return self._call(
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens, thinking=thinking,
        )

    def _raw_complete_messages(self, *, messages, max_tokens, temperature,
                               json_mode, thinking):
        # 多轮：Anthropic 的 system 是独立顶层字段，messages 里不能塞 system 角色。
        # 所以把「开头连续的 system 消息」抽出来合并成 system 字符串，其余按顺序作为对话。
        system_parts = []
        chat_messages = []
        seen_non_system = False
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system" and not seen_non_system:
                # 只合并「靠前」的 system（多数场景 system 都在最前面）
                system_parts.append(content)
            elif role == "system":
                # 对话中间又冒出 system（少见）→ 降级并进 user，避免 Anthropic 报错
                seen_non_system = True
                chat_messages.append({"role": "user", "content": f"[system] {content}"})
            else:
                seen_non_system = True
                chat_messages.append({"role": role, "content": content})

        if not chat_messages:
            # 兜底：全是 system 时，至少给一条 user，否则 Anthropic 400
            chat_messages = [{"role": "user", "content": "Continue."}]

        return self._call(
            system="\n\n".join(p for p in system_parts if p),
            messages=chat_messages,
            max_tokens=max_tokens, thinking=thinking,
        )

    # ── 实际发请求（两个入口共用）──────────────────────────────
    def _call(self, *, system, messages, max_tokens, thinking):
        # ⚠️ 故意不传 temperature：Sonnet 5 传采样参数会 400，在这里被吞掉。
        thinking_cfg = {"type": "adaptive"} if thinking else {"type": "disabled"}

        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,           # Anthropic 必填
            system=system,                   # 独立顶层 system（≠ messages 里的一条）
            thinking=thinking_cfg,
            messages=messages,
        )

        # 从 content 块列表里抽第一个 text 块；没有则空串（例如被安全分类器 refusal）
        text = next((b.text for b in resp.content if b.type == "text"), "")
        pt = resp.usage.input_tokens        # ← 注意字段名，不是 prompt_tokens
        ct = resp.usage.output_tokens       # ← 不是 completion_tokens
        return text, pt, ct
