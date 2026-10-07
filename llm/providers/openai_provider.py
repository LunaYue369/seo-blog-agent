"""OpenAI provider — 把 OpenAI SDK 抹平成 base.LLMClient 接口

只实现 _raw_complete()，负责：
  1) 调 openai 的 chat.completions.create
  2) 从响应结构里抽出 (text, prompt_tokens, completion_tokens) 归一化三元组

响应结构映射（OpenAI → 归一化）：
  text              ← resp.choices[0].message.content
  prompt_tokens     ← resp.usage.prompt_tokens
  completion_tokens ← resp.usage.completion_tokens
"""

from __future__ import annotations

import logging

from openai import OpenAI

import config as cfg
from llm.base import LLMClient

log = logging.getLogger(__name__)


class OpenAIClient(LLMClient):
    """OpenAI 系模型（gpt-4.1 / gpt-4.1-mini / gpt-5.2 …）的客户端。"""

    def __init__(self, task, model, merchant_id, merchant_cfg):
        super().__init__(task, model, merchant_id, provider="openai",
                         merchant_cfg=merchant_cfg)
        # key 缺失就 fail-fast，别静默降级（参考 [[feedback_no-backward-compat-shims]]）
        if not cfg.OPENAI_API_KEY:
            raise RuntimeError(
                f"路由到 OpenAI 模型 {model}（任务 {task}），但 OPENAI_API_KEY 未配置"
            )
        self._client = OpenAI(api_key=cfg.OPENAI_API_KEY)

    def _raw_complete(self, *, system, user, max_tokens, temperature,
                      json_mode, thinking):
        # 单轮：拼成标准的 [system, user] 两条 messages，复用 _raw_complete_messages。
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._raw_complete_messages(
            messages=messages, max_tokens=max_tokens, temperature=temperature,
            json_mode=json_mode, thinking=thinking,
        )

    def _raw_complete_messages(self, *, messages, max_tokens, temperature,
                               json_mode, thinking):
        # thinking 参数对 OpenAI 无意义，忽略（只有 Anthropic 用）。OpenAI 直接吃 messages 数组。
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # JSON 模式：OpenAI 用 response_format 强制返回合法 JSON 对象。
        # 注意（[[feedback_openai-json-response-format]]）：还必须在 prompt 里写明返回 JSON，
        # 且 max_tokens 要给够（≥1200）防截断——这两点由调用方在 prompt/参数里保证。
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = self._client.chat.completions.create(**kwargs)

        text = resp.choices[0].message.content or ""
        pt = resp.usage.prompt_tokens
        ct = resp.usage.completion_tokens
        return text, pt, ct
