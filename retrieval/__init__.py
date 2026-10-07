"""retrieval — 把「外部事实」取出来喂给 prompt 的那一层（RAG 的 R）

## 为什么单独开一个包
`agents/` 是"问模型"，`eval/` 是"验模型"，`retrieval/` 是**"给模型事实"**。
三者的失效方式完全不同，混在一起会让人分不清一个错误该去哪儿修：
  · 文章里价格错了 → 是检索给错了（retrieval），还是模型没照着写（agents），
    还是校验没拦住（eval）？分成三个包之后，这个问题永远有明确答案。

## 目前只有一个来源：商品目录
`catalog.py` —— 从 `merchants/{id}/catalog.json` 读商品，按关键词重叠挑 top-N
注入 copywriter 的 prompt。**刻意不用向量库**，理由写在 catalog.json 的
`_why_no_vector_db` 字段里（十几条结构化数据引入 embedding 是过度工程）。

## 和 eval 的分工（RAG 里最容易被忽略的一半）
检索只做**预防**：把正确的事实摆在模型面前，提高它写对的概率。
它**不提供保证** —— 模型完全可以看着 $32 的目录写出 $29。
所以每一条注入 prompt 的事实，都要在 `eval/` 里有一条对应的出口校验：

    retrieval 注入价格  →  eval.check_catalog_grounding 核对价格

这就是「grounding + attribution + verification」三件套里的第一件和第三件。
**只有出口校验是保证，前面两件都只是预防。**
"""
