"""Web 内容调研器 — 选题确定后搜索竞品文章，提取要点供 Copywriter 参考

解决问题：
  - Copywriter 纯靠 GPT 编内容，缺乏真实数据和竞品参考
  - 生成的内容千篇一律，没有差异化视角

方案：
  1. 用 Google Search 搜索选题关键词
  2. 爬取排名前几的文章正文
  3. 用 LLM 提炼竞品文章的核心要点、数据、独特观点
  4. 将调研结果注入 Copywriter prompt，提升内容深度和真实性

注意：
  - 只提取要点和数据，不复制原文（避免抄袭）
  - 爬取失败不阻断流水线（降级为无调研模式）
  - 总超时控制在 30 秒内，不拖慢生成速度
"""

import json
import logging
import re
import urllib.request
import urllib.error
import urllib.parse
from html.parser import HTMLParser

from core import merchant_config
from llm import route

log = logging.getLogger(__name__)

# 爬取配置
_MAX_PAGES = 3          # 最多爬取几个页面
_MAX_CHARS_PER_PAGE = 5000  # 每个页面最多提取多少字符
_FETCH_TIMEOUT = 8      # 单个页面爬取超时（秒）


# ── HTML 文本提取器 ───────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """简易 HTML → 纯文本提取器，跳过 script/style 标签"""

    def __init__(self):
        super().__init__()
        self._text_parts: list[str] = []
        self._skip = False  # 当前是否在 script/style 内部

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self._text_parts.append(text)

    def get_text(self) -> str:
        return " ".join(self._text_parts)


def _html_to_text(html: str) -> str:
    """将 HTML 转换为纯文本（去掉标签、脚本等）"""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        return extractor.get_text()
    except Exception:
        # HTMLParser 偶尔会遇到畸形 HTML
        return re.sub(r'<[^>]+>', ' ', html)


# ── 搜索 ─────────────────────────────────────────────────────

# 两个引擎共用的结果过滤名单（社交/文档/百科不适合当竞品正文来源）
_SKIP_DOMAINS = (
    "google.com", "youtube.com", "facebook.com", "twitter.com",
    "duckduckgo.com", ".pdf", ".doc", "wikipedia.org",
)

# 桌面浏览器 UA —— 两个引擎都需要，否则大概率被拒或拿到简版页面
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _keep_url(url: str, collected: list[str]) -> bool:
    """判断一个搜索结果链接是否值得爬（过滤名单 + 去重）。"""
    if any(skip in url for skip in _SKIP_DOMAINS):
        return False
    return url not in collected


def _google_search_urls(query: str, num_results: int = 5) -> list[str]:
    """用 Google 搜索关键词，返回结果页面 URL 列表

    使用简单的 Google Search 爬取（不需要 API key）。
    解析搜索结果页中的链接。

    Args:
        query: 搜索关键词
        num_results: 期望返回的链接数量

    Returns:
        URL 列表
    """
    encoded = urllib.parse.quote(query)
    url = f"https://www.google.com/search?q={encoded}&num={num_results}&hl=en"

    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept-Language": "en-US,en;q=0.9",
    })

    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("Google 搜索失败 [%s]: %s", query, exc)
        return []

    # 从搜索结果页提取链接（Google 把真实 URL 放在 /url?q= 参数里）
    urls: list[str] = []
    # 匹配 href="/url?q=https://..." 格式
    for match in re.finditer(r'href="/url\?q=(https?://[^"&]+)', html):
        found_url = urllib.parse.unquote(match.group(1))
        if _keep_url(found_url, urls):
            urls.append(found_url)
        if len(urls) >= num_results:
            break

    log.info("Google 搜索 [%s]: 找到 %d 个结果链接", query, len(urls))
    return urls


def _ddg_search_urls(query: str, num_results: int = 5) -> list[str]:
    """DuckDuckGo HTML 版搜索 —— Google 抓不到时的兜底引擎。

    为什么需要兜底：
      `_google_search_urls` 是直接爬 google.com/search 的 HTML 再正则抠
      `href="/url?q=..."`。Google 一旦弹验证码、改版、或按 IP 限流，
      这个正则就一条也匹配不到 → 返回 []。而 `research_topic` 对空结果的处理是
      静默返回 ""（降级为无调研模式），于是**所有文章都悄悄失去竞品数据**，
      只有翻日志才能发现。加一个协议完全不同的第二引擎能显著降低同时挂掉的概率。

    DDG 的 HTML 端点把真实 URL 包在 `/l/?uddg=<urlencoded>` 里，也可能直出裸链，
    两种形状都解析。

    Args:
        query: 搜索关键词
        num_results: 期望返回的链接数量

    Returns:
        URL 列表（失败返回 []）
    """
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept-Language": "en-US,en;q=0.9",
    })

    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("DuckDuckGo 搜索失败 [%s]: %s", query, exc)
        return []

    urls: list[str] = []
    # 形状①：/l/?uddg=https%3A%2F%2Fexample.com%2Fpost  （DDG 跳转包装）
    # 形状②：result__a 直接给裸链
    patterns = (
        r'uddg=([^"&]+)',
        r'class="result__a"[^>]*href="(https?://[^"]+)"',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html):
            found_url = urllib.parse.unquote(match.group(1))
            if not found_url.startswith("http"):
                continue
            if _keep_url(found_url, urls):
                urls.append(found_url)
            if len(urls) >= num_results:
                break
        if len(urls) >= num_results:
            break

    log.info("DuckDuckGo 搜索 [%s]: 找到 %d 个结果链接", query, len(urls))
    return urls


def _search_urls(query: str, num_results: int = 5) -> list[str]:
    """统一搜索入口：先 Google，空了自动换 DuckDuckGo。

    Returns:
        URL 列表；两个引擎都失败才返回 []（此时上层会降级为无调研模式）
    """
    urls = _google_search_urls(query, num_results=num_results)
    if urls:
        return urls

    log.warning("Google 无结果，切换 DuckDuckGo 兜底 [%s]", query)
    return _ddg_search_urls(query, num_results=num_results)


# ── 页面内容爬取 ─────────────────────────────────────────────

def _fetch_page_text(url: str) -> str:
    """爬取单个网页的正文文本

    Args:
        url: 页面 URL

    Returns:
        提取后的纯文本（截断到 MAX_CHARS_PER_PAGE）
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })

    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            # 只读取前 200KB，避免大页面卡住
            raw = resp.read(200_000)
            # 尝试从 Content-Type 获取编码
            content_type = resp.headers.get("Content-Type", "")
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
            else:
                charset = "utf-8"
            html = raw.decode(charset, errors="replace")
    except Exception as exc:
        log.warning("爬取页面失败 [%s]: %s", url[:80], exc)
        return ""

    text = _html_to_text(html)
    # 截断到最大字符数
    return text[:_MAX_CHARS_PER_PAGE]


# ── LLM 提炼要点 ─────────────────────────────────────────────

def _summarize_research(
    merchant_id: str,
    topic_title: str,
    primary_keyword: str,
    page_texts: list[dict],
) -> tuple[str, dict]:
    """用 LLM 提炼竞品文章的核心要点

    Args:
        merchant_id: 商家标识
        topic_title: 博客主题标题
        primary_keyword: 主关键词
        page_texts: [{url, text}, ...] 爬取到的页面内容

    Returns:
        (research_brief, token_usage)
        research_brief: 纯文本的调研摘要，可直接注入 copywriter prompt
    """
    # 拼接竞品文章内容
    articles_text = ""
    for i, page in enumerate(page_texts, 1):
        articles_text += f"\n--- Article {i} (from: {page['url'][:80]}) ---\n"
        articles_text += page["text"][:3000]  # 每篇最多 3000 字符
        articles_text += "\n"

    user_prompt = f"""I'm writing an SEO blog post titled: "{topic_title}"
Target keyword: "{primary_keyword}"

Below are excerpts from top-ranking competitor articles on this topic. Analyze them and extract:

1. **Key Data Points** — specific numbers, statistics, costs, measurements mentioned
2. **Unique Angles** — perspectives or arguments that stand out
3. **Common Structure** — what sections/topics do most articles cover?
4. **Content Gaps** — what do these articles MISS that our article should include?
5. **Expert Claims** — any authority-building facts, certifications, or industry standards mentioned

## Competitor Articles
{articles_text}

## Output Instructions
- Return a concise research brief (300-500 words)
- Focus on FACTS and DATA, not opinions
- Do NOT copy any sentences verbatim — paraphrase and synthesize
- Highlight anything that could make our article BETTER than these competitors
- Format as plain text with bullet points, ready to be injected into a copywriter's brief"""

    try:
        # 走统一路由层：task="research" → gpt-4.1-mini（与原 RESEARCH_MODEL 一致）。
        # 纯文本输出（非 JSON），记账自动完成。
        mcfg = merchant_config.get_merchant(merchant_id) or {"merchant_id": merchant_id}
        result = route("research", mcfg).complete(
            system="You are a content research analyst. Extract key insights from competitor articles to help write a superior blog post.",
            user=user_prompt,
            temperature=0.3,
            max_tokens=1500,
            label="web_researcher",
        )
        usage = {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost": result.cost,
        }

        brief = result.text.strip()
        log.info("[%s] Web Research 提炼完成: %d 字符", merchant_id, len(brief))
        return brief, usage

    except Exception as exc:
        log.error("[%s] Web Research LLM 调用失败: %s", merchant_id, exc)
        return "", {}


# ── 公共 API ─────────────────────────────────────────────────

def research_topic(
    merchant_id: str,
    topic: dict,
) -> str:
    """对已选定的主题进行 Web 调研，返回调研摘要

    整个流程：Google 搜索 → 爬取前 N 个页面 → LLM 提炼要点
    失败时静默降级（返回空字符串），不阻断博客生成流水线。

    Args:
        merchant_id: 商家标识
        topic: 选题字典 {title, primary_keyword, ...}

    Returns:
        调研摘要文本（可直接注入 copywriter prompt），失败时返回空字符串
    """
    title = topic.get("title", "")
    primary_kw = topic.get("primary_keyword", "")

    if not primary_kw:
        log.warning("[%s] Web Research 跳过：没有主关键词", merchant_id)
        return ""

    log.info("[%s] Web Research 开始: '%s'", merchant_id, primary_kw)

    try:
        # Step 1: 搜索（Google → DuckDuckGo 兜底）
        urls = _search_urls(primary_kw, num_results=5)
        if not urls:
            # 两个引擎都拿不到 = 大概率被限流/改版，这是需要人看一眼的信号，
            # 所以用 error 级别（warning 太容易被日志噪音埋掉）
            log.error("[%s] Web Research: Google 和 DuckDuckGo 均无结果，本篇降级为无竞品调研 [%s]",
                      merchant_id, primary_kw)
            return ""

        # Step 2: 爬取前 N 个页面
        page_texts = []
        for url in urls[:_MAX_PAGES]:
            text = _fetch_page_text(url)
            if text and len(text) > 200:  # 过滤掉太短的页面（可能是反爬页面）
                page_texts.append({"url": url, "text": text})

        if not page_texts:
            log.warning("[%s] Web Research: 所有页面爬取失败", merchant_id)
            return ""

        log.info("[%s] Web Research: 成功爬取 %d 个页面", merchant_id, len(page_texts))

        # Step 3: LLM 提炼要点
        brief, _ = _summarize_research(merchant_id, title, primary_kw, page_texts)
        return brief

    except Exception as exc:
        log.error("[%s] Web Research 整体失败: %s", merchant_id, exc)
        return ""
