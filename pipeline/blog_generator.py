"""博客生成流水线 — 编排各 Agent 协作生成完整 SEO 博客

完整流程：
1. Researcher 爬取热词 → 选出 TOP N 主题
2. Copywriter 根据主题撰写 HTML 博客
3. Reviewer 审核（最多 3 轮，不合格则打回重写）
4. Artist 美化图片 prompt
5. Seedream 生成配图
6. 组装 HTML → 保存文件 → 返回预览链接

线程安全：
- 每个商家有独立的生成锁，同一商家同一时刻只允许一个生成流水线运行
- 防止用户连续发 auto 指令或定时任务与手动指令并发导致主题重复
"""

import logging
import threading
import time
import uuid
from pathlib import Path

import config as cfg
from agents.researcher import analyze_and_pick_topics
from agents.copywriter import write_blog
from agents.artist import enhance_image_prompts
from core.merchant_config import get_seed_keywords
from pipeline.trend_scraper import scrape_trending
from pipeline.preview_server import render_blog_html
from pipeline.review_loop import run_review_loop
from channels.derive import derive_all
from retrieval.catalog import build_catalog_brief, load_catalog
from pipeline.web_researcher import research_topic
from services.template_selector import pick_template_and_layout
from services.seedream_client import SeedreamClient
from services.usage_tracker import record_usage, format_usage_report, save_to_disk, set_current_session
from services.wordpress_publisher import WordPressPublisher
from store.blog_store import save_draft, get_recent_titles

log = logging.getLogger(__name__)

# ── 商家级生成锁 — 防止同一商家并发生成导致主题重复或数据竞争 ──
_merchant_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()  # 保护 _merchant_locks 字典本身


def _get_merchant_lock(merchant_id: str) -> threading.Lock:
    """获取指定商家的生成锁（懒创建，线程安全）"""
    with _locks_lock:
        if merchant_id not in _merchant_locks:
            _merchant_locks[merchant_id] = threading.Lock()
        return _merchant_locks[merchant_id]


# ── 内部生成逻辑（不加锁，由外部调用方负责加锁）──────────────

def _generate_single_inner(
    merchant_id: str,
    merchant_cfg: dict,
    session_id: str,
    topic_index: int = 0,
    pre_scraped_topics: list[dict] | None = None,
    exclude_template: str | None = None,
    exclude_layout: str | None = None,
    progress_cb=None,
    auto_publish: bool = True,
) -> dict:
    """单篇博客生成的核心逻辑（不加锁）
    此函数不持有任何锁，由 generate_single_blog / generate_multiple_blogs 负责加锁后调用。
    在 auto n指令里的generate multiple blogs里被使用
    也在auto on指令里定时发送一篇blog的generate single blog里被使用
    exclude_template: 上一篇用的模板文件名，批量生成时避免连续相同
    exclude_layout: 上一篇用的布局名称，批量生成时避免连续相同
    """
    output_dir = Path(merchant_cfg.get("output_dir", str(cfg.OUTPUT_DIR / merchant_id)))
    output_dir.mkdir(parents=True, exist_ok=True)

    # 设置当前线程的 session_id，所有 agent 的 record_usage 自动关联
    set_current_session(session_id)

    # 计时器（只用于最后算总耗时；原先那套 _t_stages/_t_last 分阶段计时是死代码，从没被读过，已删）
    _t_start = time.time()

    # 进度回调辅助 —— 刷 Slack 进度条失败绝不能把生成流程带崩
    def _progress(stage: str, extra: str = ""):
        if progress_cb:
            try:
                progress_cb(stage, extra)
            except Exception:
                log.warning("[%s] 进度回调失败 (stage=%s)，忽略", merchant_id, stage)

    try:
        # ── Step 1: 获取候选主题 ──────────────────────────────
        if pre_scraped_topics:
            topics = pre_scraped_topics
            log.info("[%s] 使用预爬取的 %d 个候选主题", merchant_id, len(topics))
        else:
            _progress("scrape")
            log.info("[%s] Step 1: 爬取热词...", merchant_id)
            seed_keywords = get_seed_keywords(merchant_id)
            trending_data = scrape_trending(seed_keywords, max_total=cfg.SCRAPE_MAX_ITEMS)
            log.info("[%s] 爬取到 %d 条热词数据", merchant_id, len(trending_data))

            _progress("research")
            recent_titles = get_recent_titles(merchant_id)
            topics, _ = analyze_and_pick_topics(
                merchant_id, trending_data, recent_titles, count=max(3, topic_index + 1)
            )

        if not topics or topic_index >= len(topics):
            return {
                "success": False, "error": "No suitable topics found",
                "session_id": session_id, "title": "", "preview_url": "",
            }

        chosen_topic = topics[topic_index]
        topic_title = chosen_topic.get("title", "")
        log.info("[%s] Step 1 完成 — 选题: %s", merchant_id, topic_title)

        # ── Step 1.5: 选择 HTML 模板 + 内容布局风格 ─────────────
        _progress("template", f"Topic: {topic_title}")
        style_choice = pick_template_and_layout(
            merchant_id,
            merchant_cfg=merchant_cfg,
            exclude_template=exclude_template,
            exclude_layout=exclude_layout,
        )
        log.info("[%s] 风格选择 — 模板: %s | 布局: %s",
                 merchant_id, style_choice["template_name"], style_choice["layout_label"])

        # ── Step 1.8: Web Research — 搜索竞品文章提取要点 ────
        _progress("web", f"Template: {style_choice['template_name']} | Layout: {style_choice['layout_label']}")
        log.info("[%s] Step 1.8: Web Research 调研竞品...", merchant_id)
        research_brief = research_topic(merchant_id, chosen_topic)
        if research_brief:
            log.info("[%s] Step 1.8 完成 — 调研摘要 %d 字符", merchant_id, len(research_brief))
        else:
            log.info("[%s] Step 1.8 跳过 — 无调研数据，继续生成", merchant_id)

        # ── Step 1.9: 商品目录检索（RAG 的 R）──────────────────
        # 按选题从 catalog.json 里挑最相关的 top-3 商品，拼成 prompt 片段。
        # 关键词重叠打分，**刻意不用向量库**（十几条结构化数据引入 embedding 是过度工程，
        # 理由写在 catalog.json 的 _why_no_vector_db 字段里）。
        #
        # ⚠️ 这一步只做**预防**，不提供保证：模型完全可以看着 $32 的目录写出 $29。
        # 提供保证的是出口那道 check_catalog_grounding（在 Step 3 的闸门里，hard 级）。
        # 「grounding + attribution + verification」三件套里，只有第三件是保证。
        catalog = load_catalog(merchant_id)
        catalog_brief = build_catalog_brief(chosen_topic, catalog) if catalog else ""
        if catalog_brief:
            log.info("[%s] Step 1.9: 目录检索完成 — 注入 %d 个商品到 prompt（共 %d 个）",
                     merchant_id, catalog_brief.count("- **"), len(catalog.get("products", [])))
        else:
            # 没有目录不是错误，是租户差异（服务业商家没有 SKU）——
            # 但要留一行日志，否则"接地校验为什么跳过了"会变成一个谜。
            log.info("[%s] Step 1.9: 该商家无 catalog.json，跳过目录检索（接地校验也会跳过）",
                     merchant_id)

        # ── Step 2: Copywriter 撰写博客 ──────────────────────
        _progress("write", f"Topic: {topic_title}")
        log.info("[%s] Step 2: Copywriter 撰写博客...", merchant_id)
        blog_data, _ = write_blog(
            merchant_id, chosen_topic,
            layout_prompt=style_choice["layout_prompt"],
            research_brief=research_brief,
            catalog_brief=catalog_brief,
        )
        log.info("[%s] Step 2 完成 — '%s'", merchant_id, blog_data.get("title", ""))

        # ── Step 3: 质量闸门（确定性校验 → Reviewer 审核，最多 N 轮）──
        # 收口到 pipeline/review_loop.py，与 chat 流程共用同一份实现。
        # ⚠️ 注意 blog_data 会被 run_review_loop 原地修改（重写后的内容写回同一个 dict），
        #    所以这里不需要接返回值 —— 后面的 artist / render 直接用 blog_data 就是最新稿。
        #    旧代码用 `blog_data, _ = rewrite_blog(...)` 整体替换，会把 image_alts 冲掉（详见该模块注释）。
        #
        # 第三个返回值 seo_report 是**确定性校验报告**（DeterministicReport），描述的是
        # 最终这一版稿子：hard_ok / passed_checks（绿勾）/ failures（红叉带数字）/ metrics。
        # 它会被塞进 result 字典，最终渲进 Slack 结果卡片。
        review_score, review_rounds, seo_report = run_review_loop(
            merchant_id, blog_data, chosen_topic,
            merchant_cfg=merchant_cfg,   # 校验器要用它的 website 区分内链/外链
            mode="auto", image_count=3,
            progress_cb=_progress,
        )

        # ── Step 3.5: 渠道派生（内容原子 → N 个渠道投影）────────
        # 放在质量闸门**之后**：只有已经定稿、过了检查的文章才值得派生 ——
        # 从一篇会被打回重写的稿子派生邮件，派完就作废，纯浪费。
        #
        # 每条派生都过**同一套 eval 校验**（品牌禁词/绝对化承诺/价格接地），
        # 走的是 eval 里同一个函数、同一张严重度表，不是复制一份"邮件版检查"。
        # 这是"内容原子全渠道"能成立的前提：校验层对渠道无感知。
        #
        # ⚠️ 派生失败**不阻断文章**：它是附加产物。某条渠道原子编了个价格 →
        # 那条标 blocked，文章照常发布。所以这里不需要 try/except 包住整段。
        channel_atoms = derive_all(merchant_id, blog_data, chosen_topic,
                                   merchant_cfg=merchant_cfg)
        if channel_atoms:
            _ok = sum(1 for a in channel_atoms if not a.blocked)
            log.info("[%s] Step 3.5 完成 — %d/%d 个渠道原子可用（成本 $%.4f）",
                     merchant_id, _ok, len(channel_atoms),
                     sum(a.cost for a in channel_atoms))

        # ── Step 4: Artist 美化图片 prompt ───────────────────
        _progress("artist", f"Score: {review_score}/100")
        log.info("[%s] Step 4: Artist 美化图片 prompt...", merchant_id)
        raw_image_prompts = blog_data.get("image_prompts", {})
        if isinstance(raw_image_prompts, dict) and raw_image_prompts:
            enhanced_prompts, _ = enhance_image_prompts(
                merchant_id, raw_image_prompts,
                blog_data.get("title", ""), blog_data.get("excerpt", ""),
            )
        else:
            enhanced_prompts = {
                "hero": f"Professional high-quality image related to {chosen_topic.get('primary_keyword', '')}",
                "mid": f"Detail shot illustrating {chosen_topic.get('primary_keyword', '')}",
                "end": "Professional business environment, trust and reliability",
            }
        log.info("[%s] Step 4 完成", merchant_id)

        # ── Step 5: Seedream 生成配图 ─────────────────────────
        _progress("image", "Generating 3 images (hero, mid, end)")
        log.info("[%s] Step 5: Seedream 生成配图...", merchant_id)
        image_paths = {}
        seo_slug = blog_data.get("seo_slug", "blog")
        try:
            seedream = SeedreamClient()
            for slot, prompt in enhanced_prompts.items():
                if not prompt:
                    continue
                log.info("[%s] 生成 %s 图片...", merchant_id, slot)
                # 用 商家_slug_时间戳_slot.png 命名，方便辨认且唯一
                img_filename = f"{merchant_id}_{seo_slug}_{int(time.time())}_{slot}.png"
                img_path = output_dir / img_filename
                urls = seedream.generate_image(prompt=prompt, size="2K")
                if urls:
                    seedream.download_image(urls[0], img_path)
                    image_paths[slot] = img_path
                    record_usage(
                        merchant_id, "seedream", cfg.SEEDREAM_MODEL,
                        image_count=1, session_id=session_id,
                    )
        except Exception as img_err:
            log.error("[%s] Seedream 生图失败: %s", merchant_id, img_err)

        log.info("[%s] Step 5 完成 — 生成 %d 张图片", merchant_id, len(image_paths))

        # ── Step 6: 组装 HTML + 保存 ─────────────────────────
        _progress("render", f"{len(image_paths)} images ready")
        log.info("[%s] Step 6: 组装 HTML 并保存...", merchant_id)
        timestamp = int(time.time())
        filename = f"{merchant_id}_{timestamp}_{uuid.uuid4().hex[:4]}.html"
        output_path = output_dir / filename

        preview_url = render_blog_html(
            blog_data=blog_data,
            image_paths=image_paths,
            merchant_cfg=merchant_cfg,
            output_path=output_path,
            template_file=style_choice["template_file"],
        )

        # 保存草稿记录（含图片路径，供 Publish 按钮使用）
        save_draft(
            merchant_id=merchant_id,
            title=blog_data.get("title", "Untitled"),
            filename=filename,
            preview_url=preview_url,
            blog_data=blog_data,
            review_score=review_score,
            session_id=session_id,
            image_paths={slot: str(p) for slot, p in image_paths.items()},
        )
        log.info("[%s] Step 6 完成 — 预览: %s", merchant_id, preview_url)

        # ── Step 7: 发布到 WordPress（auto_publish=True 且商家配了 WP 时）─
        wp_result = {}
        wp_url = merchant_cfg.get("wordpress_url", "")
        if wp_url and auto_publish:
            _progress("publish", "Uploading images & publishing to WordPress")
            log.info("[%s] Step 7: 发布到 WordPress...", merchant_id)
            try:
                publisher = WordPressPublisher(merchant_id, merchant_cfg)
                wp_result = publisher.publish_blog(
                    blog_data=blog_data,
                    image_paths=image_paths,
                    status="private",
                )
                if wp_result.get("success"):
                    log.info("[%s] Step 7 完成 — WP ID=%d URL=%s",
                             merchant_id, wp_result.get("post_id", 0), wp_result.get("post_url", ""))
                else:
                    log.warning("[%s] Step 7 WordPress 发布失败: %s",
                                merchant_id, wp_result.get("error", "Unknown"))
            except Exception as wp_err:
                log.error("[%s] Step 7 WordPress 发布异常: %s", merchant_id, wp_err)
                wp_result = {"success": False, "error": str(wp_err)}
        elif not wp_url:
            log.info("[%s] 跳过 WordPress 发布（未配置 wordpress_url）", merchant_id)
        else:
            log.info("[%s] 跳过 WordPress 发布（手动模式，等待用户确认）", merchant_id)

        usage_report = format_usage_report(session_id)
        total_seconds = int(time.time() - _t_start)
        total_minutes = total_seconds // 60
        total_secs = total_seconds % 60
        generation_time = f"{total_minutes}m {total_secs}s" if total_minutes else f"{total_secs}s"
        log.info("[%s] 全流程完成 (耗时 %s)", merchant_id, generation_time)

        return {
            "success": True,
            "title": blog_data.get("title", "Untitled"),
            "preview_url": preview_url,
            "blog_data": blog_data,
            "review_score": review_score,
            "review_rounds": review_rounds,
            # 确定性校验报告（渲进 Slack 卡片：❌hard / ⚠️soft / 🔧自动修复 三档）。
            # 只传 UI 真正要用的字段，不把整个 dataclass 塞进去 ——
            # result 字典会被 json 序列化进 store/*.json，dataclass 进不去。
            # 渠道原子：摊成可序列化的 dict（result 会被写进 store/*.json）
            "channel_atoms": [
                {
                    "channel": a.channel, "label": a.label, "content": a.content,
                    "blocked": a.blocked, "attempts": a.attempts, "cost": a.cost,
                    "hard_failures": (a.report.hard_failures if a.report else []),
                    "soft_failures": (a.report.soft_failures if a.report else []),
                }
                for a in channel_atoms
            ],
            "seo_checks": {
                "hard_ok": seo_report.hard_ok,
                "passed": seo_report.passed_checks,
                "hard_failures": seo_report.hard_failures,
                "soft_failures": seo_report.soft_failures,
                "repairs": seo_report.repairs,
            },
            "session_id": session_id,
            "usage_report": usage_report,
            "generation_time": generation_time,
            # WordPress 发布结果
            "wp_published": wp_result.get("success", False),
            "wp_post_url": wp_result.get("post_url", ""),
            "wp_edit_url": wp_result.get("edit_url", ""),
            "wp_post_id": wp_result.get("post_id", 0),
            # 记录本篇使用的模板和布局，供批量生成时排除避免连续相同
            "template_file": style_choice["template_file"],
            "template_name": style_choice["template_name"],
            "layout_name": style_choice["layout_name"],
            "layout_label": style_choice["layout_label"],
        }

    except Exception as exc:
        log.exception("[%s] 博客生成失败: %s", merchant_id, exc)
        return {
            "success": False,
            "error": str(exc),
            "session_id": session_id,
            "title": "",
            "preview_url": "",
            "usage_report": format_usage_report(session_id),
        }


# ── 公共 API（加锁包装）─────────────────────────────────────

def generate_single_blog(
    merchant_id: str,
    merchant_cfg: dict,
    session_id: str = "",
    topic_index: int = 0,
    pre_scraped_topics: list[dict] | None = None,
    progress_cb=None,
    auto_publish: bool = True,
) -> dict:
    """生成单篇 SEO 博客（线程安全）

    自动获取商家级锁，同一商家同一时刻只允许一个生成任务运行。
    如果锁已被占用（另一个生成正在进行），立即返回错误而非阻塞等待。
    在auto on启动scheduler的时候使用，也会调用generate_single_inner

    Args:
        merchant_id: 商家标识
        merchant_cfg: 商家配置字典
        session_id: 会话 ID（用于追踪用量）
        topic_index: 使用第几个候选主题（0-based）
        pre_scraped_topics: 预先爬取好的候选主题（多篇生成时复用）

    Returns:
        生成结果字典
    """
    if not session_id:
        session_id = f"{merchant_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    merchant_lock = _get_merchant_lock(merchant_id)
    if not merchant_lock.acquire(timeout=5):
        log.warning("[%s] 生成锁已被占用，当前有其他生成任务正在运行", merchant_id)
        return {
            "success": False,
            "error": "Another generation is already running for this merchant. Please wait.",
            "session_id": session_id, "title": "", "preview_url": "",
        }

    try:
        return _generate_single_inner(
            merchant_id, merchant_cfg, session_id, topic_index, pre_scraped_topics,
            exclude_template=None, exclude_layout=None,
            progress_cb=progress_cb,
            auto_publish=auto_publish,
        )
    finally:
        merchant_lock.release()
        # 每次生成完毕持久化用量数据（防止崩溃丢失）
        try:
            save_to_disk()
        except Exception:
            log.warning("[%s] 用量数据持久化失败", merchant_id)


def generate_multiple_blogs(
    merchant_id: str,
    merchant_cfg: dict,
    count: int = 1,
    progress_cb=None,
    auto_publish: bool = True,
) -> list[dict]:
    """为某一个商家生成多篇 SEO 博客（线程安全）

    先统一爬取热词和选题，再逐篇生成，每篇选不同的主题。
    整个批量过程持有商家锁，防止定时任务与手动指令并发。

    Args:
        merchant_id: 商家标识
        merchant_cfg: 商家配置字典
        count: 生成数量

    Returns:
        结果列表
    """
    log.info("[%s] 开始批量生成 %d 篇博客", merchant_id, count)

    merchant_lock = _get_merchant_lock(merchant_id)
    if not merchant_lock.acquire(timeout=5):
        log.warning("[%s] 生成锁已被占用，跳过批量生成", merchant_id)
        return [{"success": False, "error": "Another generation is already running.", "title": "", "preview_url": ""}]

    try:
        # 统一爬取热搜词条，保留前SCRAPE_MAX_ITEMS
        seed_keywords = get_seed_keywords(merchant_id)
        trending_data = scrape_trending(seed_keywords, max_total=cfg.SCRAPE_MAX_ITEMS)
        log.info("[%s] 爬取到 %d 条热词数据", merchant_id, len(trending_data))

        # 查到最近生成的前10个blogs，避免重复
        recent_titles = get_recent_titles(merchant_id)
        # 把商家id，google search suggetions，最近生成的主题，和想给这个商家生成的blogs的个数都传给researcher人格
        # 来挑选出最佳topics，[{title, keywords, angle, why}, ...]
        topics, _ = analyze_and_pick_topics(
            merchant_id, trending_data, recent_titles, count=count,
        )

        if not topics:
            log.error("[%s] 未能选出任何主题", merchant_id)
            return [{"success": False, "error": "No topics found", "title": "", "preview_url": ""}]

        # 根据每个topic逐篇生成（已持有锁，直接调用内部逻辑避免重复抢锁）
        # 记录上一篇的模板和布局，传给下一篇避免连续重复
        results = []
        prev_template = None
        prev_layout = None
        for i in range(min(count, len(topics))):
            log.info("[%s] 正在生成第 %d/%d 篇...", merchant_id, i + 1, count)
            session_id = f"{merchant_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

            # 批量生成时，包装 progress_cb 加入篇数信息
            def _batch_progress(stage, extra="", _idx=i):
                if progress_cb:
                    progress_cb(stage, extra, post_index=_idx + 1, post_total=count)

            result = _generate_single_inner(
                merchant_id=merchant_id,
                merchant_cfg=merchant_cfg,
                session_id=session_id,
                topic_index=i,
                pre_scraped_topics=topics,
                exclude_template=prev_template,
                exclude_layout=prev_layout,
                progress_cb=_batch_progress,
                auto_publish=auto_publish,
            )
            results.append(result)

            # 记录本篇的模板和布局，下一篇排除
            if result.get("success"):
                prev_template = result.get("template_file")
                prev_layout = result.get("layout_name")

        log.info("[%s] 批量生成完成: %d/%d 成功",
                 merchant_id, sum(1 for r in results if r.get("success")), count)

        return results
    finally:
        merchant_lock.release()
        try:
            save_to_disk()
        except Exception:
            log.warning("[%s] 用量数据持久化失败", merchant_id)
