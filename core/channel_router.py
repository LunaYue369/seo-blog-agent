"""频道消息路由器 — 解析 auto 指令，分发到对应的商家流水线

支持的指令格式：
    auto 3         → 立即生成 3 篇博客供审核
    auto on 09:00 14:00 18:00 → 开启定时调度（自定义时间点）
    auto on        → 开启定时调度（使用默认时间点）
    auto off       → 关闭定时调度
    auto status    → 查看当前调度状态
    publish        → 手动发布最近一篇草稿到 WordPress
"""

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class AutoCommand:
    """解析后的 auto 指令"""
    action: str         # "generate" | "schedule_on" | "schedule_off" | "status"
    count: int          # 生成数量（仅 generate 模式有效）
    times: list[str]    # 调度时间点（仅 schedule_on 模式有效）


# 匹配合法的时间格式 HH:MM
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def parse_auto_command(text: str) -> AutoCommand | None:
    """解析用户发送的 auto 指令

    Args:
        text: 用户消息原文

    Returns:
        解析后的 AutoCommand，非 auto 指令则返回 None
    """
    text = text.strip().lower()

    # 必须以 "auto" 开头
    if not text.startswith("auto"):
        return None

    parts = text.split()

    # 纯 "auto" 不带参数 → 不处理，必须指定数量
    if len(parts) == 1:
        return None

    second = parts[1]

    # "auto 3" → 立即生成 N 篇
    if second.isdigit():
        count = int(second)
        return AutoCommand(action="generate", count=count, times=[])

    # "auto on ..." → 开启定时
    # 可以决定自动post的时间点
    if second == "on":
        times = []
        for part in parts[2:]:
            if _TIME_RE.match(part):
                # 标准化时间格式 "9:00" → "09:00"
                h, m = part.split(":")
                times.append(f"{int(h):02d}:{m}")
        return AutoCommand(action="schedule_on", count=0, times=times)

    # "auto off" → 关闭定时
    if second == "off":
        return AutoCommand(action="schedule_off", count=0, times=[])

    # "auto status" → 查看状态
    if second == "status":
        return AutoCommand(action="status", count=0, times=[])

    # 无法识别的子命令
    log.warning("无法识别的 auto 子命令: %s", text)
    return None
