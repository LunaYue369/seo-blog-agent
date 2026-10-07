"""图片 Prompt 美化师 Agent — 将简单描述转化为高质量 Seedream 生图 prompt

参考 digital-media-worker 的 media_engineer 人格，为博客配图生成
专业的、高大上的 AI 图片 prompt。
"""

import json
import logging

from agents.soul_loader import build_system_prompt
from core import merchant_config
from llm import route

log = logging.getLogger(__name__)


def enhance_image_prompts(
    merchant_id: str,
    image_prompts: dict[str, str],
    blog_title: str,
    blog_excerpt: str,
) -> tuple[dict[str, str], dict]:
    """美化博客配图的 Seedream prompt

    将 Copywriter 生成的简单图片描述优化为高质量的 Seedream 生图 prompt，
    确保生成的图片专业、高大上、适合企业级博客。

    Args:
        merchant_id: 商家标识
        image_prompts: Copywriter 输出的原始 prompt {"hero": "...", "mid": "...", "end": "..."}
        blog_title: 博客标题（提供上下文）
        blog_excerpt: 博客摘要（提供上下文）

    Returns:
        (enhanced_prompts, token_usage)
        enhanced_prompts: {"hero": "optimized prompt", "mid": "...", "end": "..."}
    """
    system_prompt = build_system_prompt(merchant_id, "artist")

    # 动态构建 prompt 列表（兼容 hero/mid/end 和 img_1/img_2/.../img_N）
    prompt_lines = []
    for slot, prompt in image_prompts.items():
        label = {"hero": "Hero (banner)", "mid": "Mid-article", "end": "End-article (CTA)"}.get(slot, f"Image {slot}")
        prompt_lines.append(f"- **{label}:** {prompt or 'No prompt provided'}")
    prompts_text = "\n                    ".join(prompt_lines)

    # 动态构建期望的输出 key
    output_keys = ", ".join(f'"{k}": "Enhanced prompt..."' for k in image_prompts.keys())

    user_prompt = f"""Enhance the following image prompts for a professional business blog.

                    ## Blog Context
                    - **Title:** {blog_title}
                    - **Summary:** {blog_excerpt}

                    ## Original Image Prompts (from copywriter)
                    {prompts_text}

                    ## Enhancement Requirements
                    1. Each prompt should be 80-150 words, highly detailed
                    2. Style: Professional, high-end, corporate-quality photography or 3D rendering
                    3. Include specific details: lighting, composition, color palette, perspective, texture
                    4. NO text, logos, watermarks, or brand names in the image
                    5. The first image should be the most visually striking — wide composition (16:9 friendly)
                    6. Middle images should illustrate specific concepts from the article
                    7. The last image should evoke trust and professionalism (supports CTA)

                    ## Output Format (JSON)
                    Return ONLY valid JSON with these exact keys:
                    {{{output_keys}}}"""

    mcfg = merchant_config.get_merchant(merchant_id) or {"merchant_id": merchant_id}
    result = route("artist", mcfg).complete(
        system=system_prompt,
        user=user_prompt,
        temperature=0.6,
        max_tokens=1200,
        json_mode=True,
        label="artist",
    )
    usage = {
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cost": result.cost,
    }

    try:
        enhanced = json.loads(result.text)
    except json.JSONDecodeError:
        log.error("[%s] Artist JSON 解析失败", merchant_id)
        enhanced = image_prompts  # 失败时回退到原始 prompt

    slot_lengths = ", ".join(f"{k}={len(v)}字" for k, v in enhanced.items())
    log.info("[%s] Artist 美化完成: %s", merchant_id, slot_lengths)

    return enhanced, usage
