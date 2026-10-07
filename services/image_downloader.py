"""Slack 图片下载器 — 下载用户在 Slack 中上传的图片到本地

当用户在 chat 对话中上传图片时，通过 Slack API 下载到本地存储，
后续可作为博客配图使用（替代 Seedream AI 生成）。

存储目录: output/uploads/（在 blog-worker 项目内）
文件命名: {时间戳}_{原始文件名}，避免同名冲突

使用示例:
    # Slack 事件中获取到的 file 对象:
    # {"url_private": "https://files.slack.com/...", "name": "car_wrap.jpg", "mimetype": "image/jpeg"}

    path = download_slack_file(
        url="https://files.slack.com/files-pri/.../car_wrap.jpg",
        filename="car_wrap.jpg",
        token="xoxb-..."
    )
    # → 返回 "/path/to/blog-agent/output/uploads/20260403_143000_car_wrap.jpg"
    # → 失败返回 None
"""

import logging
from datetime import datetime
from pathlib import Path

import requests

import config as cfg

log = logging.getLogger(__name__)

# ── 用户上传图片的本地存储目录 ──
# 放在 output/uploads/ 下，跟随项目目录，不放桌面
UPLOAD_DIR = cfg.OUTPUT_DIR / "uploads"


def download_slack_file(url: str, filename: str, token: str) -> str | None:
    """从 Slack 下载单个文件到本地

    Args:
        url:      Slack 文件的 url_private（需要 Bearer token 认证）
                  示例: "https://files.slack.com/files-pri/T012345-F678/car_wrap.jpg"
        filename: 原始文件名
                  示例: "car_wrap.jpg"
        token:    Slack Bot Token（用于 Authorization header）
                  示例: "xoxb-..."

    Returns:
        下载成功 → 本地文件的绝对路径字符串
        下载失败 → None

    输出示例:
        成功: "/path/to/blog-agent/output/uploads/20260403_143000_car_wrap.jpg"
        失败: None
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 加时间戳前缀避免文件名冲突
    # 格式: 20260403_143000_car_wrap.jpg
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_name = f"{ts}_{filename}"
    local_path = UPLOAD_DIR / local_name

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()

        with open(local_path, "wb") as f:
            f.write(resp.content)

        log.info("已下载 Slack 文件: %s → %s (%d bytes)", filename, local_path, len(resp.content))
        return str(local_path)

    except Exception as e:
        log.error("下载 Slack 文件失败 %s: %s", filename, e)
        return None
