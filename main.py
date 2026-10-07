"""Digital Blog Worker — 多商家 SEO 博客自动生成 Slack Bot

入口文件：
1. 加载所有商家配置和 Agent 人格
2. 启动预览服务器（HTTP 静态文件服务）
3. 启动 APScheduler 定时调度器
4. 启动 Slack Bot（Socket Mode）监听各频道的指令

一个 Bot 实例服务所有商家，通过频道 ID 区分。

两条独立的流程：
1. Auto 流程（命令式，无状态）：
    auto 3         → 立即生成 3 篇博客供审核
    auto on        → 开启定时调度（使用默认时间点）
    auto on 09:00 14:00 18:00 → 开启定时调度（自定义时间点）
    auto off       → 关闭定时调度
    auto status    → 查看当前调度状态
    publish        → 发布最近一篇草稿到 WordPress

2. Chat 流程（对话式，有状态）：
    任何非命令的 @mention → 进入对话模式（收集主题、关键词、图片等）
    thread 内回复        → 继续对话 / 修改请求
    支持用户上传图片作为博客配图

路由逻辑：
  用户消息进来
      │
      ├─ 是 thread 内回复 且 thread 有活跃会话 → Chat 对话流程
      │
      ├─ "@Bot auto ..."    → Auto 流程（完全不变）
      │
      └─ "@Bot 其他任何内容" → 新建 Chat 对话（替代原来的"显示帮助"）
"""

import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config as cfg
import scheduler
from core.merchant_config import load_all_merchants, get_merchant_by_channel
from core.channel_router import parse_auto_command
from core import session as chat_session
from core.session import GATHERING, CONFIRMING, GENERATING, REVIEWING, DONE
from core.i18n import t, resolve_language
from agents.conversation import chat_and_maybe_generate
from pipeline.blog_generator import generate_multiple_blogs
from pipeline.preview_server import start_preview_server
from services.image_downloader import download_slack_file
from slack_ui.blocks import (
    build_batch_summary_blocks,
    build_schedule_status_blocks,
    build_generating_message,
    build_progress_blocks,
)
from services.wordpress_publisher import WordPressPublisher
from store.blog_store import (
    get_drafts,
    mark_published,
    release_publish_claim,
    try_claim_publish,
)

# ── 日志配置 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(cfg.LOGS_DIR / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Slack App 初始化 ──────────────────────────────────────
app = App(token=cfg.SLACK_BOT_TOKEN)

# Bot 自身的 user_id（用于过滤自己的消息，防止回环）
_bot_user_id: str = ""

# ══════════════════════════════════════════════════════════
#  事件去重（幂等）
# ══════════════════════════════════════════════════════════
# Slack 的同一条消息会被重复投递，有三个独立来源：
#   ① 一条 "@Bot xxx" 同时触发 message 和 app_mention 两个事件（同一个 ts，两个 envelope）
#   ② Bolt 若没在 3 秒内 ack，Slack 会重发同一事件
#   ③ Socket Mode 断线重连期间可能重复投递
# 所以用消息自身的 ts 当幂等键（idempotency key）—— ts 在一个频道内唯一。
#
# 【为什么用 OrderedDict，而不是 set + list，也不是普通 dict】
# 旧实现是 set（负责 O(1) 查询）+ list（负责记插入顺序好淘汰最老的）两份状态。
# 两份状态 = 两份可能不一致的风险，能合就合。合并的候选有两个：
#
#   ① 普通 dict：Python 3.7+ 保证插入顺序，看起来够用。但它的淘汰要写成
#      `d.pop(next(iter(d)))`，而 CPython 的 dict 是「紧凑数组 + 索引表」结构，
#      删掉的槽位会留下空洞，直到下次扩容才压缩。所以连续从头部删时，
#      `next(iter(d))` 每次都要跳过越来越长的空洞前缀 → 病态情况退化成 O(n)。
#      本机实测连续删 19000 次：dict 0.407s vs OrderedDict 0.036s（差 11 倍）。
#
#   ② OrderedDict：内部是「哈希表 + 双向链表」，所以
#        - `ts in od`              → O(1) 查询（哈希表）
#        - `od.popitem(last=False)` → O(1) 弹出最老的（链表头，有明确保证）
#        - `od.move_to_end(ts)`     → O(1) 把某个键移到最新（普通 dict 只能 del+重插模拟）
#      这三个正是「有界缓存」需要的全部原语，也是经典 LRU 题的标准解法。
#
# 选 ②。规模上（500 条）两者都跑得动，但 OrderedDict 把「顺序在这里有语义」
# 写进了类型本身，而且淘汰和刷新都是有保证的 O(1)，不用去推理 dict 的内部实现。
#
# 【为什么淘汰要看时间而不只看条数】
# 旧实现只留最近 200 条。问题是**淘汰窗口取决于流量而不是时间**：
# 忙时 200 条可能几秒就翻完，而 Slack 的重试可能几十秒后才到 —— 那时原始 ts
# 已被挤掉，重复就漏进来了。Slack 重试都在分钟级，所以以 TTL 为主、条数只做内存兜底。
_EVENT_TTL_SECONDS = 300        # 5 分钟内的同一 ts 视为重复（覆盖 Slack 的重试窗口）
_MAX_PROCESSED_EVENTS = 500     # 内存兜底上限，防极端流量下无限增长
_processed_events: "OrderedDict[str, float]" = OrderedDict()   # ts → 首次处理时刻（链表头=最老）
# 【为什么必须加锁】
# handle_message 跑在 Bolt 的**监听线程池**里，多个事件会并发进来。
# "if ts in dict: return" 再 "dict[ts] = now" 是 check-then-act，两步之间没有原子性：
# message 和 app_mention 两个 envelope 落在两个线程上时，可能双双通过检查 → 双重处理
# （窗口只有微秒级，但真实存在，表现为偶发的"回复了两次/生成了两篇"）。
# 把判断和写入包进同一把锁里，才是真正的"认领"语义。
_processed_lock = threading.Lock()


def _claim_event(event_ts: str) -> bool:
    """原子地「认领」一个事件。

    Args:
        event_ts: Slack 消息自身的 ts，如 "1712100000.123456"

    Returns:
        True  = 首次见到，调用方可以继续处理
        False = 重复投递，调用方应立刻 return

    ⚠️ 语义选择（at-most-once）：这里在**干活之前**就标记为已处理。
       好处：重复投递绝不会双发博客/双发邮件。
       代价：如果处理中途崩了，Slack 的重试也会被当成重复丢掉，那条消息就丢了。
       对 Slack bot 这个取舍是对的（重复发内容的代价 >> 漏一条消息）；
       但如果换成订单/支付类事件，就该改成 at-least-once + 业务层用订单号做幂等。
    """
    if not event_ts:
        # 没有 ts 就无法去重（正常不会发生），放行，交给后面的过滤器处理
        return True

    now = time.time()
    with _processed_lock:
        seen_at = _processed_events.get(event_ts)
        if seen_at is not None and now - seen_at < _EVENT_TTL_SECONDS:
            return False   # 5 分钟内已处理过 → 重复

        # 记录/刷新时间戳。已存在（说明是过期后重新认领）就把它移到链表尾部，
        # 否则它会挂在最老的位置被过早淘汰 —— 给已存在的键赋值本身**不会**改变顺序。
        # 这正是 OrderedDict 比普通 dict 好用的地方：move_to_end 一步到位，
        # 用普通 dict 只能 `pop` 再重新插入来手工模拟同样的效果。
        _processed_events[event_ts] = now
        _processed_events.move_to_end(event_ts)

        # 淘汰：先按 TTL 清过期的（这是主力，因为重复投递是分钟级现象），
        # 清完还超上限再按顺序从最老的开始砍（内存兜底，极端流量下才会走到）
        if len(_processed_events) > _MAX_PROCESSED_EVENTS:
            for ts in [k for k, v in _processed_events.items() if now - v >= _EVENT_TTL_SECONDS]:
                del _processed_events[ts]
            while len(_processed_events) > _MAX_PROCESSED_EVENTS:
                _processed_events.popitem(last=False)   # last=False → 弹出最老的那个

        return True


# ══════════════════════════════════════════════════════════
#  消息事件处理 — 统一入口，路由到 Auto / Chat
# ══════════════════════════════════════════════════════════

@app.event("message")
def handle_message(event: dict, say, client) -> None:
    """处理频道中的消息 — 路由到 Auto 命令流程或 Chat 对话流程

    路由规则（按优先级）：
    1. 过滤 Bot 自身消息 + 子类型消息
    2. 如果是 thread 内回复 且 thread 有活跃 chat 会话 → Chat 对话流程
    3. 如果 @mention 了 Bot:
       a. "auto ..."  → Auto 流程
       b. 其他文字    → 新建 Chat 对话
    4. 如果没有 @mention 且不在 chat thread 内 → 忽略
    """
    # ── 去重 — 原子认领，防止 message + app_mention（以及 Slack 重试）重复处理 ──
    if not _claim_event(event.get("ts", "")):
        return

    # ── 调试日志 — 查看每条进来的消息 ──
    user = event.get("user", "")
    subtype = event.get("subtype")
    log.info("收到消息事件: user=%s subtype=%s thread_ts=%s ts=%s files=%s text=%s",
             user, subtype, event.get("thread_ts"), event.get("ts"),
             [f.get("name") for f in event.get("files", [])],
             (event.get("text") or "")[:80])

    # ── 过滤 Bot 自身消息（防自我对话回环）──
    # bot 自己的回复也会作为 message 事件推回来，不挡住就是
    # "bot 回复 → 触发事件 → bot 又回复" 的无限循环 + 无限烧钱
    if user == _bot_user_id:
        return

    # ── 过滤其它 bot 的消息（防 bot 互撕）──
    # 这个 workspace 里有多个 bot。如果别的 bot 发的消息里带 @blog-bot，
    # 上面那道"是不是我自己"的门是挡不住的 → 两个 bot 可能互相触发，
    # 指数级烧钱且很难排查。凡是带 bot_id 的消息（应用发的）一律不处理。
    if event.get("bot_id"):
        return

    # ── 过滤子类型消息（编辑、删除等，但保留 file_share 因为用户可能只上传图片无文字） ──
    if subtype and subtype not in ("file_share",):
        return

    text = event.get("text", "").strip()
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts")  # 如果是 thread 回复，这里有值
    message_ts = event.get("ts", "")    # 消息自身的 timestamp

    # ── 检查是否在活跃的 chat 会话 thread 内 ──
    # 如果用户在一个已有 chat 会话的 thread 里回复，直接走 Chat 流程
    # 不需要 @mention（thread 内对话自然延续）
    if thread_ts:
        # ★ 用 (channel, thread_ts) 复合键查会话：ts 只保证频道内唯一，
        #   裸 ts 做键会让两个商家频道在同一微秒发消息时串台（见 core/session.py 顶部说明）
        existing_session = chat_session.get(chat_session.make_key(channel, thread_ts))
        if existing_session:
            _handle_chat_message(event, existing_session, text, say, client)
            return

    # ── 必须 @mention Bot 才响应（非 thread 内的新消息） ──
    if f"<@{_bot_user_id}>" not in text:
        return

    # 去掉 @mention 部分，提取纯指令文本
    text = text.replace(f"<@{_bot_user_id}>", "").strip()

    # ── 检查 publish 指令 ──
    if text.lower().startswith("publish"):
        _handle_publish(text, channel, say, client)
        return

    # ── 检查 auto 指令 ──
    cmd = parse_auto_command(text)
    if cmd is not None:
        # 是 auto 指令 → 走原有的 Auto 流程（完全不变）
        _handle_auto_command(cmd, channel, user, say, client)
        return

    # ── 不是 auto/publish → 进入 Chat 对话流程 ──
    # 用消息自身的 ts 作为 thread_ts（新开一个 thread）
    _start_chat_session(event, text, message_ts, channel, say, client)


# ══════════════════════════════════════════════════════════
#  Chat 对话流程
# ══════════════════════════════════════════════════════════

def _start_chat_session(event: dict, text: str, message_ts: str, channel: str, say, client):
    """开始一个新的 Chat 对话会话

    当用户 @mention Bot 但不是 auto/publish 指令时触发。
    创建新会话，下载图片（如果有），启动对话层。

    Args:
        event:      Slack 事件字典
        text:       用户消息文本（已去掉 @mention）
        message_ts: 消息时间戳（作为 thread 的起始 ts）
        channel:    频道 ID
        say:        Slack say 函数
        client:     Slack WebClient
    """
    # 查找频道对应的商家
    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        say(":warning: This channel is not linked to any merchant. Please check merchant configuration.")
        return

    merchant_id = merchant_cfg["merchant_id"]

    # 创建新会话（用消息 ts 作为 thread_ts）
    sess = chat_session.get_or_create(message_ts, channel)
    log.info("[%s] 新 Chat 会话: thread=%s user=%s", merchant_id, message_ts, event.get("user", ""))

    # 检测语言 —— 用 resolve_language（双向 + 迟滞），不再是只能 en→zh 的单向棘轮
    if text:
        sess["language"] = resolve_language(sess.get("language", "en"), text)
        chat_session.persist()

    # 下载用户上传的图片（如果有）
    new_images = _download_event_images(event, sess, client)
    if new_images:
        lang = sess.get("language", "en")
        # 直接用 sess（就是同一个 dict 对象）；旧代码用裸 ts 再查一次会话，
        # 万一这中间会话被清理任务删掉就是 None.get → AttributeError
        total = len(sess.get("user_images", []))
        img_lines = "\n".join(f":frame_with_picture: Image {total - len(new_images) + i + 1}: `{name}`"
                              for i, name in enumerate(new_images))
        say(text=f":white_check_mark: *{t('received_images', lang, count=len(new_images))}* ({t('total_images', lang, total=total)})\n{img_lines}",
            thread_ts=message_ts)

    # 记录用户消息到会话历史
    if text:
        chat_session.add_message(sess["key"], "user", text)

    # 用后台线程启动对话层（避免阻塞 Slack 事件循环的 3 秒 ack 超时）
    def _thread_say(**kwargs):
        """包装 say 函数，自动加 thread_ts"""
        kwargs.setdefault("thread_ts", message_ts)
        return say(**kwargs)

    threading.Thread(
        target=_safe_run,
        args=(chat_and_maybe_generate, sess, text, _thread_say, client, merchant_id, merchant_cfg),
        daemon=True,
        name=f"chat-{merchant_id}-{message_ts[:10]}",
    ).start()


def _handle_chat_message(event: dict, sess: dict, text: str, say, client):
    """处理已有 Chat 会话 thread 内的消息

    根据会话当前状态分发处理:
    - GATHERING:  继续对话（收集信息）
    - GENERATING: 告知用户等待
    - REVIEWING:  用户打字修改意见 → 回到 GATHERING 处理
    - DONE:       用户继续说话 → 开启新一轮

    Args:
        event: Slack 事件字典
        sess:  已有的会话字典
        text:  用户消息文本（原始，可能含 @mention）
        say:   Slack say 函数
        client: Slack WebClient
    """
    thread_ts = sess["thread_ts"]   # 纯 Slack ts —— 只用于往 thread 里发消息
    channel = sess["channel"]
    key = sess["key"]                # 会话主键 —— 所有 session.* 操作都用它

    # 去掉 @mention（thread 内可能有也可能没有）
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

    # 查找商家
    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        return
    merchant_id = merchant_cfg["merchant_id"]

    # 检测语言（双向 + 迟滞，见 core/i18n.resolve_language）
    if text:
        sess["language"] = resolve_language(sess.get("language", "en"), text)
        chat_session.persist()
    lang = sess.get("language", "en")

    # 下载用户上传的图片
    new_images = _download_event_images(event, sess, client)
    if new_images:
        total = len(sess.get("user_images", []))
        img_lines = "\n".join(f":frame_with_picture: Image {total - len(new_images) + i + 1}: `{name}`"
                              for i, name in enumerate(new_images))
        say(text=f":white_check_mark: *{t('received_images', lang, count=len(new_images))}* ({t('total_images', lang, total=total)})\n{img_lines}",
            thread_ts=thread_ts)

    # 包装 say 函数，自动加 thread_ts
    def _thread_say(**kwargs):
        kwargs.setdefault("thread_ts", thread_ts)
        return say(**kwargs)

    stage = sess["stage"]

    if stage == GATHERING:
        # 对话中 — 继续收集信息
        if text:
            chat_session.add_message(key, "user", text)
        threading.Thread(
            target=_safe_run,
            args=(chat_and_maybe_generate, sess, text, _thread_say, client, merchant_id, merchant_cfg),
            daemon=True,
        ).start()

    elif stage == CONFIRMING:
        # 确认阶段 — 用户打字修改（回到 GATHERING 重新对话）
        if text:
            chat_session.update_stage(key, GATHERING)
            chat_session.add_message(key, "user", text)
            threading.Thread(
                target=_safe_run,
                args=(chat_and_maybe_generate, sess, text, _thread_say, client, merchant_id, merchant_cfg),
                daemon=True,
            ).start()
        else:
            _thread_say(text=t("confirm_or_adjust", lang))

    elif stage == GENERATING:
        # 正在生成 — 告知等待
        _thread_say(text=f"{t('still_generating', lang)} :hourglass_flowing_sand:")

    elif stage == REVIEWING:
        # 审核阶段 — 用户打字提修改意见（回到 GATHERING，GPT 提取 modify_scope）
        if text:
            chat_session.update_stage(key, GATHERING)
            chat_session.add_message(key, "user", text)
            threading.Thread(
                target=_safe_run,
                args=(chat_and_maybe_generate, sess, text, _thread_say, client, merchant_id, merchant_cfg),
                daemon=True,
            ).start()
        else:
            _thread_say(text=t("click_or_change", lang))

    elif stage == DONE:
        # 已完成 — 新一轮对话
        chat_session.update_stage(key, GATHERING)
        if text:
            chat_session.add_message(key, "user", text)
        threading.Thread(
            target=_safe_run,
            args=(chat_and_maybe_generate, sess, text, _thread_say, client, merchant_id, merchant_cfg),
            daemon=True,
        ).start()


def _download_event_images(event: dict, sess: dict, client) -> list[str]:
    """从 Slack 事件中下载用户上传的图片

    Slack 的 message 事件中 files 字段包含上传的文件信息。
    app_mention 事件可能不含 files，需要通过 API 补充获取。

    Args:
        event:  Slack 事件字典
        sess:   目标会话字典（直接传会话对象，避免再用键去查一次，
                也顺带消除了「用裸 thread_ts 查会话」这条路径）
        client: Slack WebClient（用于 API 补充获取）

    Returns:
        本次新下载的原始文件名列表（用于确认消息）
    """
    token = cfg.SLACK_BOT_TOKEN
    files = event.get("files", [])

    # app_mention 事件可能不含 files，通过 API 补充
    if not files:
        files = _fetch_files_from_api(client, event.get("channel", ""), event.get("ts", ""))

    # 用 Slack file ID 去重，防止 message + app_mention 双重下载
    downloaded_file_ids = set()
    if sess:
        # 从磁盘恢复的会话里这个字段是 list（JSON 没有 set 类型），session.load_from_disk
        # 已经转回 set；这里再 set() 兜一层，防止历史文件/手工编辑留下 list 导致 .add 报错
        downloaded_file_ids = set(sess.get("_downloaded_file_ids") or set())

    new_names: list[str] = []
    for f in files:
        file_id = f.get("id", "")
        if f.get("mimetype", "").startswith("image/") and file_id not in downloaded_file_ids:
            path = download_slack_file(f["url_private"], f["name"], token)
            if path:
                chat_session.add_user_image(sess["key"], path)
                downloaded_file_ids.add(file_id)
                new_names.append(f["name"])
                log.info("已下载用户图片: %s → %s", f["name"], path)

    if sess:
        sess["_downloaded_file_ids"] = downloaded_file_ids
        if new_names:
            chat_session.persist()   # 直接改了 sess dict，手动落盘

    return new_names


def _fetch_files_from_api(client, channel: str, ts: str) -> list:
    """当事件中没有 files 时，通过 Slack API 获取消息附件

    app_mention 事件通常不含 files 字段，需要用
    conversations.replies 来获取用户上传的图片。

    Args:
        client:  Slack WebClient
        channel: 频道 ID
        ts:      消息时间戳

    Returns:
        文件列表（可能为空）
    """
    if not ts:
        return []
    try:
        resp = client.conversations_replies(
            channel=channel, ts=ts, limit=1, inclusive=True,
        )
        for msg in resp.get("messages", []):
            if msg.get("ts") == ts and msg.get("files"):
                log.info("通过 API 补充获取到 %d 个文件", len(msg["files"]))
                return msg["files"]
    except Exception as e:
        log.warning("获取消息附件失败: %s", e)
    return []


# ══════════════════════════════════════════════════════════
#  Chat 按钮交互处理
# ══════════════════════════════════════════════════════════

@app.action("chat_confirm_generate")
def handle_chat_confirm_generate(ack, body, say, client) -> None:
    """用户确认参数，开始生成博客"""
    ack()
    thread_ts = _get_thread_ts_from_body(body)
    channel = body.get("channel", {}).get("id", "")
    if not thread_ts:
        return

    # 按钮回调里同样要用 (channel, thread_ts) 复合键查会话 —— 按钮的 payload
    # 里 channel 和 message.ts 都有，所以拼键是免费的
    key = chat_session.make_key(channel, thread_ts)
    sess = chat_session.get(key)
    if not sess:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=t("session_expired", "en"),
        )
        return

    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        return
    merchant_id = merchant_cfg["merchant_id"]

    lang = sess.get("language", "en")

    # ★ 原子状态门：双击 Confirm 时第二次会被拒，避免并发两条 pipeline 重复烧钱
    if not chat_session.try_begin_generating(key):
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"{t('still_generating', lang)} :hourglass_flowing_sand:",
        )
        return

    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":rocket: {t('starting_generation', lang)}",
    )

    def _thread_say(**kwargs):
        kwargs.setdefault("thread_ts", thread_ts)
        return say(**kwargs)

    from pipeline.chat_generator import run_chat_pipeline
    threading.Thread(
        target=_safe_run,
        args=(run_chat_pipeline, sess, merchant_id, merchant_cfg, _thread_say, client),
        daemon=True,
    ).start()


@app.action("chat_confirm_edit")
def handle_chat_confirm_edit(ack, body, client) -> None:
    """用户想继续调整参数，回到 GATHERING 状态"""
    ack()
    thread_ts = _get_thread_ts_from_body(body)
    channel = body.get("channel", {}).get("id", "")
    if not thread_ts:
        return

    # 按钮回调里同样要用 (channel, thread_ts) 复合键查会话 —— 按钮的 payload
    # 里 channel 和 message.ts 都有，所以拼键是免费的
    key = chat_session.make_key(channel, thread_ts)
    sess = chat_session.get(key)
    if not sess:
        return

    lang = sess.get("language", "en")
    chat_session.update_stage(key, GATHERING)
    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":pencil2: {t('no_problem_adjust', lang)}",
    )


@app.action(re.compile(r"^chat_regenerate_"))
def handle_chat_regenerate(ack, body, say, client) -> None:
    """处理 Chat 模式的 Regenerate（重新生成）按钮

    完全重新生成（不是修改），使用相同的参数重跑整个 pipeline。
    """
    ack()
    thread_ts = _get_thread_ts_from_body(body)
    channel = body.get("channel", {}).get("id", "")
    if not thread_ts:
        return

    # 按钮回调里同样要用 (channel, thread_ts) 复合键查会话 —— 按钮的 payload
    # 里 channel 和 message.ts 都有，所以拼键是免费的
    key = chat_session.make_key(channel, thread_ts)
    sess = chat_session.get(key)
    if not sess:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=t("session_expired", "en"),
        )
        return

    lang = sess.get("language", "en")
    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        return
    merchant_id = merchant_cfg["merchant_id"]

    # ★ 同 Confirm：原子状态门，防双击并发重跑（Regenerate 是整条 pipeline，双跑最贵）
    if not chat_session.try_begin_generating(key):
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"{t('still_generating', lang)} :hourglass_flowing_sand:",
        )
        return

    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":arrows_counterclockwise: {t('regenerating', lang)}",
    )

    def _thread_say(**kwargs):
        kwargs.setdefault("thread_ts", thread_ts)
        return say(**kwargs)

    from pipeline.chat_generator import run_chat_pipeline
    threading.Thread(
        target=_safe_run,
        args=(run_chat_pipeline, sess, merchant_id, merchant_cfg, _thread_say, client),
        daemon=True,
    ).start()


def _get_thread_ts_from_body(body: dict) -> str | None:
    """从 Slack action body 中提取 thread_ts

    按钮消息可能在 thread 内也可能不在，需要从 message 中提取。

    Args:
        body: Slack interaction payload

    Returns:
        thread_ts 字符串，或 None
    """
    message = body.get("message", {})
    return message.get("thread_ts") or message.get("ts")


# ══════════════════════════════════════════════════════════
#  Auto 命令流程（原有逻辑，完全不变）
# ══════════════════════════════════════════════════════════

def _handle_auto_command(cmd, channel: str, user: str, say, client):
    """处理 auto 指令 — 与原有逻辑完全一致

    Args:
        cmd:     AutoCommand 数据类 (action, count, times)
        channel: 频道 ID
        user:    用户 ID
        say:     Slack say 函数
        client:  Slack WebClient
    """
    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        say(":warning: This channel is not linked to any merchant. Please check merchant configuration.")
        return

    merchant_id = merchant_cfg["merchant_id"]
    store_name = merchant_cfg.get("store_name", merchant_id)

    log.info("[%s] 收到指令: %s (channel=%s, user=%s)", merchant_id, cmd.action, channel, user)

    # ── 立即生成 ──────────────────────────────────────────
    if cmd.action == "generate":
        count = cmd.count

        init_resp = client.chat_postMessage(
            channel=channel,
            text=f"Generating {count} blog post(s) for {store_name}...",
            blocks=build_generating_message(store_name, count),
        )
        progress_ts = init_resp["ts"]

        def _do_generate():
            try:
                def _on_progress(stage, extra="", post_index=1, post_total=count):
                    try:
                        blocks = build_progress_blocks(
                            store_name, stage,
                            post_index=post_index, post_total=post_total,
                            extra_info=extra,
                            auto_publish=False,
                        )
                        client.chat_update(
                            channel=channel, ts=progress_ts,
                            text=f"Generating blog for {store_name}...",
                            blocks=blocks,
                        )
                    except Exception:
                        pass

                results = generate_multiple_blogs(
                    merchant_id, merchant_cfg, count,
                    progress_cb=_on_progress,
                    auto_publish=False,
                )

                try:
                    done_blocks = build_progress_blocks(
                        store_name, "done",
                        post_index=count, post_total=count,
                    )
                    client.chat_update(
                        channel=channel, ts=progress_ts,
                        text=f"Blog generation complete for {store_name}",
                        blocks=done_blocks,
                    )
                except Exception:
                    pass

                blocks = build_batch_summary_blocks(results, store_name)
                client.chat_postMessage(
                    channel=channel,
                    text=f"Generated {len(results)} blog posts for {store_name}",
                    blocks=blocks,
                )
            except Exception as exc:
                log.exception("[%s] 生成失败: %s", merchant_id, exc)
                client.chat_postMessage(
                    channel=channel,
                    text=f":x: Blog generation failed: {exc}",
                )

        thread = threading.Thread(target=_do_generate, daemon=True,
                                  name=f"gen-{merchant_id}")
        thread.start()

    # ── 开启定时调度 ──────────────────────────────────────
    elif cmd.action == "schedule_on":
        times = scheduler.schedule_on(merchant_id, channel, cmd.times or None)
        times_str = ", ".join(times)
        say(f":white_check_mark: *Schedule activated for {store_name}*\n"
            f"Posts will be generated daily at: *{times_str}*\n"
            f"Each time slot will trigger a fresh keyword scrape + blog generation.")

    # ── 关闭定时调度 ──────────────────────────────────────
    elif cmd.action == "schedule_off":
        scheduler.schedule_off(merchant_id)
        say(f":no_entry_sign: *Schedule deactivated for {store_name}*\n"
            f"No more automatic blog generation. Use `auto` to generate manually.")

    # ── 查看状态 ──────────────────────────────────────────
    elif cmd.action == "status":
        status = scheduler.get_schedule_status(merchant_id)
        recent_drafts = get_drafts(merchant_id, limit=5)
        blocks = build_schedule_status_blocks(
            store_name,
            status.get("active", False),
            status.get("times", []),
            recent_drafts,
        )
        say(text=f"Schedule status for {store_name}", blocks=blocks)


# ══════════════════════════════════════════════════════════
#  Publish 指令 + 按钮（原有逻辑，完全不变）
# ══════════════════════════════════════════════════════════

def _handle_publish(text: str, channel: str, say, client) -> None:
    """处理 publish 指令 — 手动发布最近一篇草稿到 WordPress

    从 blog_store 获取最近的草稿，读取对应的 HTML 文件，
    然后通过 WordPressPublisher 发布到 WordPress（status=private）。

    用法：
        @Bot publish        → 发布最近一篇草稿
    """
    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        say(":warning: This channel is not linked to any merchant.")
        return

    merchant_id = merchant_cfg["merchant_id"]
    store_name = merchant_cfg.get("store_name", merchant_id)
    wp_url = merchant_cfg.get("wordpress_url", "")

    if not wp_url:
        say(f":warning: *{store_name}* has no WordPress URL configured in merchant.json.")
        return

    drafts = get_drafts(merchant_id, limit=1)
    if not drafts:
        say(f":warning: No drafts found for *{store_name}*. Generate a blog first with `@bot auto 1`.")
        return

    # ── 幂等认领 ──
    # `publish` 指令连打两次（或和按钮点击撞上）同样会重复发布，所以走同一套认领机制
    session_id = drafts[0].get("session_id", "")
    claimed, draft = try_claim_publish(merchant_id, session_id)
    if not claimed:
        status = draft.get("status", "")
        if status == "published":
            say(f":information_source: *{draft.get('title', '')}* 已经发布过了：<{draft.get('wp_post_url', '')}|View on WordPress>")
        elif status == "publishing":
            say(":hourglass_flowing_sand: This draft is already being published, please wait...")
        else:
            say(f":warning: Draft not found (session: `{session_id}`)")
        return

    draft_title = draft.get("title", "Untitled")

    say(f":outbox_tray: Publishing *{draft_title}* to WordPress (private)...")

    try:
        publisher = WordPressPublisher(merchant_id, merchant_cfg)

        blog_data = {
            "title": draft_title,
            "content_html": draft.get("content_html", ""),
            "excerpt": draft.get("excerpt", ""),
            "tags": draft.get("tags", []),
            "seo_slug": draft.get("seo_slug", ""),
            "image_alts": draft.get("image_alts", {}),
        }

        image_paths = {}
        saved_paths = draft.get("image_paths", {})
        for slot, path_str in saved_paths.items():
            p = Path(path_str)
            if p.exists():
                image_paths[slot] = p

        wp_result = publisher.publish_blog(
            blog_data=blog_data,
            image_paths=image_paths,
            status="private",
        )

        if wp_result.get("success"):
            mark_published(merchant_id, session_id, wp_result)   # 提交状态，之后再点会被拒
            post_url = wp_result.get("post_url", "")
            edit_url = wp_result.get("edit_url", "")
            img_count = wp_result.get("images_uploaded", 0)
            tag_count = wp_result.get("tags_count", 0)
            image_names = wp_result.get("image_names", {})
            img_lines = "\n".join(
                f"  • `{slot}`: `{name}`" for slot, name in image_names.items()
            ) if image_names else "  (none)"
            say(
                f":white_check_mark: *Published to WordPress!*\n\n"
                f":page_facing_up: *Title:* {draft_title}\n"
                f":framed_picture: *Images:* {img_count}/3 uploaded\n{img_lines}\n"
                f":label: *Tags:* {tag_count}\n"
                f":lock: *Status:* Private\n"
                f":link: *Post:* <{post_url}|View on WordPress>\n"
                f":pencil2: *Edit:* <{edit_url}|Open in WP Admin>"
            )
        else:
            release_publish_claim(merchant_id, session_id)   # 失败要释放，否则永久卡 publishing
            error = wp_result.get("error", "Unknown error")
            say(f":x: *WordPress publish failed:* {error}")

    except Exception as exc:
        release_publish_claim(merchant_id, session_id)
        log.exception("[%s] Manual publish failed: %s", merchant_id, exc)
        say(f":x: *Publish error:* {exc}")


@app.action(re.compile(r"^wp_publish_"))
def handle_publish_button(ack, body, client) -> None:
    """处理 Slack 中 Publish 按钮的点击事件

    按钮的 action_id 格式: wp_publish_{session_id}
    通过 session_id 找到对应的草稿，发布到 WordPress。
    同时服务 Auto 流程和 Chat 流程的 Publish 按钮。
    """
    ack()

    action_id = body["actions"][0]["action_id"]
    session_id = action_id.replace("wp_publish_", "")
    channel = body["channel"]["id"]
    user = body["user"]["id"]

    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        client.chat_postMessage(channel=channel, text=":warning: This channel is not linked to any merchant.")
        return

    merchant_id = merchant_cfg["merchant_id"]
    store_name = merchant_cfg.get("store_name", merchant_id)

    # ── 幂等认领 ──★ 修 bug：用户双击 Publish 会触发两次 action，
    #    旧代码直接发布 → WordPress 上出现两篇一样的文章。
    #    try_claim_publish 用「持久化的状态机」把发布权原子地锁给第一次点击。
    thread_ts = body.get("message", {}).get("thread_ts")
    claimed, draft = try_claim_publish(merchant_id, session_id)
    if not claimed:
        status = draft.get("status", "")
        if status == "published":
            msg = (f":information_source: *{draft.get('title', '')}* 已发布过了："
                   f"<{draft.get('wp_post_url', '')}|View on WordPress>")
        elif status == "publishing":
            msg = ":hourglass_flowing_sand: Already publishing this draft, please wait..."
        else:
            msg = f":warning: Draft not found (session: `{session_id}`)"
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)
        return

    draft_title = draft.get("title", "Untitled")

    # 如果是 Chat 流程的 Publish，标记会话为 DONE
    if thread_ts:
        sess_key = chat_session.make_key(channel, thread_ts)
        if chat_session.get(sess_key):
            chat_session.update_stage(sess_key, DONE)

    progress_resp = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f":hourglass_flowing_sand: Publishing *{draft_title}* to WordPress...",
    )
    progress_ts = progress_resp["ts"]

    log.info("[%s] Publish 按钮点击: session=%s title=%s user=%s",
             merchant_id, session_id, draft_title, user)

    try:
        publisher = WordPressPublisher(merchant_id, merchant_cfg)

        blog_data = {
            "title": draft_title,
            "content_html": draft.get("content_html", ""),
            "excerpt": draft.get("excerpt", ""),
            "tags": draft.get("tags", []),
            "seo_slug": draft.get("seo_slug", ""),
            "image_alts": draft.get("image_alts", {}),
        }

        image_paths = {}
        saved_paths = draft.get("image_paths", {})
        for slot, path_str in saved_paths.items():
            p = Path(path_str)
            if p.exists():
                image_paths[slot] = p
            else:
                log.warning("[%s] 图片文件不存在: %s", merchant_id, path_str)

        wp_result = publisher.publish_blog(
            blog_data=blog_data,
            image_paths=image_paths,
            status="private",
        )

        if wp_result.get("success"):
            mark_published(merchant_id, session_id, wp_result)   # 提交，之后再点直接被拒
            post_url = wp_result.get("post_url", "")
            edit_url = wp_result.get("edit_url", "")
            img_count = wp_result.get("images_uploaded", 0)
            tag_count = wp_result.get("tags_count", 0)
            image_names = wp_result.get("image_names", {})
            img_lines = "\n".join(
                f"  • `{slot}`: `{name}`" for slot, name in image_names.items()
            ) if image_names else "  (none)"
            publish_text = (
                f":white_check_mark: *Published to WordPress!*\n"
                f":page_facing_up: *Title:* {draft_title}\n"
                f":framed_picture: *Images:* {img_count}/3 uploaded\n{img_lines}\n"
                f":label: *Tags:* {tag_count}\n"
                f":lock: *Status:* Private\n"
                f":link: *Post:* <{post_url}|View on WordPress>\n"
                f":pencil2: *Edit:* <{edit_url}|Open in WP Admin>"
            )
            client.chat_update(
                channel=channel, ts=progress_ts,
                text=f"Published: {draft_title}",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": publish_text},
                }],
            )
        else:
            release_publish_claim(merchant_id, session_id)   # 失败释放，用户可重试
            error = wp_result.get("error", "Unknown error")
            client.chat_update(
                channel=channel, ts=progress_ts,
                text=f":x: *Publish failed:* {draft_title}: {error}",
            )

    except Exception as exc:
        release_publish_claim(merchant_id, session_id)
        log.exception("[%s] Button publish failed: %s", merchant_id, exc)
        client.chat_postMessage(channel=channel, text=f":x: *Publish error:* {exc}")


@app.event("app_mention")
def handle_mention(event: dict, say, client) -> None:
    """app_mention 事件 — 仅处理 message 事件遗漏的情况

    Slack 对 @mention 消息会同时触发 message + app_mention 两个事件。
    message handler 已经处理了大部分情况，这里只在 message 事件未处理时补充
    （例如订阅范围/权限差异导致 message 事件没推过来）。

    去重完全交给 handle_message 里的 _claim_event —— 认领失败它自己会 return。
    旧代码在这里先 peek 一次 `ts in _processed_events` 再调用，等于把
    "查" 和 "写" 拆到了两个函数里，中间的时间差正是并发双重处理的窗口。
    """
    log.info("app_mention 事件: user=%s thread_ts=%s ts=%s",
             event.get("user"), event.get("thread_ts"), event.get("ts", ""))
    handle_message(event, say, client)


# ══════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════

def _safe_run(func, *args):
    """安全运行函数 — 捕获异常并记录日志，防止后台线程崩溃"""
    try:
        func(*args)
    except Exception:
        log.exception("后台线程执行出错: %s", func.__name__)


# ══════════════════════════════════════════════════════════
#  启动
# ══════════════════════════════════════════════════════════

def main() -> None:
    """主启动函数"""
    global _bot_user_id

    log.info("=" * 60)
    log.info("Digital Blog Worker — Starting up")
    log.info("=" * 60)

    # 1. 验证必需的配置
    if not cfg.SLACK_BOT_TOKEN or not cfg.SLACK_APP_TOKEN:
        log.error("缺少 SLACK_BOT_TOKEN 或 SLACK_APP_TOKEN")
        sys.exit(1)

    if not cfg.OPENAI_API_KEY:
        log.error("缺少 OPENAI_API_KEY")
        sys.exit(1)

    # 2. 加载所有商家配置和 Agent 人格
    log.info("Step 1: 加载商家配置...")
    load_all_merchants()

    # 3. 获取 Bot 自身 user_id（用于防回环）
    log.info("Step 2: 获取 Bot 身份...")
    auth_resp = app.client.auth_test()
    _bot_user_id = auth_resp.get("user_id", "")
    log.info("Bot user_id: %s", _bot_user_id)

    # 4. 恢复上次进程留下的 chat 会话（重启后 thread 里能继续对话）
    log.info("Step 3: 恢复 chat 会话...")
    chat_session.load_from_disk()

    # 5. 启动预览服务器
    log.info("Step 4: 启动预览服务器...")
    start_preview_server()

    # 6. 启动定时调度器（内部会顺带从磁盘恢复各商家的 auto on 闹钟）
    log.info("Step 5: 启动定时调度器...")
    scheduler.init(app.client)

    # 7. 启动 Slack Socket Mode
    log.info("Step 6: 启动 Slack Socket Mode...")
    log.info("=" * 60)
    log.info("Bot is ready! Listening for commands and chat in merchant channels.")
    log.info("=" * 60)

    handler = SocketModeHandler(app, cfg.SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
