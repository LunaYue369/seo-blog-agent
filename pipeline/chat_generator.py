"""对话式博客生成 Pipeline — 接收用户对话收集到的参数，生成博客

与 auto 流程（blog_generator.py）的区别：
- auto: trend_scraper 爬热词 → researcher 选题 → 全 AI 生图 → 自动发布
- chat: 用户提供主题 → 可上传图片 → 混合生图 → 预览 + 按钮发布

共享的部分（直接复用 auto 流程的模块）：
- copywriter: 撰写 SEO 博客
- reviewer:   审核评分（最多 3 轮）
- artist:     美化 AI 图片 prompt
- seedream:   生成配图（仅 AI 生成的槽位）
- preview_server: 渲染 HTML 预览
- wordpress_publisher: 发布到 WP
- blog_store: 保存草稿

跳过的部分：
- trend_scraper:  不需要（用户直接给主题）
- researcher:     不需要（用户直接给关键词）
- web_researcher: 可选（根据用户主题自动搜索竞品）

图片处理逻辑：
  博客使用编号槽位: img_1, img_2, ..., img_N（N 由用户决定）

  image_mode="generate" (默认):
    → 所有 N 个槽位由 Seedream AI 生成

  image_mode="user":
    → 用户上传的图片按顺序分配: 图1→img_1, 图2→img_2, ...
    → 不足 N 张时，缺的槽位 AI 生成
    → per_image_modes 控制每张图的处理方式（raw/enhance/reference）

  image_mode="mixed":
    → image_assignments 指定哪些槽位用用户图片: {"img_1": 1, "img_3": 2}
    → generate_slots 指定哪些槽位 AI 生成: ["img_2"]
    → 未指定的槽位也 AI 生成

使用示例:
    # 在 conversation.py 的 chat_and_maybe_generate 中被调用:
    run_chat_pipeline(
        sess=sess,                    # 包含 params, user_images 等
        merchant_id="harbor_auto_spa",
        merchant_cfg={...},
        say=say,                      # Slack say() 函数
        client=client,                # Slack WebClient
    )

    # sess["params"] 示例:
    # {
    #     "topic": "Ultimate Guide to Tesla Model Y PPF",
    #     "primary_keyword": "Tesla PPF Lakeview",
    #     "secondary_keywords": ["XPEL PPF cost", "ceramic coating vs PPF"],
    #     "angle": "Cost-benefit for Tesla owners",
    #     "image_mode": "mixed",
    #     "image_assignments": {"img_1": 1, "img_3": 2},
    #     "generate_slots": ["img_2"],
    # }
"""

import logging
import shutil
import time
import uuid
from pathlib import Path

import config as cfg
from agents.copywriter import write_chat_blog, rewrite_blog
from agents.artist import enhance_image_prompts
from core import session
from core.session import REVIEWING
from core.i18n import t
from pipeline.preview_server import render_blog_html
from eval.deterministic import check_seo
from eval.repair import repair_all
from retrieval.catalog import build_catalog_brief, load_catalog
from pipeline.review_loop import run_review_loop
from pipeline.web_researcher import research_topic
from services.template_selector import pick_template_and_layout
from services.seedream_client import SeedreamClient
from services.usage_tracker import record_usage, format_usage_report, save_to_disk, set_current_session
from slack_ui.blocks import build_chat_progress_blocks
from store.blog_store import save_draft

log = logging.getLogger(__name__)


def run_chat_pipeline(
    sess: dict,
    merchant_id: str,
    merchant_cfg: dict,
    say,
    client,
):
    """对话式博客生成的完整 pipeline

    从 sess["params"] 中读取用户通过对话提供的参数，
    走 copywriter → reviewer → 图片处理 → 预览 → 保存草稿 的流程。

    Args:
        sess:         会话字典（来自 session.get_or_create），包含:
                      - params: 对话收集到的参数（topic, keywords, image_mode 等）
                      - user_images: 用户上传的图片路径列表
                      - thread_ts: Slack 线程 ID
        merchant_id:  商家标识
                      示例: "harbor_auto_spa"
        merchant_cfg: 商家配置字典
        say:          Slack say() 函数
        client:       Slack WebClient

    副作用:
        - 更新 sess["draft"] 为生成结果
        - 更新 sess["stage"] 为 REVIEWING
        - 在 Slack thread 中发送进度更新和最终结果

    输出到 Slack 的消息流:
        1. "[1/6] Selecting template & layout..."
        2. "[2/6] Researching competitor articles..."
        3. "[3/6] Writing blog content..."
        4. "[4/6] Reviewing content quality..."
        5. "[5/6] Processing images..."
        6. "[6/6] Assembling preview..."
        7. 最终结果卡片 + Publish 按钮
    """
    thread_ts = sess["thread_ts"]   # 纯 Slack ts —— 只用于往 thread 里发消息
    channel = sess["channel"]
    key = sess["key"]                # 会话主键 —— 所有 session.* 操作都用它
    params = sess["params"]
    user_images = sess["user_images"]
    creative_brief = sess.get("creative_brief", {})
    user_image_requests = sess.get("user_image_requests", {})
    store_name = merchant_cfg.get("store_name", merchant_id)
    lang = sess.get("language", "en")

    session_id = f"chat_{merchant_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    set_current_session(session_id)

    output_dir = Path(merchant_cfg.get("output_dir", str(cfg.OUTPUT_DIR / merchant_id)))
    output_dir.mkdir(parents=True, exist_ok=True)

    _t_start = time.time()

    # ── 进度消息（单条消息动态更新，和 auto 模式一样）──
    _progress_ts = None
    _fallback_text = t("generating_for", lang, name=store_name)

    def _progress(stage_key: str, extra: str = ""):
        """更新进度消息（首次发送，后续 chat_update 覆盖同一条）"""
        nonlocal _progress_ts
        blocks = build_chat_progress_blocks(store_name, stage_key, extra, lang=lang)
        try:
            if _progress_ts is None:
                resp = client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=_fallback_text,
                    blocks=blocks,
                )
                _progress_ts = resp["ts"]
            else:
                client.chat_update(
                    channel=channel, ts=_progress_ts,
                    text=_fallback_text,
                    blocks=blocks,
                )
        except Exception:
            pass

    try:
        # ── 检查是否是修改请求 ──────────────────────────────
        modify_scope = params.get("modify_scope", {})
        modify_feedback = params.get("modify_feedback", "")

        if modify_scope and not sess["draft"]:
            # 用户请求修改但没有草稿 — 清除 modify 参数，走正常生成
            log.warning("[%s][chat] 收到修改请求但无草稿，转为正常生成", merchant_id)
            say(text=t("no_draft_to_modify", lang),
                thread_ts=thread_ts)
            params.pop("modify_scope", None)
            params.pop("modify_feedback", None)
            modify_scope = {}

        is_modification = bool(modify_scope) and bool(modify_feedback) and bool(sess["draft"])

        if is_modification:
            # ⚠️ 必须把 client 传进去：_run_modification 结尾要用 client.chat_postMessage 发结果卡。
            #    旧代码没传，函数体里直接引用 client → NameError，被本函数的 except 吞掉，
            #    结果是「改稿文件其实已生成、草稿也存了盘，但用户只看到 ❌ 生成失败」。
            _run_modification(
                sess, params, merchant_id, merchant_cfg,
                session_id, output_dir, say, client, thread_ts, channel,
            )
            return

        # ── Step 1: 选择 HTML 模板 + 内容布局风格 ─────────────
        _progress("template")
        style_choice = pick_template_and_layout(merchant_id, merchant_cfg=merchant_cfg)
        log.info("[%s][chat] 风格: 模板=%s 布局=%s",
                 merchant_id, style_choice["template_name"], style_choice["layout_label"])

        # ── Step 2: Web Research（失败不阻断）─────────────────
        _progress("web")
        # 从用户参数构建 topic dict（格式和 researcher 输出一致）
        topic = _build_topic_from_params(params)
        research_brief = research_topic(merchant_id, topic)
        if research_brief:
            log.info("[%s][chat] 竞品调研完成: %d 字符", merchant_id, len(research_brief))

        # ── 确定图片数量 ──
        # 优先级: 用户在对话中指定 > 上传图片数量 > 默认 3 张
        # 没上传图片时至少 3 张（全 AI 生成）
        image_count = params.get("image_count", 0)
        if not image_count:
            image_count = len(user_images) if user_images else 3
        if not user_images:
            image_count = max(image_count, 3)
        else:
            image_count = max(image_count, 1)

        # ── 构建图片计划（在 copywriter 之前）──────────────────
        image_plan = _build_image_plan(params, user_images, image_count, user_image_requests)

        # ── Step 3: Chat Copywriter 撰写博客（带创意简报 + 图片计划）
        _progress("write")
        log.info("[%s][chat] Chat Copywriter 撰写: %s (image_count=%d, brief_sections=%d)",
                 merchant_id, topic.get("title", ""), image_count,
                 len(creative_brief.get("content_structure", [])))
        blog_data, _ = write_chat_blog(
            merchant_id,
            creative_brief=creative_brief,
            image_plan=image_plan,
            topic=topic,
            layout_prompt=style_choice["layout_prompt"],
            research_brief=research_brief or "",
            # chat 模式同样注入目录：用户在对话里聊到价格时，模型手上得有真实数字。
            # 出口那道 check_catalog_grounding 是共用的（review_loop 里），所以两条
            # pipeline 的"防"和"验"都对齐了，不会一条严一条松。
            catalog_brief=build_catalog_brief(topic, load_catalog(merchant_id)),
        )
        log.info("[%s][chat] Chat Copywriter 完成: '%s'", merchant_id, blog_data.get("title", ""))

        # ── Step 4: 质量闸门（确定性校验 → Reviewer 审核，最多 N 轮）──
        # 收口到 pipeline/review_loop.py（与 auto 流程同一份实现）；
        # 循环内部每轮都会自己 _progress("review"/"rewrite")，这里不用再手动打一次。
        #
        # chat 模式的槽位是 img_1..img_N（N = image_count），确定性校验器会拿这个集合
        # 去比对正文里的占位符 —— 所以 image_count 传错会直接被校验器抓出来，
        # 而不是等到 _execute_image_plan 时静默少一张图。
        review_score, review_rounds, seo_report = run_review_loop(
            merchant_id, blog_data, topic,
            merchant_cfg=merchant_cfg,   # 校验器要用它的 website 区分内链/外链
            mode="chat", image_count=image_count,
            progress_cb=_progress,
        )

        # ── Step 5: 按计划处理图片 ───────────────────────────
        _progress("image", f"{image_count} images")
        image_paths = _execute_image_plan(
            image_plan, blog_data,
            merchant_id, session_id, output_dir,
        )

        # ── Step 6: 组装 HTML + 保存草稿 ─────────────────────
        _progress("render")
        timestamp = int(time.time())
        filename = f"{merchant_id}_chat_{timestamp}_{uuid.uuid4().hex[:4]}.html"
        output_path = output_dir / filename

        preview_url = render_blog_html(
            blog_data=blog_data,
            image_paths=image_paths,
            merchant_cfg=merchant_cfg,
            output_path=output_path,
            template_file=style_choice["template_file"],
        )

        # 保存草稿
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

        # ── 构建结果 ─────────────────────────────────────────
        usage_report = format_usage_report(session_id)
        total_seconds = int(time.time() - _t_start)
        gen_time = f"{total_seconds // 60}m {total_seconds % 60}s" if total_seconds >= 60 else f"{total_seconds}s"

        result = {
            "success": True,
            "title": blog_data.get("title", "Untitled"),
            "preview_url": preview_url,
            "blog_data": blog_data,
            "image_paths": {slot: str(p) for slot, p in image_paths.items()},
            "review_score": review_score,
            "review_rounds": review_rounds,
            # 确定性校验报告 —— 同 auto 流程，只放 UI 要用的字段（要能 json 序列化，
            # 因为 result 会被整体写进 store/chat_sessions.json 做重启恢复）。
            "seo_checks": {
                "hard_ok": seo_report.hard_ok,
                "passed": seo_report.passed_checks,
                "hard_failures": seo_report.hard_failures,
                "soft_failures": seo_report.soft_failures,
                "repairs": seo_report.repairs,
            },
            "session_id": session_id,
            "usage_report": usage_report,
            "generation_time": gen_time,
            "wp_published": False,
            "template_name": style_choice["template_name"],
            "layout_label": style_choice["layout_label"],
        }

        # 进度标记完成
        _progress("done")

        # 存入会话草稿（用于修改场景）—— 直接改 dict，要手动落盘
        sess["draft"] = {"result": result, "session_id": session_id}
        session.persist()

        # 更新状态为 REVIEWING（update_stage 内部会再存一次盘）
        session.update_stage(key, REVIEWING)

        # ── 发送结果到 Slack ──────────────────────────────────
        from slack_ui.blocks import build_chat_result_blocks
        blocks = build_chat_result_blocks(result, lang=lang)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Blog generated: {blog_data.get('title', '')}",
            blocks=blocks,
        )

        log.info("[%s][chat] 生成完成: '%s' (耗时 %s)", merchant_id, blog_data.get("title", ""), gen_time)

    except Exception as exc:
        log.exception("[%s][chat] Pipeline 失败: %s", merchant_id, exc)
        session.update_stage(key, session.GATHERING if not sess["draft"] else REVIEWING)
        say(text=f":x: {t('gen_failed', lang, error=str(exc))}",
            thread_ts=thread_ts)

    finally:
        try:
            save_to_disk()
        except Exception:
            pass


def _build_topic_from_params(params: dict) -> dict:
    """从对话参数构建 topic 字典，格式与 researcher 输出一致

    让 copywriter 可以无缝接收，不需要区分来源是 auto 还是 chat。

    Args:
        params: 对话收集到的参数
                示例: {
                    "topic": "Ultimate Guide to Tesla PPF",
                    "primary_keyword": "Tesla PPF Lakeview",
                    "secondary_keywords": ["XPEL cost", "ceramic vs PPF"],
                    "angle": "Cost-benefit analysis",
                    "word_count": 1500,
                }

    Returns:
        topic 字典，格式与 researcher.analyze_and_pick_topics 输出一致
        示例: {
            "title": "Ultimate Guide to Tesla PPF",
            "primary_keyword": "Tesla PPF Lakeview",
            "secondary_keywords": ["XPEL cost", "ceramic vs PPF"],
            "angle": "Cost-benefit analysis",
            "estimated_word_count": 1500,
        }
    """
    return {
        "title": params.get("topic", "Blog Post"),
        "primary_keyword": params.get("primary_keyword", params.get("topic", "")),
        "secondary_keywords": params.get("secondary_keywords", []),
        "angle": params.get("angle", ""),
        "estimated_word_count": params.get("word_count", 1200),
    }


# 用户图片允许的处理方式 —— GPT 只能从这三个里选，其它值一律按 raw 兜底
_VALID_USER_ACTIONS = ("raw", "enhance", "reference")


def _coerce_image_index(img_num) -> int | None:
    """把 GPT 给的「第几张图」归一化成 0-based 索引。

    GPT 在 json_mode 下类型不稳定，同一个字段可能给 1 / "1" / " 1 " / "第1张"。

    Args:
        img_num: GPT 返回的原始值（int / str / 其它）

    Returns:
        0-based 索引；无法解析时返回 None

    示例:
        _coerce_image_index(2)      → 1
        _coerce_image_index("2")    → 1
        _coerce_image_index("img_2")→ None
        _coerce_image_index(0)      → None   （编号从 1 开始，0 视为非法）
    """
    if isinstance(img_num, bool):        # bool 是 int 的子类，得先挡掉
        return None
    if isinstance(img_num, int):
        return img_num - 1 if img_num >= 1 else None
    if isinstance(img_num, str) and img_num.strip().isdigit():
        n = int(img_num.strip())
        return n - 1 if n >= 1 else None
    return None


def _build_image_plan(
    params: dict,
    user_images: list[str],
    image_count: int,
    user_image_requests: dict[str, str] | None = None,
) -> dict[str, dict]:
    """从对话参数构建图片处理计划 — 在 copywriter 之前执行

    根据 conversation 提取的 image_mode/assignments/per_image_modes + 用户对每张图的描述，
    为每个槽位确定处理方式，让 copywriter 知道哪些需要写 prompt、哪些是用户原图、
    以及用户对每张图的具体要求。

    Args:
        params:               对话参数（含 image_mode, image_assignments, per_image_modes 等）
        user_images:          用户上传的图片路径列表
        image_count:          图片总数
        user_image_requests:  用户对每张图的自然语言描述
                              示例: {"img_2": "Sunset Tesla side shot", "img_4": "Price comparison chart"}

    Returns:
        有序字典，每个槽位的处理计划:
        {
            "img_1": {
                "source": "user", "action": "raw",
                "original_name": "car.jpg", "path": "C:/.../car.jpg"
            },
            "img_2": {
                "source": "user", "action": "reference",
                "original_name": "side.jpg", "path": "C:/.../side.jpg",
                "user_request": "Sunset Tesla side shot, warm golden tones"
            },
            "img_3": {
                "source": "ai", "action": "generate",
                "user_request": "Price comparison chart, clean minimalist style"
            },
        }

        user_request 字段说明:
        - raw 槽位: 没有 user_request（原图直接用）
        - reference/enhance/generate 槽位: 如果用户在对话中描述了要求，则包含 user_request
        - copywriter 会基于 user_request 润色生成最终的 image prompt
        - 如果没有 user_request，copywriter 根据文章上下文自行决定
    """
    image_mode = params.get("image_mode", "")
    image_assignments = params.get("image_assignments", {})
    per_image_modes = params.get("per_image_modes", [])
    requests = user_image_requests or {}

    if not image_mode:
        image_mode = "user" if user_images else "generate"

    all_slots = [f"img_{i}" for i in range(1, image_count + 1)]
    plan: dict[str, dict] = {}

    def _user_entry(idx: int) -> dict:
        """构建用户图片条目"""
        src = Path(user_images[idx])
        action = per_image_modes[idx] if idx < len(per_image_modes) else "raw"
        # ⚠️ per_image_modes 来自 GPT，可能给出 "original"/"keep"/"ai" 这类不在契约内的值。
        #    旧代码原样塞进 plan → _execute_image_plan 的 if/elif 全不匹配 →
        #    这个槽位既不复制原图也不生图，图片凭空消失（占位符被清掉，正文少一张图，无日志）。
        #    这里在「造 plan」时就收敛到合法值，非法值按最安全的 raw（直接用原图）处理。
        if action not in _VALID_USER_ACTIONS:
            log.warning("[image_plan] 图片 %d 的处理方式非法: %r，按 raw 处理", idx + 1, action)
            action = "raw"
        # 去掉下载时加的时间戳前缀 (YYYYMMDD_HHMMSS_) 还原原始文件名
        parts = src.name.split("_", 2)
        display_name = parts[2] if len(parts) >= 3 else src.name
        return {"source": "user", "action": action, "original_name": display_name, "path": str(src)}

    if image_mode == "generate" or (image_mode == "user" and not user_images):
        for slot in all_slots:
            plan[slot] = {"source": "ai", "action": "generate"}

    elif image_mode == "user":
        for i, slot in enumerate(all_slots):
            if i < len(user_images):
                plan[slot] = _user_entry(i)
            else:
                plan[slot] = {"source": "ai", "action": "generate"}

    elif image_mode == "mixed":
        for slot, img_num in image_assignments.items():
            # ⚠️ GPT 返回的 JSON 里图片编号可能是字符串 "1" 而不是数字 1
            #    （json_mode 不保证类型，实测 image_assignments 两种都出现过）。
            #    旧代码 `isinstance(img_num, int)` 遇到 "1" 直接判 False → 这个槽位
            #    悄悄退化成 AI 生成，用户明确指定的那张上传照片被丢掉，且无任何日志。
            #    这里统一归一化成 int，无法解析的才跳过并留下 warning。
            idx = _coerce_image_index(img_num)
            if slot in all_slots and idx is not None:
                if 0 <= idx < len(user_images):
                    plan[slot] = _user_entry(idx)
                else:
                    log.warning("[image_plan] %s 指定的图片编号 %s 超出已上传图片范围(共 %d 张)，改为 AI 生成",
                                slot, img_num, len(user_images))
            elif slot in all_slots:
                log.warning("[image_plan] %s 的图片编号无法解析: %r，改为 AI 生成", slot, img_num)
        for slot in all_slots:
            if slot not in plan:
                plan[slot] = {"source": "ai", "action": "generate"}

    else:
        for slot in all_slots:
            plan[slot] = {"source": "ai", "action": "generate"}

    # ── 注入用户对每张图的自然语言要求 ──
    # 只给非 raw 的槽位添加 user_request（raw 原图不需要 prompt）
    for slot, desc in requests.items():
        if slot in plan and plan[slot]["action"] != "raw" and desc:
            plan[slot]["user_request"] = desc

    log.info("[image_plan] %s", {
        s: f"{p['action']}({p.get('original_name', '')}) req={p.get('user_request', '')[:30]}"
        for s, p in plan.items()
    })
    return plan


def _execute_image_plan(
    image_plan: dict[str, dict],
    blog_data: dict,
    merchant_id: str,
    session_id: str,
    output_dir: Path,
) -> dict[str, Path]:
    """按图片计划执行处理 — 在 copywriter + reviewer 之后执行

    遍历 image_plan，根据每个槽位的 action 决定处理方式：
    - raw:       直接复制用户原图
    - enhance:   AI 修改风格（当前=复制，TODO 对接 API）
    - reference: 以用户图为参考，用 copywriter 的 prompt AI 生成新图
    - generate:  纯 AI 生成（用 copywriter 的 prompt）

    Args:
        image_plan:   _build_image_plan 的输出
        blog_data:    copywriter 输出（含 image_prompts）
        merchant_id:  商家标识
        session_id:   会话 ID
        output_dir:   输出目录

    Returns:
        {"img_1": Path(...), "img_2": Path(...), ...}
    """
    image_paths: dict[str, Path] = {}
    ai_generate_slots: list[str] = []

    img2img_slots: list[tuple[str, Path]] = []  # (slot, src_path) — enhance + reference

    for slot, info in image_plan.items():
        action = info["action"]

        if action == "raw":
            # 用户原图直接复制
            src = Path(info["path"])
            if src.exists():
                image_paths[slot] = _process_single_user_image(src, "raw", slot, merchant_id, output_dir)
            else:
                log.warning("[%s][chat] 用户图片不存在: %s，槽位 %s 改为 AI 生成", merchant_id, src, slot)
                ai_generate_slots.append(slot)

        elif action in ("enhance", "reference"):
            # 都走图生图：发原图 + prompt 给 Seedream
            # enhance: prompt 描述编辑效果（滤镜、调色、修饰）
            # reference: prompt 描述新图（以原图为风格/构图参考）
            src = Path(info["path"])
            if src.exists():
                img2img_slots.append((slot, src))
            else:
                log.warning("[%s][chat] 用户图片不存在: %s，槽位 %s 改为纯 AI 生成", merchant_id, src, slot)
                ai_generate_slots.append(slot)

        elif action == "generate":
            # 纯 AI 生成
            ai_generate_slots.append(slot)

        else:
            # 兜底分支：plan 里出现契约外的 action（_build_image_plan 已收敛过，
            # 但 plan 也可能来自旧草稿/未来其它入口）。绝不能静默丢槽位 —— 那会让
            # 正文少一张图且完全没有痕迹，所以退化为 AI 生成并留 warning。
            log.warning("[%s][chat] 槽位 %s 的 action 未知: %r，退化为 AI 生成",
                        merchant_id, slot, action)
            ai_generate_slots.append(slot)

    # 图生图：enhance + reference（逐张，因为每张参考图不同）
    if img2img_slots:
        generated = _generate_img2img(
            img2img_slots, image_plan, blog_data,
            merchant_id, session_id, output_dir,
        )
        image_paths.update(generated)

    # 纯文生图：批量 AI 生成
    if ai_generate_slots:
        generated = _generate_selected_images(blog_data, ai_generate_slots, merchant_id, session_id, output_dir)
        image_paths.update(generated)

    return image_paths


def _process_single_user_image(
    src: Path,
    mode: str,
    slot: str,
    merchant_id: str,
    output_dir: Path,
) -> Path:
    """复制用户上传的图片到输出目录（用于 raw 模式和 img2img fallback）"""
    # 统一命名: {merchant}_chat_{slot}_{timestamp}{后缀}
    suffix = src.suffix or ".jpg"
    dest_name = f"{merchant_id}_chat_{slot}_{int(time.time())}{suffix}"
    dest = output_dir / dest_name

    shutil.copy2(src, dest)
    log.info("[%s][chat] 图片 %s: 复制原图 (%s) %s", merchant_id, slot, mode, src.name)

    return dest


def _generate_img2img(
    slots: list[tuple[str, Path]],
    image_plan: dict[str, dict],
    blog_data: dict,
    merchant_id: str,
    session_id: str,
    output_dir: Path,
) -> dict[str, Path]:
    """用 Seedream 图生图处理 enhance / reference 槽位

    enhance: 发原图 + 编辑指令 prompt → Seedream 返回修改后的图
    reference: 发原图作参考 + 新场景 prompt → Seedream 返回风格相似的新图

    两者都走 generate_image(prompt=..., images=[src])，区别在 prompt 内容：
    - enhance prompt 由 copywriter 生成，描述滤镜/调色/修饰效果
    - reference prompt 由 copywriter 生成，描述全新场景（Seedream 自动参考原图风格）

    Args:
        slots:        [(slot_name, src_path), ...] 需要图生图的槽位
        image_plan:   完整图片计划（用于读 action 类型）
        blog_data:    copywriter 输出（含 image_prompts）
        merchant_id:  商家标识
        session_id:   会话 ID
        output_dir:   输出目录

    Returns:
        {"img_2": Path(...), ...} 成功生成的图片
    """
    image_paths: dict[str, Path] = {}
    raw_prompts = blog_data.get("image_prompts", {})
    seo_slug = blog_data.get("seo_slug", "blog")

    # Artist 美化 prompt
    prompts_to_enhance = {slot: raw_prompts.get(slot, "") for slot, _ in slots if raw_prompts.get(slot)}
    if prompts_to_enhance:
        try:
            enhanced_prompts, _ = enhance_image_prompts(
                merchant_id, prompts_to_enhance,
                blog_data.get("title", ""), blog_data.get("excerpt", ""),
            )
        except Exception as e:
            log.warning("[%s][chat] Artist 美化失败，使用原始 prompt: %s", merchant_id, e)
            enhanced_prompts = prompts_to_enhance
    else:
        enhanced_prompts = {}

    try:
        seedream = SeedreamClient()
        for slot, src_path in slots:
            action = image_plan[slot]["action"]
            prompt = enhanced_prompts.get(slot, raw_prompts.get(slot, ""))

            if not prompt:
                # 没有 prompt 时用 fallback
                keyword = blog_data.get("title", "professional business")
                if action == "enhance":
                    prompt = f"Enhance this photo: improve lighting, color grading, and overall quality. Related to {keyword}"
                else:
                    prompt = f"Create a new professional image inspired by this reference photo. Related to {keyword}"

            log.info("[%s][chat] Seedream 图生图 %s (%s): src=%s prompt=%s",
                     merchant_id, slot, action, src_path.name, prompt[:80])

            img_filename = f"{merchant_id}_chat_{seo_slug}_{int(time.time())}_{slot}.png"
            img_path = output_dir / img_filename

            urls = seedream.generate_image(prompt=prompt, images=[str(src_path)], size="2K")
            if urls:
                seedream.download_image(urls[0], img_path)
                image_paths[slot] = img_path
                record_usage(
                    merchant_id, "seedream", cfg.SEEDREAM_MODEL,
                    image_count=1, session_id=session_id,
                )

    except Exception as img_err:
        log.error("[%s][chat] Seedream 图生图失败: %s", merchant_id, img_err)
        # fallback: 失败的槽位复制原图
        for slot, src_path in slots:
            if slot not in image_paths and src_path.exists():
                dest = _process_single_user_image(src_path, "raw", slot, merchant_id, output_dir)
                image_paths[slot] = dest
                log.warning("[%s][chat] %s 图生图失败，fallback 使用原图", merchant_id, slot)

    return image_paths


def _generate_selected_images(
    blog_data: dict,
    slots: list[str],
    merchant_id: str,
    session_id: str,
    output_dir: Path,
) -> dict[str, Path]:
    """为指定槽位 AI 生成图片

    先通过 artist 美化 prompt，再调用 Seedream 生成。

    Args:
        blog_data:   copywriter 输出（含 image_prompts）
        slots:       需要生成的槽位列表
                     示例: ["hero", "end"]（只生成 hero 和 end，跳过 mid）
        merchant_id: 商家标识
        session_id:  会话 ID
        output_dir:  输出目录

    Returns:
        成功生成的图片路径字典
        示例: {"hero": Path(".../hero.png"), "end": Path(".../end.png")}
    """
    image_paths: dict[str, Path] = {}
    raw_prompts = blog_data.get("image_prompts", {})

    # 只取需要生成的槽位的 prompt
    prompts_to_enhance = {s: raw_prompts.get(s, "") for s in slots if raw_prompts.get(s)}

    if not prompts_to_enhance:
        # 没有 prompt，用默认
        keyword = blog_data.get("title", "professional business")
        prompts_to_enhance = {
            s: f"Professional high-quality image related to {keyword}"
            for s in slots
        }

    # Artist 美化 prompt
    try:
        enhanced_prompts, _ = enhance_image_prompts(
            merchant_id, prompts_to_enhance,
            blog_data.get("title", ""), blog_data.get("excerpt", ""),
        )
    except Exception as e:
        log.warning("[%s][chat] Artist 美化失败，使用原始 prompt: %s", merchant_id, e)
        enhanced_prompts = prompts_to_enhance

    # Seedream 生成
    seo_slug = blog_data.get("seo_slug", "blog")
    try:
        seedream = SeedreamClient()
        for slot, prompt in enhanced_prompts.items():
            if not prompt:
                continue
            log.info("[%s][chat] Seedream 生成 %s 图片...", merchant_id, slot)
            img_filename = f"{merchant_id}_chat_{seo_slug}_{int(time.time())}_{slot}.png"
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
        log.error("[%s][chat] Seedream 生图失败: %s", merchant_id, img_err)

    return image_paths


def _run_modification(
    sess: dict,
    params: dict,
    merchant_id: str,
    merchant_cfg: dict,
    session_id: str,
    output_dir: Path,
    say,
    client,
    thread_ts: str,
    channel: str,
):
    """处理用户的修改请求 — 根据 modify_scope 局部重新生成

    修改范围由 params["modify_scope"] 控制:
    - title=True:   只重写标题
    - content=True: 重写正文
    - images="all": 重新生成所有图片
    - images=[...]: 只重新生成/替换指定图片

    Args:
        sess:         会话字典（含 draft 和 user_images）
        params:       对话参数（含 modify_scope 和 modify_feedback）
        merchant_id:  商家标识
        merchant_cfg: 商家配置
        session_id:   新的 session ID
        output_dir:   输出目录
        say:          Slack say 函数（发纯文本提示用）
        client:       Slack WebClient（发结果卡 blocks 用）
                      ★ 旧版本漏了这个参数，函数末尾却引用 client → NameError
        thread_ts:    Slack thread timestamp
        channel:      Slack channel ID

    示例:
        # 用户说 "title is too long, make it concise"
        # params = {
        #     "modify_scope": {"title": True, "content": False, "images": "keep"},
        #     "modify_feedback": "title is too long, make it concise"
        # }
        # → 只调用 copywriter rewrite，保留其他部分
    """
    modify_scope = params["modify_scope"]
    modify_feedback = params["modify_feedback"]
    prev_result = sess["draft"].get("result", {})
    prev_blog_data = prev_result.get("blog_data", {})

    lang = sess.get("language", "en")
    say(text=t("working_on_mods", lang), thread_ts=thread_ts)

    blog_data = dict(prev_blog_data)  # 浅复制，避免修改原数据

    # ── 文案修改（标题/内容）──
    need_text_change = modify_scope.get("title", False) or modify_scope.get("content", False)
    if need_text_change:
        feedback = {
            "score": prev_result.get("review_score", 70),
            "issues": [modify_feedback],
            "suggestions": [modify_feedback],
        }
        # 从 blog_data 的 image_prompts 推断图片数量
        _img_count = len(blog_data.get("image_prompts", {})) or 3
        revised, _ = rewrite_blog(merchant_id, blog_data, feedback, round_num=1, mode="chat", image_count=_img_count)
        # 根据 scope 选择性更新
        if modify_scope.get("title", False):
            blog_data["title"] = revised.get("title", blog_data["title"])
        if modify_scope.get("content", False):
            blog_data["content_html"] = revised.get("content_html", blog_data["content_html"])
            blog_data["excerpt"] = revised.get("excerpt", blog_data["excerpt"])
            blog_data["tags"] = revised.get("tags", blog_data["tags"])

    # ── 图片修改 ──
    images_action = modify_scope.get("images", "keep")
    prev_image_paths = {}
    # 从 result 中直接获取（run_chat_pipeline 已存入）
    for slot, p in prev_result.get("image_paths", {}).items():
        prev_image_paths[slot] = Path(p)
    # fallback: 从 draft store 获取
    if not prev_image_paths and sess["draft"].get("session_id"):
        from store.blog_store import get_drafts
        drafts = get_drafts(merchant_id, limit=10)
        for d in drafts:
            if d.get("session_id") == sess["draft"]["session_id"]:
                for slot, p in d.get("image_paths", {}).items():
                    prev_image_paths[slot] = Path(p)
                break

    if images_action == "keep":
        image_paths = prev_image_paths
    elif images_action == "all":
        # ⚠️ 槽位名必须跟 blog_data 里的占位符/prompt 键一致，否则生成出来的图
        #    在 render 时匹配不上 <!-- BLOG_IMAGE:xxx --> 占位符，等于白花钱且正文无图。
        #    旧代码的 fallback 硬编码 img_1..img_3，但 auto 流程产出的稿子用的是
        #    hero/mid/end；chat 里用户也可能要 5 张。改为按优先级取真实槽位名：
        #      旧图路径的键 → blog_data.image_prompts 的键 → blog_data.image_alts 的键 → 最后才兜底
        all_slots = (
            list(prev_image_paths.keys())
            or list(blog_data.get("image_prompts", {}).keys())
            or list(blog_data.get("image_alts", {}).keys())
            or [f"img_{i}" for i in range(1, 4)]
        )
        log.info("[%s][chat] 全量重生图片，槽位: %s", merchant_id, all_slots)
        image_paths = _generate_selected_images(blog_data, all_slots, merchant_id, session_id, output_dir)
    elif isinstance(images_action, list):
        image_paths = dict(prev_image_paths)
        for change in images_action:
            slot = change.get("slot", "")
            action = change.get("action", "")
            if action == "regenerate" and slot:
                new_imgs = _generate_selected_images(
                    blog_data, [slot], merchant_id, session_id, output_dir,
                )
                image_paths.update(new_imgs)
            elif action == "replace" and slot:
                # 同 _build_image_plan：GPT 可能把 image_num 给成字符串 "2"，
                # 旧代码直接 `img_num - 1` 会抛 TypeError（str - int），
                # 而这个异常在 run_chat_pipeline 的 except 里只会变成一句「生成失败」。
                idx = _coerce_image_index(change.get("image_num", 0))
                if idx is not None and 0 <= idx < len(sess["user_images"]):
                    src = Path(sess["user_images"][idx])
                    if src.exists():
                        dest = _process_single_user_image(src, "raw", slot, merchant_id, output_dir)
                        image_paths[slot] = dest
                else:
                    log.warning("[%s][chat] replace 指定的图片编号无效: %r，跳过槽位 %s",
                                merchant_id, change.get("image_num"), slot)
    else:
        image_paths = prev_image_paths

    # ── 重新渲染预览 ──
    style_choice = pick_template_and_layout(merchant_id, merchant_cfg=merchant_cfg)
    timestamp = int(time.time())
    filename = f"{merchant_id}_chat_mod_{timestamp}_{uuid.uuid4().hex[:4]}.html"
    output_path = output_dir / filename

    preview_url = render_blog_html(
        blog_data=blog_data,
        image_paths=image_paths,
        merchant_cfg=merchant_cfg,
        output_path=output_path,
        template_file=style_choice["template_file"],
    )

    save_draft(
        merchant_id=merchant_id,
        title=blog_data.get("title", "Untitled"),
        filename=filename,
        preview_url=preview_url,
        blog_data=blog_data,
        review_score=prev_result.get("review_score", 0),
        session_id=session_id,
        image_paths={slot: str(p) for slot, p in image_paths.items()},
    )

    # ── 改稿后重跑确定性校验（零成本，所以没有不跑的理由）──────────────
    # 为什么必须重跑：稿子已经变了。如果直接把上一版的 seo_checks 抄进新 result，
    # 卡片上那列绿勾描述的是一份**已经不存在的稿子** —— 那比不显示更糟。
    # 这里不重跑 reviewer（那要花钱，且改稿是用户主动要求的，不该再评一次分），
    # 但确定性检查是免费的：改完摘要超了 160 字、改标题时混进 emoji，这里立刻抓到。
    #
    # 两个参照物的来源和主流程不同，都说明一下：
    #   topic          改稿路径没有选题字典（用户是在 thread 里直接说"标题太长"），
    #                  所以传空 dict —— 关键词那两项会明确报"跳过"，不会假装通过。
    #   expected_slots 用 image_paths 的键。这些是**磁盘上真实存在的图片文件**的槽位名，
    #                  来自 _execute_image_plan 的产物，不是 LLM 自己说的 —— 符合
    #                  "校验参照物必须独立于被校验对象"这条原则。
    # 修复也要跑：用户手改标题/摘要之后同样可能超长，这里一样是"代码能修就别问模型"。
    _mod_repairs = [r.note for r in repair_all(blog_data)]
    for _note in _mod_repairs:
        log.info("[%s][chat] 改稿后确定性修复: %s", merchant_id, _note)

    _mod_report = check_seo(
        blog_data, {},
        website=merchant_cfg.get("website") or merchant_cfg.get("wordpress_url") or "",
        expected_slots=tuple(image_paths.keys()) or None,
        # 改稿路径同样要查品牌禁词：用户手改标题时最容易把禁词带进门面
        brand_terms=(merchant_cfg.get("brand_voice") or {}).get("forbidden_terms") or [],
        # 改稿路径也要核价格：用户手改正文时同样可能把一个编造的价格塞进来
        catalog=load_catalog(merchant_id),
    )
    _mod_report.repairs = _mod_repairs
    log.info("[%s][chat] 改稿后确定性校验 — %s", merchant_id, _mod_report.summary_line())
    # 只用 ASCII 前缀：非 ASCII 会在 Windows GBK 控制台上让 logging 抛异常丢日志
    for _f in _mod_report.hard_failures:
        log.warning("[%s][chat]   HARD FAIL: %s", merchant_id, _f)
    for _f in _mod_report.soft_failures:
        log.info("[%s][chat]   soft: %s", merchant_id, _f)

    result = {
        "success": True,
        "title": blog_data.get("title", "Untitled"),
        "preview_url": preview_url,
        "blog_data": blog_data,
        "image_paths": {slot: str(p) for slot, p in image_paths.items()},
        "review_score": prev_result.get("review_score", 0),
        "review_rounds": 0,
        "seo_checks": {
            "hard_ok": _mod_report.hard_ok,
            "passed": _mod_report.passed_checks,
            "hard_failures": _mod_report.hard_failures,
            "soft_failures": _mod_report.soft_failures,
            "repairs": _mod_report.repairs,
        },
        "session_id": session_id,
        "usage_report": format_usage_report(session_id),
        "generation_time": "modified",
        "wp_published": False,
        "template_name": style_choice["template_name"],
        "layout_label": style_choice["layout_label"],
    }

    sess["draft"] = {"result": result, "session_id": session_id}
    # 清除 modify 参数，防止下次对话误判为修改
    params.pop("modify_scope", None)
    params.pop("modify_feedback", None)
    session.persist()                                   # draft/params 是直接改的，手动落盘
    session.update_stage(sess["key"], REVIEWING)

    from slack_ui.blocks import build_chat_result_blocks
    blocks = build_chat_result_blocks(result, lang=lang)
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f"Modified: {blog_data.get('title', '')}",
        blocks=blocks,
    )
