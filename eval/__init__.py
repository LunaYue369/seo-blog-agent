"""eval — 稿件质量评估层（三层评估的地基）

## 为什么单独开一个包，而不是塞进 agents/
`agents/` 里的每个模块都是「一次 LLM 调用」的封装（copywriter/reviewer/artist…），
它们的共同特征是：**花钱、慢、结果不确定**。
`eval/` 里的东西性质完全相反：**零成本、毫秒级、同样输入永远同样输出**。
两类东西混在一起，最直接的坏处是没法给后者写单元测试（跑测试要 API key + 要联网 +
每次结果还不一样）。分开之后 `eval/` 可以在 CI 里跑，`agents/` 不行。

## 三层评估（本包只实现第①层）
  ① 确定性校验器  ← 本包 deterministic.py。能用代码算出真假的，绝不问 LLM。
  ② LLM 裁判      ← 现有 agents/reviewer.py（同族自评，有偏差）+ 未来的换族裁判
                     （config.DEFAULT_ROUTING["judge"] = claude-sonnet-5，路由已就位）
  ③ 人审 / 线上反馈 ← GSC / GA4 / WP 编辑改动率 / 询盘归因（目前只在 roadmap 里）

排列顺序不是随便排的，是 **cascaded evaluation + early rejection**：
便宜的过滤器放前面，贵的放后面；前面能否决的，后面根本不用跑。

## 对外只暴露一个入口
    from eval.deterministic import check_seo
调用方（目前只有 pipeline/review_loop.py 一处）不需要知道里面有几项检查。
"""
