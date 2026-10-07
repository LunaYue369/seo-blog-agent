"""Chat 会话状态管理 — 每个 Slack thread 对应一个独立会话

状态机（与 auto 流程完全隔离，仅用于 @Bot chat / 自由对话）：

  GATHERING  →  CONFIRMING  →  GENERATING  →  REVIEWING  →  DONE
       ^            |                                        |
       │            └── (用户继续修改 → 回到 GATHERING)        │
       └─────────────────────────────────────────────────────┘  (用户要求修改时回到 GATHERING)

每个会话存储（按职责分五组，详见 get_or_create 的返回示例）：
  身份    key / thread_ts / channel   ★ 三个字段分工不同，别混用
  状态机  stage                       决定"用户此刻打字是什么意思"
  记忆    messages[] / params{} / creative_brief{} / user_image_requests{}
          / user_images[] / language
  产物    draft{}                     生成结果，供改稿和 Publish 用
  计量    usage{} / created_at / last_active_at
                                      成本统计 + 过期清理（按 last_active_at，LRU 语义）

使用示例：
  # 用户在 Slack 发送 "@Bot 帮我写一篇 Tesla Model Y 贴膜的文章"
  sess = get_or_create("1712100000.123456", "C0000000001")
  # → sess["key"] == "C0000000001:1712100000.123456"

  # ★ 之后所有操作都用 sess["key"]，不要用裸 thread_ts（见下面 _sessions 的说明）
  add_message(sess["key"], "user", "帮我写一篇...")
  if try_begin_generating(sess["key"]):      # 原子 CAS，防双击并发
      ...
  update_stage(sess["key"], REVIEWING)

  # 发消息回 Slack 时才用 thread_ts
  say(text="...", thread_ts=sess["thread_ts"])

线程安全：所有操作都通过 _lock 保护，可在多个 Slack 事件线程中并发调用。
持久化：任何改动都会写 store/chat_sessions.json，启动时 load_from_disk() 恢复。
"""

import json
import logging
import threading
import time

import config as cfg

log = logging.getLogger(__name__)

# ── 全局线程锁 — 保护 _sessions 字典的读写 ──
_lock = threading.Lock()

# ── 所有活跃会话 ──────────────────────────────────────────
# key = "{channel}:{thread_ts}" 的复合键
# 示例: {"C0000000001:1234567890.123456": {会话数据...}, ...}
#
# 【为什么不能只用 thread_ts 当 key（这是修掉的一个真实缺陷）】
# Slack 对 ts 的承诺是「**频道内**唯一」，**没有承诺跨频道唯一**。
# 最硬的证据是 Slack 自己的 API 长相：凡是定位一条消息的接口都要求同时给
# channel 和 ts（conversations_replies / chat_update / reactions_add ...）——
# 如果 ts 自己就能唯一定位消息，Slack 根本不需要让你传 channel。
# **API 签名就是设计者在说：消息的主键是 (channel, ts) 这个组合。**
#
# 只用 ts 做 key 的后果（概率极低但是静默的跨租户串台）：
#   两个商家频道在同一微秒各发一条消息 → ts 相同 →
#   B 商家的消息命中 A 商家的会话 → GPT 读到 A 的对话历史/参数/草稿（跨租户泄露）
#   → _handle_chat_message 里用 sess["channel"] 反查商家 → 拿到 A 的人格和 WordPress
#   → 而 say 是 Bolt 按【事件的】channel 绑的 → 回复发到 B 频道
#   一半用事件的 channel、一半用会话的 channel = 行为不一致，极难排查。
#
# 所以 key 必须带上租户维度。这不只是修 bug，也是把我们的数据模型
# 对齐到 Slack 的数据模型上。
_sessions: dict[str, dict] = {}


def make_key(channel: str, thread_ts: str) -> str:
    """构造会话的复合主键。

    Args:
        channel:   Slack 频道 ID，如 "C0000000001"
        thread_ts: thread 根消息的 ts，如 "1712100000.123456"

    Returns:
        "C0000000001:1712100000.123456"

    用 ":" 分隔是安全的：Slack 的 channel ID（C/D/G 开头的大写字母数字）
    和 ts（数字+小数点）都不含冒号，所以不会有歧义。
    """
    return f"{channel}:{thread_ts}"

# ── 会话状态常量 ──────────────────────────────────────────
GATHERING = "gathering_info"   # 正在收集信息（对话中）
CONFIRMING = "confirming"      # 参数确认中，等待用户点 Confirm 或继续修改
GENERATING = "generating"      # 正在调用 pipeline 生成博客
REVIEWING = "reviewing"        # 生成完毕，等待用户审核/修改
DONE = "done"                  # 本轮完成（用户满意或已发布）

# ── 持久化 ────────────────────────────────────────────────
# 旧行为：会话只在内存，Bot 一重启（改代码、掉线、机器重启）所有 thread 的
# 对话历史/已上传图片/草稿全部消失。用户在 thread 里继续说话时，
# main.py:140 的 `chat_session.get(thread_ts)` 返回 None → 消息被当成
# 「非 @mention 的普通消息」直接 return，Bot 表现为**彻底不理人**（连报错都没有）。
# 现在每次改动都落盘，启动时 load_from_disk() 恢复。
_STORE_FILE = cfg.STORE_DIR / "chat_sessions.json"

# 需要特殊处理的字段：set 不能直接进 JSON，存盘转 list、读盘转回 set
_SET_FIELDS = ("_downloaded_file_ids",)


def _to_jsonable(sess: dict) -> dict:
    """把一个会话转成可 JSON 序列化的形状（set → list）。"""
    out = dict(sess)
    for field in _SET_FIELDS:
        if field in out and isinstance(out[field], set):
            out[field] = sorted(out[field])
    return out


def _save_unlocked() -> None:
    """把全部会话写盘。★调用方必须已持有 _lock★（内部不再抢锁，避免死锁）。

    写盘失败只记日志不抛异常 —— 持久化是增强功能，不能让它把正在进行的
    对话流程带崩（磁盘满/权限问题时宁可退回「重启失忆」的旧行为）。
    """
    try:
        data = {ts: _to_jsonable(s) for ts, s in _sessions.items()}
        _STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换，防止进程正好在写一半时被杀 → 留下半个坏 JSON
        tmp = _STORE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp.replace(_STORE_FILE)
    except Exception as e:
        log.warning("会话持久化失败（忽略，仅影响重启后的恢复）: %s", e)


def persist() -> None:
    """公开的存盘入口 —— 给「绕过本模块直接改 sess dict」的调用方用。

    本模块自己的 update_stage / add_message / add_user_image / add_usage
    都会自动存盘；但 conversation.py 和 chat_generator.py 有几处是直接
    `sess["creative_brief"] = ...` / `sess["draft"] = ...` 这样改的，
    那些地方改完要手动调一次本函数，否则重启后丢的正是最关键的草稿。
    """
    with _lock:
        _save_unlocked()


def load_from_disk() -> int:
    """启动时恢复会话（由 main.py 在起 Slack 之前调用一次）。

    Returns:
        恢复的会话数量

    两处「恢复时必须修正」的状态：
      1. set 字段从 list 转回 set（_downloaded_file_ids 否则会变成 list，
         `add` 方法不存在 → 图片去重逻辑 AttributeError）。
      2. **stage == GENERATING 的会话要归一化**：进程是在生成中被杀的，
         那个后台线程早没了，没人会再把它推进到 REVIEWING。
         若原样恢复，用户在 thread 里说什么都只会收到「还在生成中，请稍候」
         （main.py:307-309），这个 thread 永久卡死。
         → 有草稿的退回 REVIEWING，没草稿的退回 GATHERING。
    """
    if not _STORE_FILE.exists():
        return 0

    try:
        with open(_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.error("会话文件损坏，跳过恢复: %s", e)
        return 0

    restored = 0
    fixed_stage = 0
    migrated = 0
    with _lock:
        for stored_key, sess in (data or {}).items():
            if not isinstance(sess, dict):
                continue

            for field in _SET_FIELDS:
                sess[field] = set(sess.get(field) or [])

            # ── 3. 旧格式 key 迁移 ──
            # 改用复合键之前，文件里的 key 是裸 thread_ts（不含 channel）。
            # 会话体内部本来就存了 channel，所以可以无损重建正确的 key。
            # 不迁移的后果：老会话用旧 key 存在内存里，而新代码用复合键去查 →
            # 永远查不到 → 用户在老 thread 里继续说话，Bot 完全不理人。
            channel = sess.get("channel", "")
            thread_ts = sess.get("thread_ts", stored_key)
            key = sess.get("key") or (make_key(channel, thread_ts) if channel else stored_key)
            if key != stored_key:
                migrated += 1
            sess["key"] = key
            sess.setdefault("thread_ts", thread_ts)

            # 老记录没有 last_active_at 字段 → 用 created_at 回填，
            # 否则它们会被下一次 cleanup_old 当成"极老"全部清掉
            if not sess.get("last_active_at"):
                sess["last_active_at"] = sess.get("created_at") or time.time()

            if sess.get("stage") == GENERATING:
                sess["stage"] = REVIEWING if sess.get("draft") else GATHERING
                fixed_stage += 1

            _sessions[key] = sess
            restored += 1

    log.info("已恢复 %d 个 chat 会话（%d 个卡在 generating 已归位，%d 个旧格式 key 已迁移）",
             restored, fixed_stage, migrated)
    return restored


def get_or_create(thread_ts: str, channel: str) -> dict:
    """获取已有会话，或创建一个新会话

    Args:
        thread_ts: Slack 消息的 thread timestamp（线程唯一标识）
                   示例: "1234567890.123456"
        channel:   Slack 频道 ID
                   示例: "C0000000001"

    Returns:
        会话字典，包含完整的会话状态。示例:
        {
            # ── 身份（三个字段各有分工，别混用）──
            "key":       "C0000000001:1712100000.123456",   # 会话主键（查会话）
            "thread_ts": "1712100000.123456",               # 纯 Slack ts（发消息到 thread）
            "channel":   "C0000000001",                     # 频道 ID（反查商家）
            # ── 状态机 ──
            "stage": "gathering_info",
            # ── 记忆（喂给 GPT / 喂给 pipeline）──
            "messages": [],              # 对话历史（滑动窗口，最多 20 条）
            "params": {},                # GPT 逐轮抽取的结构化参数
            "creative_brief": {},        # 综合多轮对话生成的"导演脚本"
            "user_image_requests": {},   # 用户对每张图的自然语言要求
            "user_images": [],           # 用户上传图片的本地路径
            "language": "en",            # UI 语言（每条消息用 resolve_language 更新）
            # ── 产物 ──
            "draft": {},                 # {"result": pipeline 返回的 dict, "session_id": "..."}
            # ── 计量与生命周期 ──
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "api_calls": 0, "estimated_cost": 0.0},
            "created_at": 1712100000.0,      # 仅用于统计/排查
            "last_active_at": 1712100500.0,  # ★ cleanup_old 按这个判过期（LRU）
            "_downloaded_file_ids": set(),  # 内部字段：已下载的 Slack file ID（存盘时转 list）
        }
    """
    key = make_key(channel, thread_ts)
    with _lock:
        if key not in _sessions:
            _sessions[key] = {
                # ★ 三个身份字段各有分工，别混用：
                #   key       = 会话主键（查会话用，带租户维度）
                #   thread_ts = 纯 Slack ts（调 Slack API 发消息到 thread 时用）
                #   channel   = 频道 ID（反查商家配置时用）
                "key": key,
                "thread_ts": thread_ts,
                "channel": channel,
                "stage": GATHERING,
                "messages": [],           # 完整对话历史，格式: [{"role": "user"/"assistant", "content": "..."}]
                "params": {},             # GPT 提取的生成参数（主题、关键词、图片模式等）
                "creative_brief": {},     # 多轮对话综合生成的创意简报（内容结构、用户文本、语气等）
                                          # 示例: {
                                          #   "content_structure": [{"section": "开头", "requirement": "..."}],
                                          #   "user_provided_text": {"price_data": "Model Y 全车 $6500"},
                                          #   "tone": "professional but approachable",
                                          #   "special_requests": "强调XPEL认证"
                                          # }
                "user_image_requests": {}, # 用户对每张图的自然语言描述
                                          # 示例: {"img_2": "夕阳下Tesla侧面施工特写，暖色调",
                                          #         "img_4": "价格对比图，简洁风格"}
                "user_images": [],        # 用户上传的图片本地路径列表
                "language": "en",         # 用户语言（"zh" 或 "en"），根据首条消息自动检测
                "draft": {},              # 当前草稿: {"result": pipeline 返回的 dict, "session_id": "..."}
                "usage": {                # 本次会话的 token 用量统计
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "api_calls": 0,
                    "estimated_cost": 0.0,
                },
                "created_at": time.time(),      # 只用于统计/排查，不再用于过期判断
                "last_active_at": time.time(),   # ★ 过期判断看这个（LRU 语义）
                "_downloaded_file_ids": set(),  # 已下载的 Slack file ID，防 message+app_mention 双重下载
            }
            _save_unlocked()
        return _sessions[key]


def get(key: str) -> dict | None:
    """获取已有会话（不创建）

    Args:
        key: 会话复合主键（用 make_key(channel, thread_ts) 构造）
             ⚠️ 不是裸的 thread_ts —— 见文件顶部关于跨频道 ts 碰撞的说明

    Returns:
        会话字典，不存在时返回 None
    """
    with _lock:
        return _sessions.get(key)


def update_stage(key: str, stage: str):
    """更新会话状态

    Args:
        key:   会话复合主键（make_key 构造）
        stage:     新状态（GATHERING / GENERATING / REVIEWING / DONE）

    示例:
        update_stage("1234567890.123456", GENERATING)
        # → 会话从 gathering_info 变为 generating
    """
    with _lock:
        if key in _sessions:
            _sessions[key]["stage"] = stage
            _touch_unlocked(_sessions[key])
            _save_unlocked()


def try_begin_generating(key: str) -> bool:
    """原子地把会话推进到 GENERATING —— 已经在生成中则拒绝。

    【要解决的 bug】
    Confirm & Generate / Regenerate 两个按钮，用户双击就会触发两次 action，
    各起一个后台线程跑 run_chat_pipeline。而 chat 流程**没有商家级锁**
    （锁只做在 blog_generator 的 auto 流程里），于是：
      - 两条 pipeline 同时跑同一个会话，各烧一遍 GPT + Seedream 的钱
      - 两边都往 sess["draft"] 写，最终留下哪个版本取决于线程调度（竞态）
      - 用户会收到两张结果卡，不知道该点哪个 Publish

    【为什么不能在 handler 里写 `if sess["stage"] == GENERATING: return`】
    那是 check-then-act：两个线程可能同时读到 REVIEWING，然后都判定"可以开始"。
    判断和写入必须在同一把锁里，也就是 compare-and-set（CAS）语义。

    Args:
        key: 会话复合主键

    Returns:
        True  = 抢到了，调用方可以起 pipeline 线程
        False = 已在生成中（或会话不存在），调用方应提示用户等待

    示例:
        if not session.try_begin_generating(thread_ts):
            say("还在生成中，请稍候")
            return
    """
    with _lock:
        sess = _sessions.get(key)
        if sess is None:
            return False
        if sess.get("stage") == GENERATING:
            return False          # 已经有一条 pipeline 在跑
        sess["stage"] = GENERATING
        _touch_unlocked(sess)
        _save_unlocked()
        return True


def _touch_unlocked(sess: dict) -> None:
    """刷新「最后活跃时间」。★调用方必须已持有 _lock★

    【为什么需要这个字段（修掉的一个真 bug）】
    cleanup_old 原来是按 `created_at`（**创建**时间）判断过期的：
        expired = [k for k, s in _sessions.items() if s["created_at"] < cutoff]
    这意味着一个 25 小时前开的 thread，**哪怕用户此刻正在里面聊，也会被清掉**。
    后果：用户下一句话进来 → chat_session.get(key) 返回 None →
    消息被当成"非 @mention 的普通消息"直接 return → **Bot 彻底不理人，连报错都没有**。
    跨天推进一篇稿子（周五提需求、周一继续改）是完全正常的用法，所以这不是边缘情况。

    修法：额外记 `last_active_at`，每次有交互就刷新，按它判断过期。
    这正是 FIFO 和 LRU 的区别 ——
      按 created_at 淘汰 = FIFO（只看什么时候进来的，不管有没有在用）
      按 last_active_at 淘汰 = LRU（最久没被碰的先走，活跃的自动续命）
    **会话管理必须用 LRU 语义，因为"最近说过话"是"马上还会说话"的强信号。**
    """
    sess["last_active_at"] = time.time()


MAX_MESSAGES_PER_THREAD = 20  # 保留最近 N 条（约 10 轮对话）


def add_message(key: str, role: str, content: str):
    """添加一条对话记录到历史（超过上限时丢弃最早的消息）

    Args:
        key:     会话复合主键（make_key 构造）
        role:      "user" 或 "assistant"
        content:   消息文本

    示例:
        add_message("1234567890.123456", "user", "帮我写一篇关于 PPF 贴膜的文章")
        add_message("1234567890.123456", "assistant", "好的！请问想侧重哪个方面？")
    """
    with _lock:
        if key in _sessions:
            msgs = _sessions[key]["messages"]
            msgs.append({
                "role": role,
                "content": content,
            })
            if len(msgs) > MAX_MESSAGES_PER_THREAD:
                _sessions[key]["messages"] = msgs[-MAX_MESSAGES_PER_THREAD:]
            _touch_unlocked(_sessions[key])
            _save_unlocked()


def add_user_image(key: str, path: str):
    """记录用户上传的图片路径（自动去重）

    Args:
        key:  会话复合主键（make_key 构造）
        path:      图片本地路径，如 "/path/to/blog-agent/output/uploads/20260403_120000_photo.jpg"

    示例:
        add_user_image("1234567890.123456", "/path/to/20260403_car.jpg")
        add_user_image("1234567890.123456", "/path/to/20260403_car.jpg")  # 重复，不会再次添加
        # → user_images = ["/path/to/20260403_car.jpg"]
    """
    with _lock:
        if key in _sessions:
            if path not in _sessions[key]["user_images"]:
                _sessions[key]["user_images"].append(path)
                _touch_unlocked(_sessions[key])
                _save_unlocked()


def add_usage(key: str, prompt_tokens: int, completion_tokens: int, cost: float):
    """累加本次会话的 token 用量

    Args:
        key:               会话复合主键（make_key 构造）
        prompt_tokens:     输入 token 数
        completion_tokens: 输出 token 数
        cost:             本次调用的预估费用（美元）

    示例:
        add_usage("1234567890.123456", 500, 200, 0.003)
        # → usage = {"prompt_tokens": 500, "completion_tokens": 200, "api_calls": 1, "estimated_cost": 0.003}
        add_usage("1234567890.123456", 300, 100, 0.002)
        # → usage = {"prompt_tokens": 800, "completion_tokens": 300, "api_calls": 2, "estimated_cost": 0.005}
    """
    with _lock:
        if key in _sessions:
            u = _sessions[key]["usage"]
            u["prompt_tokens"] += prompt_tokens
            u["completion_tokens"] += completion_tokens
            u["api_calls"] += 1
            u["estimated_cost"] = round(u["estimated_cost"] + cost, 6)
            _touch_unlocked(_sessions[key])
            _save_unlocked()


def cleanup_old(max_age_hours: int = 24) -> int:
    """清理超过指定时间的过期会话，释放内存

    Args:
        max_age_hours: 最大存活时间（小时），默认 24 小时

    Returns:
        被清理的会话数量

    示例:
        cleaned = cleanup_old(12)  # 清理超过 12 小时的会话
        # → 返回 3（清理了 3 个过期会话）
    """
    cutoff = time.time() - max_age_hours * 3600
    with _lock:
        # ★ 按 last_active_at（最后活跃时间）判断，不是 created_at（创建时间）——
        #   否则正在聊的老 thread 会被清掉，用户下一句话就没人理了（见 _touch_unlocked 注释）。
        # 三重兜底：last_active_at → created_at → 0
        #   `or` 链条能同时处理"字段不存在"和"字段是 0/None"两种情况；
        #   最终落到 0 表示"极老"，会被清理 —— 对无法判断年龄的脏数据，
        #   宁可清掉（用户重开一个 thread 即可）也不要让它永久占内存。
        expired = [
            k for k, s in _sessions.items()
            if (s.get("last_active_at") or s.get("created_at") or 0) < cutoff
        ]
        for ts in expired:
            del _sessions[ts]
        if expired:
            _save_unlocked()
    if expired:
        log.info("已清理 %d 个超过 %dh 的过期会话", len(expired), max_age_hours)
    return len(expired)
