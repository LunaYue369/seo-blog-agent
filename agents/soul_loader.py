"""人格加载器 — 从商家目录加载 Agent 人格 markdown 文件

每个商家必须有完整的 souls 目录，不提供 fallback 默认人格。
启动时按商家 ID 加载：
    merchants/{merchant_id}/souls/_shared.md     → 该商家所有 Agent 共用的背景知识
    merchants/{merchant_id}/souls/researcher.md  → 热词研究员
    merchants/{merchant_id}/souls/copywriter.md  → SEO 文案
    merchants/{merchant_id}/souls/reviewer.md    → 审稿员
    merchants/{merchant_id}/souls/artist.md      → 图片 prompt 美化师
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# 所有商家人格存储: {merchant_id: {"_shared": "内容", "researcher": "内容", ...}}
_soul_store: dict[str, dict[str, str]] = {}


def load_merchant_souls(merchant_id: str, souls_dir: Path) -> None:
    """加载指定商家的所有人格文件到缓存

    Args:
        merchant_id: 商家标识（如 harbor_auto_spa）
        souls_dir: 该商家的 souls 目录路径
    """
    if not souls_dir.is_dir():
        raise FileNotFoundError(f"商家人格目录不存在: {souls_dir}")

    # souls是dict()，人格：人格的Prompt
    souls: dict[str, str] = {}

    # 所有必需的人格文件（_shared 是通用背景，其余是 Agent 人格）
    # 文件名规则: _shared → _shared.md, researcher → researcher.md
    required_files = {
        "_shared": "_shared.md",
        "researcher": "researcher.md",
        "copywriter": "copywriter.md",
        "reviewer": "reviewer.md",
        "artist": "artist.md",
    }

    for soul_id, filename in required_files.items():
        md_path = souls_dir / filename
        if not md_path.exists():
            raise FileNotFoundError(
                f"商家 {merchant_id} 缺少必需的人格文件: {md_path}"
            )
        souls[soul_id] = md_path.read_text(encoding="utf-8")
        log.info("[%s] 已加载人格: %s (%d 字符)", merchant_id, soul_id, len(souls[soul_id]))

    # 加载额外的人格文件（如果有）
    for md_file in souls_dir.glob("*.md"):
        agent_id = md_file.stem
        if agent_id not in souls:
            souls[agent_id] = md_file.read_text(encoding="utf-8")
            log.info("[%s] 已加载额外人格: %s", merchant_id, agent_id)

    _soul_store[merchant_id] = souls
    log.info("[%s] 人格加载完成 — 共 %d 个", merchant_id, len(souls) - 1)


def get_shared(merchant_id: str) -> str:
    """获取商家通用背景知识"""
    return _soul_store.get(merchant_id, {}).get("_shared", "")


def get_soul(merchant_id: str, agent_id: str) -> str:
    """获取商家某个 Agent 的人格定义"""
    return _soul_store.get(merchant_id, {}).get(agent_id, "")


def build_system_prompt(merchant_id: str, agent_id: str) -> str:
    """拼接通用背景 + 独立人格 → 完整 system prompt

    Args:
        merchant_id: 商家标识
        agent_id: Agent 标识（researcher / copywriter / reviewer / artist）

    Returns:
        拼接后的完整的人格 system prompt
    """
    parts = []
    shared = get_shared(merchant_id)
    # _shared是谁都有的
    if shared:
        parts.append(shared)
    soul = get_soul(merchant_id, agent_id)
    if soul:
        parts.append(soul)
    if not parts:
        raise ValueError(f"商家 {merchant_id} 的 Agent {agent_id} 没有可用的人格定义")
    return "\n\n---\n\n".join(parts)
