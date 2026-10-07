"""博客草稿持久化存储 — 每个商家独立 JSON 文件，线程安全

每个商家的草稿存储在单独的文件中：
    store/drafts_harbor_auto_spa.json
    store/drafts_summit_hvac.json

用于去重（避免重复主题）和状态查询。
"""

import json
import logging
import threading
import time
from pathlib import Path

import config as cfg

log = logging.getLogger(__name__)

# 每个商家一把锁，互不影响
_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_lock(merchant_id: str) -> threading.Lock:
    """获取指定商家的存储锁（懒创建，线程安全）"""
    with _locks_lock:
        if merchant_id not in _locks:
            _locks[merchant_id] = threading.Lock()
        return _locks[merchant_id]


def _store_path(merchant_id: str) -> Path:
    """获取商家的草稿存储文件路径"""
    return cfg.STORE_DIR / f"drafts_{merchant_id}.json"


def _load(merchant_id: str) -> list[dict]:
    """加载指定商家的草稿列表"""
    path = _store_path(merchant_id)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("[%s] 草稿存储加载失败: %s", merchant_id, e)
        return []


def _save(merchant_id: str, drafts: list[dict]) -> None:
    """保存指定商家的草稿列表"""
    path = _store_path(merchant_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(drafts, f, indent=2, ensure_ascii=False)
    except OSError as e:
        log.error("[%s] 草稿存储保存失败: %s", merchant_id, e)


def save_draft(
    merchant_id: str,
    title: str,
    filename: str,
    preview_url: str,
    blog_data: dict,
    review_score: int = 0,
    session_id: str = "",
    image_paths: dict[str, str] | None = None,
) -> None:
    """保存一条博客草稿记录

    Args:
        merchant_id: 商家标识
        title: 博客标题
        filename: HTML 文件名
        preview_url: 预览链接
        blog_data: 完整博客数据（title, excerpt, tags, seo_slug 等）
        review_score: Reviewer 评分
        session_id: 生成会话 ID
        image_paths: 图片本地路径 {"hero": "C:/...png", "mid": "...", "end": "..."}
    """
    record = {
        "title": title,
        "filename": filename,
        "preview_url": preview_url,
        "content_html": blog_data.get("content_html", ""),
        "excerpt": blog_data.get("excerpt", ""),
        "tags": blog_data.get("tags", []),
        "seo_slug": blog_data.get("seo_slug", ""),
        "image_alts": blog_data.get("image_alts", {}),
        "review_score": review_score,
        "session_id": session_id,
        "image_paths": image_paths or {},
        "status": "draft",
        "created_at": time.time(),
    }

    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)
        drafts.append(record)
        _save(merchant_id, drafts)

    log.info("[%s] 草稿已保存: %s (score=%d)", merchant_id, title, review_score)


# ══════════════════════════════════════════════════════════
#  发布幂等 —— 认领(claim) → 提交(commit) / 回滚(release)
# ══════════════════════════════════════════════════════════
# 【要解决的 bug】
# Slack 的 Publish 按钮用户手抖点两次，就会触发两次 wp_publish_ action。
# 旧代码在 handler 里直接调 WordPress publish，没有任何"这篇发过没"的判断
# （草稿记录里的 status 永远写死 "draft"，发布成功后从没回写过）
# → WordPress 上出现两篇一模一样的文章。
#
# 【为什么不用内存里的一把锁】
# 内存锁只能防"同一进程内的同时点击"，防不住：
#   - 进程重启后用户再点一次（锁没了，但文章已经发过）
#   - 以后多实例部署（每个进程一把锁，等于没锁）
# 所以状态必须写进那条草稿记录本身（持久化的状态机），这才是真正的幂等键：
#
#   draft            ← 刚生成，可发布
#     ↓ try_claim_publish() 抢到
#   publishing       ← 正在发，此时任何人再点都会被拒
#     ↓ 成功                    ↓ 失败
#   published        release_publish_claim() → 退回 draft，允许重试
#
# 【为什么这三个函数都在同一把商家锁里】
# "读状态 → 判断 → 写新状态" 必须原子，否则两个线程可能同时读到 "draft"
# 然后都判定"我可以发" —— 这就是 check-then-act 竞态。加锁把三步合成一步。
#
# ══════════════════════════════════════════════════════════
# 【租约（lease）—— 修补"永久卡在 publishing"的洞】
# 上面的状态机有一个致命前提：认领之后一定会有人把状态推进（成功→published
# 或失败→draft）。但如果进程在 publishing 期间**被杀掉**（部署、断电、崩溃、
# Ctrl-C），release_publish_claim 永远不会执行 →
#   这篇草稿永久停在 "publishing" → 用户之后无论怎么点 Publish 都被拒
#   → 表现出来就是"这篇稿子再也发不出去了"，而且没有任何报错线索。
#
# 解法不是"永久占有"，而是"限时占有"：认领时记下开始时间（publish_started_at），
# 判断时多加一条 —— **如果 publishing 已经超过租期，视为上一个执行者已经死了，
# 允许重新认领（接管）**。
#
# 这就是 lease（租约）：**有期限的独占权**。它和普通 lock 的区别是：
#   lock  = 我持有，直到【我主动释放】        → 持有者死了就永久死锁
#   lease = 我持有，直到【我释放 或 租期到】  → 持有者死了，租约自己过期
#
# 【租期怎么定：必须 > 正常操作的最长耗时】
# 发一篇到 WordPress 要上传 3 张 2K 图 + 建 tag + 发文，慢的时候一两分钟。
# 租期设 10 分钟 = 留 5 倍余量。租期太短的后果比太长严重得多：
#   太短 → 正常执行还没做完就被别人抢走 → 两个执行者同时发 → 真的发出两篇（脑裂）
#   太长 → 只是故障后恢复慢一点（用户多等 10 分钟才能重试）
# 所以【宁长勿短】。
_PUBLISH_LEASE_SECONDS = 600   # 10 分钟


def _lease_expired(draft: dict, now: float) -> bool:
    """判断一条 publishing 状态的草稿，它的租约是否已经过期（= 上个执行者大概已经死了）。

    Args:
        draft: 草稿记录
        now:   当前时间戳

    Returns:
        True = 租约已过期，可以被接管

    注意 `publish_started_at` 缺失时按"已过期"处理：那是本次改动之前写入的老记录
    （或被手工编辑过），如果按"未过期"处理就会让它永久锁死 —— 而永久锁死正是
    我们要修的那个 bug 本身。**兜底方向永远要选"可恢复"的那一边。**
    """
    started_at = draft.get("publish_started_at") or 0
    return (now - started_at) >= _PUBLISH_LEASE_SECONDS


def try_claim_publish(merchant_id: str, session_id: str) -> tuple[bool, dict]:
    """尝试「认领」某篇草稿的发布权（原子操作）。

    Args:
        merchant_id: 商家标识
        session_id:  草稿的 session_id（Slack 按钮 action_id 里编码的那个）

    Returns:
        (claimed, draft)
        claimed=True  → 认领成功，调用方可以去发 WordPress
        claimed=False → 要么草稿不存在（draft 为 {}），
                        要么已经在发/已发完（draft 里带 status 和 post_url，
                        调用方应该据此给用户一个友好提示而不是重复发布）

    示例:
        ok, draft = try_claim_publish("harbor_auto_spa", "chat_xxx_123")
        if not ok:
            if draft:  say(f"这篇已经发过了：{draft.get('wp_post_url')}")
            else:      say("找不到这篇草稿")
            return
    """
    now = time.time()
    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)
        for d in drafts:
            if d.get("session_id") != session_id:
                continue

            status = d.get("status", "draft")

            # ── published 是终态：永久拒绝，不受租约影响 ──
            # 已经发到 WordPress 上的事实不会因为时间流逝而失效
            if status == "published":
                log.info("[%s] 发布认领被拒（已发布）: %s", merchant_id, d.get("title", ""))
                return False, dict(d)           # 返回副本，避免调用方误改内存里的记录

            # ── publishing：要看租约还在不在 ──
            if status == "publishing":
                if not _lease_expired(d, now):
                    # 租约有效 = 真的有人正在发，拒绝（这条分支挡的就是"双击"）
                    log.info("[%s] 发布认领被拒（正在发布中，租约未过期）: %s",
                             merchant_id, d.get("title", ""))
                    return False, dict(d)
                # 租约过期 = 上个执行者大概死了（进程被杀/崩溃），接管它
                # 用 warning 级别：这说明发生过一次异常终止，值得被人看到
                log.warning("[%s] publishing 租约已过期 %.0f 秒，接管发布: %s",
                            merchant_id, now - (d.get("publish_started_at") or 0),
                            d.get("title", ""))

            # ── 到这里说明：状态是 draft，或者是租约已过期的 publishing ──
            # 两种情况都执行同样的动作：把租约的开始时间刷新到现在（= 重新计时）
            d["status"] = "publishing"
            d["publish_started_at"] = now
            _save(merchant_id, drafts)
            log.info("[%s] 发布认领成功: %s", merchant_id, d.get("title", ""))
            return True, dict(d)

        return False, {}                        # 草稿不存在


def mark_published(merchant_id: str, session_id: str, wp_result: dict) -> None:
    """发布成功后提交状态 —— 把 status 落成 published 并记下 WP 的 URL/ID。

    Args:
        merchant_id: 商家标识
        session_id:  草稿的 session_id
        wp_result:   WordPressPublisher.publish_blog 的返回值
                     （取其中的 post_url / edit_url / post_id）
    """
    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)
        for d in drafts:
            if d.get("session_id") == session_id:
                d["status"] = "published"
                d["wp_post_url"] = wp_result.get("post_url", "")
                d["wp_edit_url"] = wp_result.get("edit_url", "")
                d["wp_post_id"] = wp_result.get("post_id", 0)
                d["published_at"] = time.time()
                _save(merchant_id, drafts)
                log.info("[%s] 已标记为已发布: %s → %s",
                         merchant_id, d.get("title", ""), d["wp_post_url"])
                return
    log.warning("[%s] mark_published 未找到草稿: %s", merchant_id, session_id)


def release_publish_claim(merchant_id: str, session_id: str) -> None:
    """发布失败时释放认领 —— 退回 draft，让用户可以重试。

    ⚠️ 必须放在 handler 的 except / 失败分支里。漏掉这一步，草稿会永久卡在
       "publishing" 状态，用户再点 Publish 只会被拒，看起来像 Bot 坏了。
    """
    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)
        for d in drafts:
            if d.get("session_id") == session_id and d.get("status") == "publishing":
                d["status"] = "draft"
                d.pop("publish_started_at", None)
                _save(merchant_id, drafts)
                log.info("[%s] 发布失败，认领已释放（可重试）: %s",
                         merchant_id, d.get("title", ""))
                return


def get_recent_titles(merchant_id: str, limit: int = 10) -> list[str]:
    """获取商家最近发布的博客标题（用于去重）

    Args:
        merchant_id: 商家标识
        limit: 最多返回数量

    Returns:
        标题列表（最新的在前）
    """
    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)

    drafts.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return [d["title"] for d in drafts[:limit]]


def get_drafts(merchant_id: str, limit: int = 20) -> list[dict]:
    """获取商家的草稿列表

    Args:
        merchant_id: 商家标识
        limit: 最多返回数量

    Returns:
        草稿记录列表（最新的在前）
    """
    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)

    drafts.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return drafts[:limit]
