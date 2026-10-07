"""llm.providers — 各厂商 SDK 的适配层

每个 provider 只实现 base.LLMClient._raw_complete()，把该厂商的调用方式和响应结构
抹平成归一化的 (text, prompt_tokens, completion_tokens)。router 按模型名前缀选用。

新增厂商步骤：
  1) 在这里加 xxx_provider.py，写一个 LLMClient 子类，实现 _raw_complete()
  2) 在 router._pick_provider 加一条前缀规则，在 router._PROVIDERS 注册类
  → 上层 agent 一行都不用改
"""
