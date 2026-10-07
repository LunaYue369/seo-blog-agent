"""定时调度器 — 管理每个商家的自动发帖计划

功能：
    用户在 Slack 频道发 "auto on 09:00 14:00 18:00"
    → 本模块为该商家注册 3 个定时任务（闹钟）
    → 每天到了 09:00 / 14:00 / 18:00（美西时间）自动触发博客生成流水线
    → 生成结果发回 Slack 频道

支持的操作：
    schedule_on()  — 开启定时（注册闹钟）
    schedule_off() — 关闭定时（移除闹钟）
    get_schedule_status() — 查看当前状态

持久化（2026-07-25 修）：调度状态会写入 store/schedules.json，
Bot 重启时 init() 自动把 active=True 的商家闹钟重新注册回去。
旧行为是纯内存 —— 半夜重启一次，第二天所有商家的自动发帖就静默停了，
而 "auto status" 也会显示未开启，只能靠人发现「今天怎么没发文」。
"""

import json
import logging

# APScheduler 第三方库：Python 定时任务调度器
# BackgroundScheduler — 在后台线程运行，不阻塞主线程
# CronTrigger — 类似 Linux cron 的触发器，支持 "每天几点几分" 这种定时
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config as cfg

log = logging.getLogger(__name__)

# ── 模块级变量（全局唯一，相当于单例）──────────────────────

# APScheduler 实例 — 相当于一个按美西时间走的闹钟
# 启动后在后台线程里每秒检查"现在几点了，有没有到点的任务"
_scheduler: BackgroundScheduler | None = None

# Slack WebClient 引用 — 存下来是因为定时任务触发时（可能是凌晨无人时）
# 需要用它往 Slack 频道发消息。在 init() 时由 main.py 传入。
_slack_client = None

# 内存中的调度状态（仅运行期间有效，重启后清空）
# 用于 "auto status" 查询当前哪些商家开了定时、时间点是什么
# 例: {"harbor_auto_spa": {"active": True, "times": ["09:00", "14:00"], "channel_id": "C0000000001"}}
_schedule_state: dict[str, dict] = {}

# 调度状态落盘位置（重启恢复用）
_STORE_FILE = cfg.STORE_DIR / "schedules.json"


# ── 状态持久化 ────────────────────────────────────────────

def _save_state() -> None:
    """把 _schedule_state 写盘。失败只记日志（不能因为写盘失败就不让用户开调度）。"""
    try:
        _STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_schedule_state, f, ensure_ascii=False, indent=2)
        tmp.replace(_STORE_FILE)   # 原子替换，防止写一半被杀留下坏 JSON
    except Exception as e:
        log.warning("调度状态持久化失败（忽略，仅影响重启后的恢复）: %s", e)


def _restore_state() -> int:
    """从磁盘恢复调度状态，并把 active=True 的闹钟重新注册进 APScheduler。

    Returns:
        恢复并重新注册成功的商家数量

    注意：只恢复 active=True 的；用户手动 auto off 过的不会被复活。
    """
    if not _STORE_FILE.exists():
        return 0

    try:
        with open(_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:
        log.error("调度状态文件损坏，跳过恢复: %s", e)
        return 0

    restored = 0
    for merchant_id, state in data.items():
        if not isinstance(state, dict):
            continue
        _schedule_state[merchant_id] = state
        times = state.get("times") or []
        channel_id = state.get("channel_id") or ""
        if state.get("active") and times and channel_id:
            _register_jobs(merchant_id, channel_id, times)
            restored += 1
            log.info("[%s] 已从磁盘恢复调度: %s → 频道 %s", merchant_id, times, channel_id)

    return restored


# ── 初始化 ────────────────────────────────────────────────

def init(slack_client) -> None:
    """初始化调度器 — 由 main.py 启动时调用一次

    做四件事：
    1. 存下 Slack client 引用（后续定时任务用它发消息）
    2. 创建并启动 APScheduler 后台调度器
    3. 注册 chat session 每小时清理任务
    4. 从磁盘恢复上次的调度状态（重新注册闹钟）

    Args:
        slack_client: Slack WebClient 实例（main.py 的 app.client）
    """
    global _scheduler, _slack_client
    # 存下 Slack client，定时任务到点时用它发消息到频道
    _slack_client = slack_client

    # 创建 APScheduler 实例，设定时区为美西时间
    # 这样用户说 "09:00" 就是美西时间早上 9 点
    _scheduler = BackgroundScheduler(timezone=cfg.TIMEZONE)
    # 启动后台线程，开始计时（从此刻起 APScheduler 开始工作）
    _scheduler.start()
    log.info("调度器已启动 (timezone=%s)", cfg.TIMEZONE)

    # 每小时清理超过 24 小时的旧 chat session，防止内存泄漏
    from core.session import cleanup_old
    _scheduler.add_job(
        cleanup_old,
        trigger="interval",
        hours=1,
        id="session_cleanup",
        kwargs={"max_age_hours": 24},
        replace_existing=True,
    )
    log.info("已注册 session 清理任务: 每小时清理 >24h 的会话")

    # 恢复上次的调度（必须在 _scheduler.start() 之后，否则 add_job 无处可加）
    restored = _restore_state()
    if restored:
        log.info("调度恢复完成 — %d 个商家的闹钟已重新注册", restored)


# ── 定时任务回调（闹钟响了执行什么）──────────────────────

def _job_callback(merchant_id: str, channel_id: str) -> None:
    """定时任务回调 — APScheduler 到点时自动调用这个函数

    这个函数就是"闹钟响了之后要做的事"：
    1. 往 Slack 发一条"开始生成"的提示
    2. 执行完整的博客生成流水线（爬热词→选题→写作→审核→生图→预览）
    3. 把结果发回 Slack 频道

    generate_single_blog 内部已有商家级锁保护：
    - 如果此时用户正在手动生成（auto 3），定时任务不会阻塞等待
    - 而是直接返回 "already running" 错误，发一条 skip 消息到 Slack

    Args:
        merchant_id: 商家标识（如 "harbor_auto_spa"）
        channel_id: 目标 Slack 频道 ID（如 "C0000000001"）
    """
    # 延迟导入：避免模块之间循环依赖
    # （scheduler.py 和 blog_generator.py 互相不能在文件顶部 import 对方）
    from core.merchant_config import get_merchant
    from pipeline.blog_generator import generate_single_blog
    from slack_ui.blocks import build_blog_result_blocks

    log.info("[%s] 定时任务触发 — 开始生成博客", merchant_id)

    # 从内存中取出该商家的完整配置（merchant.json 里的内容）
    merchant_cfg = get_merchant(merchant_id)
    if not merchant_cfg:
        log.error("[%s] 商家配置不存在，跳过", merchant_id)
        return

    try:
        # 先往 Slack 频道发一条提示消息，告诉用户"定时任务开始了"
        store_name = merchant_cfg.get("store_name", merchant_id)
        if _slack_client:
            _slack_client.chat_postMessage(
                channel=channel_id,
                text=f":clock3: Scheduled blog generation started for {store_name}...",
            )

        # 执行完整的博客生成流水线（6 步：爬热词→选题→写作→审核→生图→组装 HTML）
        # 内部有商家锁，如果有人正在手动生成，会返回 success=False
        result = generate_single_blog(merchant_id, merchant_cfg)

        # 把结果发回 Slack 频道
        if _slack_client:
            if result.get("success"):
                # 生成成功 — 发送带预览链接、评分、用量的富消息卡片
                blocks = build_blog_result_blocks(result, index=1, total=1)
                _slack_client.chat_postMessage(
                    channel=channel_id,
                    text=f"Blog generated: {result.get('title', 'N/A')}",
                    blocks=blocks,
                )
            else:
                # 生成失败或被锁跳过 — 发送警告消息
                _slack_client.chat_postMessage(
                    channel=channel_id,
                    text=f":warning: Scheduled generation skipped: {result.get('error', 'Unknown')}",
                )
    except Exception as exc:
        # 未预期的异常 — 记录日志并通知 Slack
        log.exception("[%s] 定时生成失败: %s", merchant_id, exc)
        if _slack_client:
            _slack_client.chat_postMessage(
                channel=channel_id,
                text=f":x: Scheduled blog generation failed: {exc}",
            )


# ── 注册/移除定时任务（管理闹钟）──────────────────────────

def _register_jobs(merchant_id: str, channel_id: str, times: list[str]) -> None:
    """为商家注册定时任务 — 往 APScheduler 的闹钟里添加响铃时间

    比如 times=["09:00", "14:00", "18:00"] 会注册 3 个 job：
        blog_harbor_auto_spa_0900 → 每天 09:00 触发 _job_callback
        blog_harbor_auto_spa_1400 → 每天 14:00 触发 _job_callback
        blog_harbor_auto_spa_1800 → 每天 18:00 触发 _job_callback

    每次调用前会先清除该商家的旧任务，防止用户改时间后闹钟叠加。

    Args:
        merchant_id: 商家标识
        channel_id: 目标 Slack 频道
        times: 时间点列表（HH:MM 格式，如 ["09:00", "14:00"]）
    """
    if not _scheduler:
        return

    # 先清除该商家的所有旧闹钟（防止改时间后新旧叠加）
    _remove_jobs(merchant_id)

    # 逐个时间点注册新闹钟
    for t in times:
        try:
            # 拆分 "09:00" → hour=9, minute=0
            hour, minute = t.split(":")
            # 生成唯一 job ID，如 "blog_harbor_auto_spa_0900"
            job_id = f"blog_{merchant_id}_{t.replace(':', '')}"

            # 调用 APScheduler 的 add_job 注册定时任务
            _scheduler.add_job(
                _job_callback,       # 到点执行什么函数
                trigger=CronTrigger( # 每天几点几分触发（cron 风格）
                    hour=int(hour),
                    minute=int(minute),
                    timezone=cfg.TIMEZONE,
                ),
                id=job_id,           # 任务唯一标识（用于后续查找和删除）
                args=[merchant_id, channel_id],  # 传给 _job_callback 的参数
                replace_existing=True,    # 同 ID 已存在就覆盖（安全措施）
                misfire_grace_time=300,   # 如果错过触发时间，5 分钟内还能补执行
            )
            log.info("[%s] 已注册定时任务: %s", merchant_id, t)
        except Exception as exc:
            log.error("[%s] 注册定时任务失败 (%s): %s", merchant_id, t, exc)


def _remove_jobs(merchant_id: str) -> None:
    """移除指定商家的所有定时任务 — 清除该商家的所有闹钟

    通过 job ID 前缀匹配来找到该商家的所有任务。
    比如 merchant_id="harbor_auto_spa"，会匹配并删除：
        blog_harbor_auto_spa_0900
        blog_harbor_auto_spa_1400
        blog_harbor_auto_spa_1800
    """
    if not _scheduler:
        return

    # 所有该商家的 job ID 都以 "blog_{merchant_id}_" 开头
    prefix = f"blog_{merchant_id}_"
    # 遍历 APScheduler 中所有已注册的 job，按前缀匹配删除
    for job in _scheduler.get_jobs():
        if job.id.startswith(prefix):
            job.remove()  # APScheduler 提供的删除方法
            log.info("[%s] 已移除定时任务: %s", merchant_id, job.id)


# ── 公共 API（供 main.py 调用）────────────────────────────

def schedule_on(merchant_id: str, channel_id: str, times: list[str] | None = None) -> list[str]:
    """开启商家的自动调度 — 用户发 "auto on 09:00 14:00" 时调用

    流程：
    1. 如果用户没指定时间，用商家默认时间或全局默认时间
    2. 调用 _register_jobs 注册闹钟
    3. 更新内存中的调度状态

    Args:
        merchant_id: 商家标识
        channel_id: 目标 Slack 频道
        times: 用户指定的时间点列表；为空则使用默认值

    Returns:
        实际使用的时间点列表（返回给 Slack 展示）
    """
    if not times:
        # 用户没指定时间 → 尝试用商家 merchant.json 里的 default_post_times
        from core.merchant_config import get_merchant
        merchant_cfg = get_merchant(merchant_id) or {}
        times = merchant_cfg.get("default_post_times")
        if not times:
            # 商家也没配 → 用全局默认（.env 里的 POST_TIMES，如 "09:00,13:00,17:00"）
            times = [t.strip() for t in cfg.DEFAULT_POST_TIMES.split(",") if t.strip()]

    # 往 APScheduler 注册闹钟
    _register_jobs(merchant_id, channel_id, times)

    # 记录状态到内存（用于 "auto status" 查询）+ 落盘（用于重启恢复）
    _schedule_state[merchant_id] = {
        "active": True,
        "times": times,
        "channel_id": channel_id,
    }
    _save_state()

    log.info("[%s] 调度已开启: %s", merchant_id, times)
    return times


def schedule_off(merchant_id: str) -> None:
    """关闭商家的自动调度 — 用户发 "auto off" 时调用

    1. 移除该商家在 APScheduler 中的所有闹钟
    2. 更新内存状态为 inactive
    """
    # 删除所有该商家的定时任务
    _remove_jobs(merchant_id)

    # 标记为非活跃并落盘（否则重启后会被 _restore_state 复活）
    if merchant_id in _schedule_state:
        _schedule_state[merchant_id]["active"] = False
        _save_state()

    log.info("[%s] 调度已关闭", merchant_id)


def get_schedule_status(merchant_id: str) -> dict:
    """获取商家的调度状态 — 用户发 "auto status" 时调用

    Returns:
        {"active": True/False, "times": ["09:00", "14:00"], "channel_id": "C0..."}
        如果该商家从未开启过调度，返回默认的空状态
    """
    return _schedule_state.get(merchant_id, {"active": False, "times": [], "channel_id": ""})
