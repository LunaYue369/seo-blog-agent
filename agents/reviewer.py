"""审稿员 Agent — 多轮审核 SEO 博客质量

职责：
1. 评估博客的 SEO 质量、可读性、关键词密度
2. 检查内容安全性（虚假承诺、违规用语等）
3. 给出评分和具体修改建议
4. 评分低于阈值则打回 Copywriter 重写
"""

import json
import logging

from agents.soul_loader import build_system_prompt
from core import merchant_config
from llm import route

log = logging.getLogger(__name__)


def review_blog(
    merchant_id: str,
    blog_data: dict,
    topic: dict,
    round_num: int = 1,
) -> tuple[dict, dict]:
    """审核博客草稿，返回评分和反馈

    Args:
        merchant_id: 商家标识
        blog_data: 博客数据 {title, content_html, excerpt, tags, seo_slug}
        topic: 原始选题 {primary_keyword, secondary_keywords, ...}
        round_num: 当前审核轮次

    Returns:
        (feedback, token_usage)
        feedback: {score, passed, issues, suggestions, details}
    """
    system_prompt = build_system_prompt(merchant_id, "reviewer")

    primary_kw = topic.get("primary_keyword", "")
    secondary_kws = topic.get("secondary_keywords", [])

    user_prompt = f"""Review the following SEO blog post draft (Round {round_num}).

                    ## Target Keywords
                    - Primary: {primary_kw}
                    - Secondary: {', '.join(secondary_kws)}

                    ## Blog Title
                    {blog_data.get('title', '')}

                    ## Blog Excerpt
                    {blog_data.get('excerpt', '')}

                    ## Blog Content (HTML)
                    {blog_data.get('content_html', '')}

                    ## SEO Slug
                    {blog_data.get('seo_slug', '')}

                    ## Tags
                    {', '.join(blog_data.get('tags', []))}

                    ## Review Checklist — Score Each Area (0-100)

                    ### 1. SEO Technical (30% weight)
                    - Primary keyword in title, H2s, first paragraph, and conclusion?
                    - Secondary keywords used naturally?
                    - Meta description (excerpt) compelling and under 160 chars?
                    - URL slug SEO-friendly?
                    - Proper heading hierarchy (H2 → H3)?

                    ### 2. Content Quality (40% weight)
                    - Expert-level depth with specific details, numbers, actionable advice?
                    - Engaging opening hook?
                    - Scannable structure (short paragraphs, lists, subheadings)?
                    - Strong CTA at the end?
                    - No fluff or generic filler paragraphs?

                    ### 3. Readability (15% weight)
                    - Clear, professional language?
                    - Appropriate for the target audience?
                    - Good flow between sections?
                    - Varied sentence structure?

                    ### 4. Brand Safety (15% weight)
                    - No false claims or guarantees?
                    - No competitor bashing?
                    - Factually accurate?
                    - Professional tone maintained?

                    ## Output Format (JSON)
                    Return ONLY valid JSON:
                    {{
                        "score": 85,
                        "breakdown": {{
                            "seo_technical": 90,
                            "content_quality": 80,
                            "readability": 88,
                            "brand_safety": 95
                        }},
                        "passed": true,
                        "issues": [
                            "Specific issue 1 that needs fixing",
                            "Specific issue 2"
                        ],
                        "suggestions": [
                            "Actionable suggestion 1",
                            "Actionable suggestion 2"
                        ],
                        "highlights": [
                            "What the draft does well 1",
                            "What the draft does well 2"
                        ]
                    }}

                    Set "passed" to true only if score >= 80. Be strict but fair."""

    # 走统一路由层：task="review" → 便宜 OpenAI 档（gpt-4.1-mini，与原行为一致）。
    # label 保留 reviewer_rN 细粒度报表；记账由 complete() 内部自动完成，不再手动 record_usage。
    mcfg = merchant_config.get_merchant(merchant_id) or {"merchant_id": merchant_id}
    result = route("review", mcfg).complete(
        system=system_prompt,
        user=user_prompt,
        temperature=0.3,
        max_tokens=1500,
        json_mode=True,
        label=f"reviewer_r{round_num}",
    )
    # 回传 usage 字典保持调用方契约（多数调用方其实丢弃它，记账已在 complete 内完成）
    usage = {
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cost": result.cost,
    }

    try:
        feedback = json.loads(result.text)
    except json.JSONDecodeError:
        log.error("[%s] Reviewer JSON 解析失败 (round %d)", merchant_id, round_num)
        feedback = {"score": 0, "passed": False, "issues": ["JSON parse error"], "suggestions": []}

    score = feedback.get("score", 0)
    passed = feedback.get("passed", False)
    log.info("[%s] Reviewer 评分: %d/100 (passed=%s, round=%d)",
             merchant_id, score, passed, round_num)

    return feedback, usage
