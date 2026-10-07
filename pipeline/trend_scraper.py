"""热词爬取器 — 从 Google Search Suggest 抓取 SEO 热词

通过 Google 自动补全接口获取用户真实搜索关键词，
这些词条是按全球搜索频率排序的，SEO 价值最高。

统一输出格式: [{keyword, source, context}, ...]
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import urllib.parse

log = logging.getLogger(__name__)


def scrape_google_suggest(seed_keywords: list[str], max_per_seed: int = 8) -> list[dict]:
    """通过 Google 自动补全 API 获取用户真实搜索关键词

    对每个种子词调用 Google Suggest，获取相关的长尾关键词。

    Args:
        seed_keywords: 种子关键词列表
        max_per_seed: 每个种子词最多获取的建议数量

    Returns:
        热搜词条：[{热搜词条的 keyword, source, context}]
    """
    items: list[dict] = []
    seen: set[str] = set()

    # 对于每个种子词，比如 "paint protection film Tesla"
    for seed in seed_keywords:
        try:
            """
            在 Google 搜索框里输入 "paint protection film Tesla" 时，
            下拉框里会根据实时热度排列出相关热搜词。
            我们只需要知道这些热搜词是啥就行了，不需要访问每个热搜词对应的帖子。
            """
            # 拼装搜索 url
            encoded = urllib.parse.quote(seed)
            url = (
                f"https://suggestqueries.google.com/complete/search?"
                f"client=firefox&q={encoded}"
            )
            # 伪装成浏览器发请求，不然 Google 可能拒绝（返回 403）
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            """
            data 是 Google 返回的原始 JSON：
            ["paint protection film Tesla", ["paint protection film tesla model 3", "paint protection film tesla model y", ...]]
            suggestions：就是 suggestions list
            """
            # 搜索，拿到下拉菜单的数据
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            suggestions = data[1] if len(data) > 1 else []

            # 最多取前 8 个热搜词
            count = 0
            for suggestion in suggestions[:max_per_seed]:
                normalized = suggestion.strip().lower()
                # 记录该热搜词（去重）
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    items.append({
                        "keyword": suggestion.strip(),
                        "source": "google_suggest",
                        "context": f"Autocomplete for: {seed}",
                    })
                    count += 1

            log.info("Google Suggest [%s]: %d suggestions", seed, count)

        except Exception as exc:
            log.warning("Google Suggest '%s' failed: %s", seed, exc)
            continue

    log.info("Google Suggest total: %d keywords", len(items))
    return items


# ── 统一入口 ─────────────────────────────────────────────────

def scrape_trending(seed_keywords: list[str], max_total: int = 30) -> list[dict]:
    """统一热词爬取入口

    Args:
        seed_keywords: 商家的种子关键词列表
        max_total: 最大返回数量

    Returns:
        去重后的热词列表 [{热搜词条的keyword, source, context}, ...]，最多返回30个
    """
    all_items: list[dict] = []

    # 用Google Search Suggest查找热搜词条
    try:
        all_items = scrape_google_suggest(seed_keywords)
        log.info("Google Suggest: %d unique items", len(all_items))
    except Exception as exc:
        log.error("Google Suggest 整体失败: %s", exc)

    # 如果 Suggest 没拿到任何数据，用种子词兜底
    if not all_items:
        log.warning("未返回任何数据！使用种子词作为 fallback")
        for kw in seed_keywords[:max_total]:
            all_items.append({
                "keyword": kw,
                "source": "seed_keyword",
                "context": "Fallback: using seed keyword directly",
            })

    return all_items[:max_total]
