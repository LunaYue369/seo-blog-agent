"""配置加载器 — 从 .env 读取所有配置项

所有配置通过环境变量注入，支持 .env 文件自动加载。
频道与商家的映射关系在 merchants/{id}/merchant.json 中定义。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ── 项目根目录 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

# 加载 .env
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes")


def _env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


# ── Slack ────────────────────────────────────────────────────
SLACK_BOT_TOKEN: str = _env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN: str = _env("SLACK_APP_TOKEN")

# ── OpenAI ───────────────────────────────────────────────────
OPENAI_API_KEY: str = _env("OPENAI_API_KEY")
BLOG_MODEL: str = _env("BLOG_MODEL", "gpt-4.1")
RESEARCH_MODEL: str = _env("RESEARCH_MODEL", "gpt-4.1-mini")

# ── Anthropic（换族裁判用）──────────────────────────────────
# 只有做 eval「换族裁判」（GPT 写稿 → Claude 判稿）才需要这把 key。
# 没配也不影响写稿/发布等主流程；只有真的路由到 claude-* 模型时才会用到。
ANTHROPIC_API_KEY: str = _env("ANTHROPIC_API_KEY")

# ── LLM 任务 → 模型 默认路由表 ────────────────────────────────
# 这是「兜底默认」。每个商家可在 merchant.json 里写 "routing" 覆盖对应任务。
# router.route(task, merchant_cfg) 的查表顺序：
#   merchant_cfg["routing"][task]  →  DEFAULT_ROUTING[task]  →  报错
# 模型名前缀决定走哪个 provider：claude-* → anthropic，其余 → openai。
#
# 设计原则（面试可讲）：写稿留强模型、杂活用便宜模型、裁判必须换族。
DEFAULT_ROUTING: dict[str, str] = {
    "topic":    _env("MODEL_TOPIC",    "gpt-4.1-mini"),   # 选题：排序/挑选，便宜档
    "research": _env("MODEL_RESEARCH", "gpt-4.1-mini"),   # 深挖：资料整理，便宜档
    "write":    _env("MODEL_WRITE",    "gpt-4.1"),        # 写稿：质量命门（可升 gpt-5.2）
    "rewrite":  _env("MODEL_REWRITE",  "gpt-4.1"),        # 重写：同写稿
    "chat":     _env("MODEL_CHAT",     "gpt-4.1-mini"),   # chat 对话：中档够用
    "review":   _env("MODEL_REVIEW",   "gpt-4.1-mini"),   # 现有质量/SEO 审稿：便宜档（同 OpenAI）
    "judge":    _env("MODEL_JUDGE",    "claude-sonnet-5"),# ⭐ eval 换族裁判（后续 eval 模块用，非现有 reviewer）
    "artist":   _env("MODEL_ARTIST",   "gpt-4.1-mini"),   # 配图 prompt：短文本，便宜
    # ⭐ 定点修复：只重写小标题，不动正文。输入约 300 token、输出约 100 token，
    # 所以哪怕用便宜档也够用（实测一次 ≈ $0.0003，整篇重写是 $0.028，差两个数量级）。
    # 新能力配一个自己的任务键而不是复用 "rewrite"，是为了让它的成本档位**显式可见、
    # 且能按商家覆盖** —— 复用 rewrite 就会悄悄用上 gpt-4.1 那档，白花 10 倍的钱。
    "headings": _env("MODEL_HEADINGS", "gpt-4.1-mini"),   # 定点改标题：短文本，便宜
    "channel":  _env("MODEL_CHANNEL",  "gpt-4.1-mini"),   # 全渠道派生：短派生，便宜
}

# ── 火山引擎 Seedream（AI 生图）────────────────────────────
VOLCENGINE_API_KEY: str = _env("VOLCENGINE_API_KEY")
SEEDREAM_MODEL: str = _env("SEEDREAM_MODEL", "doubao-seedream-4-5-251128")

# ── 发帖调度 ─────────────────────────────────────────────────
# 默认发帖时间点（逗号分隔，24 小时制）
DEFAULT_POST_TIMES: str = _env("POST_TIMES", "09:00,13:00,17:00")

# ── 审稿设置 ─────────────────────────────────────────────────
REVIEWER_MAX_ROUNDS: int = _env_int("REVIEWER_MAX_ROUNDS", 3)
REVIEWER_MIN_SCORE: int = _env_int("REVIEWER_MIN_SCORE", 80)

# ── 确定性 SEO 校验（eval 第①层，零成本）────────────────────
# 全部阈值集中在这里，和上面的 REVIEWER_* 放一起 —— 「审几轮 / 多少分算过 /
# 硬性指标卡在哪」是同一类决策（threshold），应该在同一个地方看得全。
# 术语区分：这些是 **threshold（阈值）**；EVAL_HARD_GATE 才是 **gate（闸门）**。
# 阈值决定"算不算过"，闸门决定"过不了会不会被拦住"，两回事，别混着叫。
#
# 每条阈值对应 agents/copywriter.py 里「SEO 十条」的第几条，见 eval/deterministic.py。
EVAL_EXCERPT_MIN_CHARS: int = _env_int("EVAL_EXCERPT_MIN_CHARS", 150)   # 第 6 条：Google 摘要展示下限
EVAL_EXCERPT_MAX_CHARS: int = _env_int("EVAL_EXCERPT_MAX_CHARS", 160)   # 第 6 条：展示上限，超了被截断
EVAL_H2_MIN: int = _env_int("EVAL_H2_MIN", 4)                            # 第 3 条（含 FAQ 那节）
EVAL_H2_MAX: int = _env_int("EVAL_H2_MAX", 7)                            # 上限：章节太碎不利于阅读
EVAL_INTERNAL_LINKS_MIN: int = _env_int("EVAL_INTERNAL_LINKS_MIN", 3)    # 第 7 条
EVAL_INTERNAL_LINKS_MAX: int = _env_int("EVAL_INTERNAL_LINKS_MAX", 5)    # 上限：内链太多会稀释权重
EVAL_EXTERNAL_LINKS_MIN: int = _env_int("EVAL_EXTERNAL_LINKS_MIN", 1)    # 第 8 条
EVAL_EXTERNAL_LINKS_MAX: int = _env_int("EVAL_EXTERNAL_LINKS_MAX", 2)    # 上限：外链太多把权重送出去
EVAL_PRIMARY_KW_WINDOW_WORDS: int = _env_int("EVAL_PRIMARY_KW_WINDOW_WORDS", 100)  # 第 2 条
# h2 关键词堆砌：同一个 N 词短语最多允许出现在几个 h2 里。
# 为什么不是 0：SEO 十条第 3 条本来就要求每个 h2 含副关键词，少量重复是**期望行为**，
# 定在 2 是"允许呼应、不允许换皮重复"。3 词是短语粒度的下限（2 词太容易偶然撞上）。
EVAL_H2_PHRASE_MAX_REPEAT: int = _env_int("EVAL_H2_PHRASE_MAX_REPEAT", 2)
EVAL_H2_PHRASE_MIN_WORDS: int = _env_int("EVAL_H2_PHRASE_MIN_WORDS", 3)

# 闸门开关：True = 硬性不过就跳过 reviewer 直接打回（省钱，默认）；
#          False = 只报告不阻断，照常调 reviewer。
# 留这个开关的理由：万一某个商家的稿子被某条阈值反复误伤，能先关掉闸门保住出稿，
# 再慢慢调阈值 —— 而不是被迫去注释代码。关掉后报告依然会渲进 Slack 卡片。
EVAL_HARD_GATE: bool = _env_bool("EVAL_HARD_GATE", True)

# 定点修复开关：检测到 h2 关键词堆砌时，是否花一次便宜的模型调用只重写那几个标题。
# 为什么需要它：h2 堆砌是 soft（不阻断），而 soft 只在"因为别的原因已经要重写"时才搭车修 ——
# 实测 reviewer 很宽松（连续两篇都 90 分直接通过），于是这类问题会**无限累积、一条都修不到**。
# 定点修复就是补这个缺口：不阻断出稿，但花 $0.0003 把它修掉。
EVAL_TARGETED_FIX: bool = _env_bool("EVAL_TARGETED_FIX", True)

# ── 预览服务 ─────────────────────────────────────────────────
PREVIEW_HOST: str = _env("PREVIEW_HOST", "0.0.0.0")
PREVIEW_PORT: int = _env_int("PREVIEW_PORT", 8900)
PREVIEW_BASE_URL: str = _env("PREVIEW_BASE_URL", "http://localhost:8900")

# ── 热词爬取 ─────────────────────────────────────────────────
SCRAPE_MAX_ITEMS: int = _env_int("SCRAPE_MAX_ITEMS", 30)

# ── Slack UI 语言 ────────────────────────────────────────────
# 2026-07-27 起：**所有 Slack UI 文案（卡片 / 按钮 / 进度 / 状态提示）统一英文**，
# 目的是展示一致 —— 多商家频道并排看时不希望一半中文一半英文。
#
# ⚠️ 这条只管 UI 文案（走 core.i18n.t() 的那些）。**不影响两件事**：
#   ① Bot 的对话回复 —— 那是 conversation.py 的 prompt 控制的，仍然跟随用户语言
#      （运营同事用中文提需求，bot 还是中文回）；
#   ② session 里的 language 字段 —— 仍在检测和记录，只是 UI 不再消费它。
# 所以这不是"删掉 i18n"，而是**把 UI 语言的决策权从会话语言收归配置** ——
# i18n 那套机制原样留着，把这里改成空字符串就退回旧的跟随行为，代码零改动。
SLACK_UI_LANG: str = _env("SLACK_UI_LANG", "en")

# ── 时区 ─────────────────────────────────────────────────────
TIMEZONE: str = _env("TIMEZONE", "America/Los_Angeles")

# ── 目录 ─────────────────────────────────────────────────────
OUTPUT_DIR: Path = BASE_DIR / "output"
STORE_DIR: Path = BASE_DIR / "store"
MERCHANTS_DIR: Path = BASE_DIR / "merchants"
TEMPLATES_DIR: Path = BASE_DIR / "templates"
LOGS_DIR: Path = BASE_DIR / "logs"

# 确保目录存在
for _d in (OUTPUT_DIR, STORE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
