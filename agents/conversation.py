"""博客对话层 — GPT 理解用户意图，提取博客生成参数，判断是否可以开始生成

核心流程：
1. 把会话历史 + 用户最新消息 + 已上传图片列表 发给 GPT
2. GPT 返回结构化 JSON:
   - ready: bool     — 信息是否充足可以开始生成
   - reply: str      — 回复给用户的话（英文，因为博客面向英文市场）
   - params: dict    — 提取到的博客生成参数
3. 如果 ready=True → 进入 pipeline 生成博客
   如果 ready=False → 把 reply 发给用户，继续对话

与 auto 流程的区别：
- auto: trend_scraper 爬热词 → researcher 选题 → 全 AI 生图（固定 hero/mid/end 三槽位）
- chat: 用户对话提供主题 → 可上传图片做配图 → 编号槽位 img_1..img_N → 灵活混合

图片处理模式说明（编号槽位 img_1, img_2, ..., img_N，N 由用户决定）：
- "generate"  — 全部由 Seedream AI 生成（默认，和 auto 一样）
- "user"      — 全部使用用户上传的图片（按顺序分配到 img_1, img_2, ...）
- "mixed"     — 混合模式：用户指定哪些槽位用上传图片，哪些用 AI 生成
                 通过 image_assignments 指定: {"img_1": 1, "img_3": 2}（图片编号→槽位）
                 未指定的槽位自动 AI 生成

使用示例:
    # 在 router 中被调用（main.py 把消息路由到这里）:
    chat_and_maybe_generate(sess, "帮我写一篇关于 Tesla PPF 贴膜的文章", say, client)

    # GPT 可能返回（信息不足，继续追问）:
    # {"ready": false, "reply": "Got it! What angle would you like...", "params": {"topic": "Tesla PPF"}}

    # GPT 可能返回（信息充足，开始生成）:
    # {"ready": true, "reply": "Great! Starting generation...", "params": {"topic": "...", "image_mode": "generate"}}
"""

import json
import logging
import re
from pathlib import Path

from agents.soul_loader import build_system_prompt, get_soul
from core import session
from core.session import CONFIRMING, GENERATING
from core.i18n import resolve_language
from llm import route

log = logging.getLogger(__name__)


def _build_extraction_instruction(store_name: str, merchant_id: str) -> str:
    """构建对话层的指令 — 创意顾问 + 简报生成器

    GPT 的三重角色：
    1. 创意顾问：帮用户想角度、解答疑惑、提出建议、润色表达
    2. 参数提取：从对话中逐轮提取 SEO 参数 + 图片分配
    3. 简报生成：ready=true 时，综合多轮对话生成 creative_brief（导演脚本）

    核心原则：用户说的每一句关于内容和图片的要求都要被保留，
    不能只提取结构化参数而丢弃用户的创意意图。

    Args:
        store_name:  商家名称，如 "Harbor Auto Spa" / "Summit HVAC"
        merchant_id: 商家 ID，如 "harbor_auto_spa" / "summit_hvac"

    Returns:
        完整的指令字符串（追加到 system prompt 后面）

    输出示例（ready=false，追问阶段）:
        {{
          "ready": false,
          "reply": "收到！Tesla 贴膜文章。想从哪个角度写？...",
          "params": {{"topic": "Tesla PPF"}},
          "creative_brief": {{}},
          "user_image_requests": {{}}
        }}

    输出示例（ready=true，开始生成）:
        {{
          "ready": true,
          "reply": "明白了，确认一下：...",
          "params": {{"topic": "...", "primary_keyword": "...", "angle": "...", ...}},
          "creative_brief": {{
            "content_structure": [
              {{"section": "Opening", "requirement": "用用户提供的数据引入，配 img_1"}},
              {{"section": "Price Comparison Table", "requirement": "XPEL/3M/Suntek 三品牌价格对比"}}
            ],
            "user_provided_text": {{"price_data": "Model Y 全车 $6500，前脸 $2500"}},
            "tone": "professional but approachable, industry expert voice",
            "special_requests": "强调 XPEL 认证，结尾 CTA 提供免费评估"
          }},
          "user_image_requests": {{
            "img_2": "Sunset shot of Tesla side profile during PPF installation, warm tones",
            "img_4": "Clean price comparison infographic with data labels"
          }}
        }}
    """
    return f"""
## Communication Rules
- CRITICAL: Your "reply" MUST be in the SAME language the user is using. If the user writes in Chinese, reply in Chinese. If in English, reply in English. Match their language exactly.
- CRITICAL: All values in "params", "creative_brief", and "user_image_requests" MUST be in English (these are internal data for the pipeline)
- The blog article itself is always generated in English for an English-speaking audience
- If the user speaks Chinese, translate their intent into English for params/brief/requests, but keep your "reply" in Chinese

## Reply Formatting (Slack mrkdwn — NOT standard Markdown)
- CRITICAL: Slack bold is *single asterisk* like *this*, NOT **double asterisk**
- CRITICAL: Slack italic is _underscore_ like _this_, NOT *single asterisk*
- Use emoji at the start of each section/paragraph as visual dividers
- Use *bold* (single asterisk) for key terms and section headers
- Use bullet points for lists (suggestions, options, confirmations)
- Use `backticks` for file names, keywords, and technical terms
- Keep each paragraph short (2-3 sentences max), add line breaks between sections
- When confirming before generation (ready=true), format as a structured summary:
  :memo: *Topic:* ...
  :dart: *Keyword:* ...
  :pencil: *Angle:* ...
  :frame_with_picture: *Images:* (list each image with its mode)
  :bulb: *Style:* ...
- When asking questions, present options as a numbered or bulleted list with emoji

## JSON Response Format

You must reply in JSON format:
{{
  "ready": true/false,

  "reply": "your reply to the user",

  "params": {{
    // ── 核心 SEO 参数（逐轮提取，非空值才覆盖，"__clear__" 可删除字段）──

    "topic": "",              // 博客主题
                              //   e.g. "Tesla Model Y PPF Paint Protection Film Guide"
    "primary_keyword": "",    // 主 SEO 关键词
                              //   e.g. "Tesla PPF Lakeview"
    "secondary_keywords": [], // 辅助关键词 (3-5 个)
                              //   e.g. ["paint protection film cost", "XPEL PPF Tesla"]
    "angle": "",              // 文章切入角度
                              //   e.g. "Cost-benefit analysis for Tesla owners in Southern California"
    "word_count": 1200,       // 目标字数 (默认 1200, 范围 800-2000)
    "style": "",              // 写作风格
                              //   e.g. "authoritative expert guide" / "friendly how-to"

    // ── 图片参数 ──
    // 槽位用编号: img_1, img_2, ..., img_N
    "image_count": 3,         // 图片总数 (默认 3, 范围 1-10)
    "image_mode": "",         // "generate" = 全 AI | "user" = 全用户图 | "mixed" = 混合
    "image_assignments": {{}}, // mixed 模式: 哪个槽位用哪张用户图
                              //   e.g. {{"img_1": 1, "img_3": 2}}
    "generate_slots": [],     // mixed 模式: 哪些槽位 AI 生成
                              //   e.g. ["img_2", "img_4"]
    "per_image_modes": [],    // 每张用户图的处理方式: "raw" / "enhance" / "reference"
                              //   长度 = 用户上传图片数量，默认全 "raw"

    // ── 修改参数（仅当已有草稿且用户请求修改时）──
    "modify_scope": {{}},      // 改什么: {{"title": true, "content": true, "images": "keep"/"all"/[...]}}
    "modify_feedback": ""     // 用户的修改要求原文
  }},

  "creative_brief": {{
    // ★ 综合多轮对话生成的创意简报 — ready=true 时必须填写完整
    // ★ 这是给 copywriter 的"导演脚本"，用户说的每一个内容要求都必须在这里体现

    "content_structure": [
      // 用户要求的文章结构，按顺序排列
      // 每个 section 可以指定: 要求(requirement)、配套图片(image)、用户提供的文本(user_text)
      // 示例:
      // {{"section": "Opening Hook", "requirement": "Start with a shocking stat about paint damage"}},
      // {{"section": "Price Comparison Table", "requirement": "Compare XPEL/3M/Suntek prices for Model Y", "user_text": "Full body $6500, front only $2500"}},
      // {{"section": "Installation Process", "requirement": "Step-by-step with photos", "image": "img_2"}},
      // {{"section": "Results Gallery", "requirement": "Before/after showcase", "image": "img_3"}},
      // {{"section": "FAQ", "requirement": "Must include warranty question"}},
      // {{"section": "CTA", "requirement": "Emphasize free consultation offer"}}
    ],

    "user_provided_text": {{
      // 用户在对话中提供的原始文本/数据/引用，copywriter 必须融入文章
      // key 可以自由命名，value 是用户的原文(翻译为英文)
      // 示例:
      // "price_data": "Model Y full body $6500, front bumper $2500, 15% cheaper than competitors",
      // "testimonial": "We are the only shop in Southern California with both XPEL and Ceramic Pro certifications",
      // "opening_line": "Your Tesla deserves better than rock chips"
    }},

    "tone": "",
    // 用户要求的语气/风格
    // 示例: "professional but approachable, industry expert voice"
    // 如果用户没指定，根据商家定位推荐合适的语气

    "target_audience": "",
    // 目标读者
    // 示例: "Tesla owners in Lakeview, 30-50 age, high income"

    "special_requests": ""
    // 用户的其他特殊要求（不属于上面任何分类的）
    // 示例: "mention XPEL warranty is transferable, add internal link to /services/ppf/"
  }},

  "user_image_requests": {{
    // 用户对每张需要 AI 处理的图片的自然语言描述
    // key = 槽位名 (img_1, img_2, ...)
    // value = 用户的要求（翻译为英文）
    // 只填非 "raw" 的槽位（raw = 用户原图直接用，不需要描述）
    //
    // 示例:
    // "img_2": "Sunset shot of Tesla Model Y side profile during PPF installation, warm golden tones, technician visible",
    // "img_3": "Enhance the photo to look more professional and magazine-quality, cinematic color grading",
    // "img_4": "Clean minimalist price comparison infographic, white background, blue accent colors"
    //
    // 如果用户没有对某张 AI 图片提出具体要求，不要填（留给 copywriter 根据文章上下文决定）
  }}
}}

## Rules for ready=true

### 必要条件（缺任何一个都要追问）:
1. **topic** — 明确的博客主题（不能太模糊如"PPF"，需要具体方向）
2. **primary_keyword** — 主 SEO 关键词
3. **angle** — 具体的切入角度

### 例外情况（可以不满足全部 3 个）:
- 用户明确说"开始"/"go ahead"/"生成" → 根据商家知识补全默认值
- 用户请求修改已有草稿 (modify_scope) → 直接 ready=true

### 追问策略:
- 缺主题/角度 → 基于商家业务建议 2-3 个具体方向（不是固定的，根据对话上下文动态生成）
- 缺关键词 → 根据主题推荐最佳 SEO 关键词组合
- 图片分配不清 → 简洁确认: "你的 3 张图怎么用？全部直接放还是需要 AI 处理？"
- 如果用户意图很清楚，不要过度追问 — 快速进入生成

### creative_brief 填写规则:
- ready=false 时: creative_brief 可以为空 {{}} 或只填已确定的部分
- ready=true 时: 必须综合整个对话历史，生成完整的 creative_brief
- 用户在对话中提到的每一个内容要求（小标题、段落要求、数据、引用）都必须出现在 content_structure 或 user_provided_text 中
- 如果用户没有指定详细结构，根据商家业务 + 主题 + 角度推荐合理的结构

## Image Rules

### 图片模式检测:
- 上传 N 张图 + 没说怎么用 → image_mode="user", image_count=N, 按顺序分配
- 上传图 + 说"作为参考" → per_image_modes=["reference", ...]
- 上传图 + 说"美化"/"enhance" → per_image_modes=["enhance", ...]
- 上传 2 张 + 说"再AI生成3张" → image_mode="mixed", image_count=5
- 没上传图 + 没指定数量 → image_mode="generate", image_count=3

### user_image_requests 填写规则:
- 只填非 raw 的图片（raw 原图不需要描述）
- 如果用户对某张图有具体描述（"第2张要夕阳下的Tesla"），提取并翻译为英文
- 如果用户说"第3张美化一下"但没具体说怎么美化，可以不填（让 copywriter 决定）
- 对于纯 AI generate 的槽位，如果用户有要求也要记录

### 图片确认:
- 新图片出现时确认编号: "收到 3 张图 (Image 1, 2, 3)。"
- 后续上传时: "收到 Image 4，现在共 4 张。"

## Modification Rules (已有草稿时)
- 只改标题: modify_scope={{"title": true, "content": false, "images": "keep"}}
- 只改内容: modify_scope={{"title": false, "content": true, "images": "keep"}}
- 改特定图片: modify_scope={{..., "images": [{{"slot": "img_2", "action": "regenerate"}}]}}
- 用上传图替换: modify_scope={{..., "images": [{{"slot": "img_3", "action": "replace", "image_num": 2}}]}}
- 全部重来: modify_scope 留空
- 修改时直接 ready=true

"""


def chat_and_maybe_generate(sess: dict, user_text: str, say, client, merchant_id: str, merchant_cfg: dict):
    """对话层主函数 — 理解用户意图，提取参数，可能触发生成 pipeline

    完整流程：
    1. 加载商家人格（_shared + assistant）构建 system prompt
    2. 注入上下文（已上传图片列表 + 当前草稿）
    3. 添加完整对话历史
    4. 调用 GPT → 解析 JSON 回复
    5. 合并参数到会话
    6. ready=True → 通知用户 + 启动 pipeline
       ready=False → 回复追问

    Args:
        sess:         会话字典（来自 session.get_or_create）
        user_text:    用户本次发送的文字内容
                      示例: "帮我写一篇关于 Tesla PPF 贴膜的文章"
        say:          Slack say() 函数，用于发送消息
        client:       Slack WebClient，用于 API 调用
        merchant_id:  商家标识
                      示例: "harbor_auto_spa"
        merchant_cfg: 商家配置字典（来自 merchant.json）
                      示例: {"merchant_id": "harbor_auto_spa", "store_name": "Harbor Auto Spa", ...}

    副作用：
        - 更新 sess["params"]（合并 GPT 提取的参数）
        - 更新 sess["messages"]（记录 assistant 回复）
        - 更新 sess["usage"]（累加 token 用量）
        - ready=True 时：更新 stage 为 GENERATING，启动 chat_generator.run_chat_pipeline
    """
    thread_ts = sess["thread_ts"]   # 纯 Slack ts —— 只用于 say(thread_ts=...) 发消息
    key = sess["key"]                # 会话主键 —— 所有 session.* 操作都用它
    store_name = merchant_cfg.get("store_name", merchant_id)

    # ── 语言检测（每条消息都重算，双向 + 迟滞；见 core/i18n.resolve_language）──
    # 旧逻辑只允许 en→zh 一次性升级，英文用户被一句中文带偏后就永久锁死中文 UI。
    if user_text:
        sess["language"] = resolve_language(sess.get("language", "en"), user_text)
    lang = sess.get("language", "en")

    # ── 1. 构建 system prompt ──
    # 尝试加载 assistant 人格，如果没有则只用 _shared
    assistant_soul = get_soul(merchant_id, "assistant")
    if assistant_soul:
        system_prompt = build_system_prompt(merchant_id, "assistant")
    else:
        # 没有 assistant.md 时，只用 _shared 背景知识
        from agents.soul_loader import get_shared
        system_prompt = get_shared(merchant_id)

    system_prompt += "\n\n" + _build_extraction_instruction(store_name, merchant_id)

    # ── 2. 构建消息列表 ──
    messages = [{"role": "system", "content": system_prompt}]

    # 注入用户已上传的图片编号清单（如果有）
    # GPT 看不到图片内容，但用户在 Slack 能看到缩略图
    # Bot 回复时用 "Image 1 (car_front.jpg)" 格式引用，用户一眼对照文件名
    if sess["user_images"]:
        img_lines = [f"[User has uploaded {len(sess['user_images'])} image(s):]"]
        for i, img_path in enumerate(sess["user_images"], 1):
            fname = Path(img_path).name
            # 去掉时间戳前缀 (YYYYMMDD_HHMMSS_) 还原原始文件名
            parts = fname.split("_", 2)
            original_name = parts[2] if len(parts) >= 3 else fname
            img_lines.append(f"  Image {i}: {original_name}")
        img_lines.append("[When referring to images, always use 'Image N (filename)' format, e.g. 'Image 1 (car_front.jpg)']")
        messages.append({"role": "system", "content": "\n".join(img_lines)})

    # 注入当前草稿（如果有，用于修改场景）
    if sess["draft"]:
        draft_note = _format_draft_context(sess["draft"])
        messages.append({"role": "system", "content": draft_note})

    # 添加完整对话历史
    for msg in sess["messages"]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # ── 3. 调用 GPT（走统一路由层：task="chat" → gpt-4.1-mini）──
    # chat 是多轮对话，用 complete_messages 传完整 messages 数组（保留多条 system + 对话历史）。
    # 全局记账由 complete_messages 内部自动完成（agent="conversation"）；此外再单独累加到
    # 本 thread 的 session usage（session.add_usage 是「每次会话」维度，跟全局台账并行）。
    try:
        llm_result = route("chat", merchant_cfg).complete_messages(
            messages=messages,
            temperature=0.4,
            max_tokens=3000,
            json_mode=True,
            label="conversation",
        )
    except Exception as e:
        log.error("对话层 GPT 调用失败: %s", e)
        fallback_reply = "Sorry, I encountered an error. Please try again."
        say(text=fallback_reply, thread_ts=thread_ts)
        session.add_message(key, "assistant", fallback_reply)
        return

    # 累加到本 thread 的会话用量（成本已由 complete_messages 按价格表算好）
    session.add_usage(key, llm_result.prompt_tokens, llm_result.completion_tokens, llm_result.cost)

    # ── 4. 解析 GPT 回复 ──
    # ⚠️ 旧代码把解析结果又赋回 `result`，覆盖了 LLMResult 对象。当时能跑是因为
    #    token/cost 恰好在上一行取完了；一旦以后有人想在下面读 result.model / result.cost
    #    就会拿到 dict 而报 AttributeError。这里拆成 llm_result / parsed 两个名字。
    raw = llm_result.text
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.error("对话层 JSON 解析失败: %s", raw[:200])
        fallback_reply = "Sorry, I had trouble understanding. Could you rephrase?"
        say(text=fallback_reply, thread_ts=thread_ts)
        # 记录 assistant 回复，防止上下文断裂（下轮对话 GPT 能看到这条）
        session.add_message(key, "assistant", fallback_reply)
        return

    # GPT 偶尔会返回顶层是 list 或字符串的 JSON（json_mode 只保证是合法 JSON，不保证是对象）
    if not isinstance(parsed, dict):
        log.error("对话层返回的 JSON 顶层不是对象: %s", raw[:200])
        fallback_reply = "Sorry, I had trouble understanding. Could you rephrase?"
        say(text=fallback_reply, thread_ts=thread_ts)
        session.add_message(key, "assistant", fallback_reply)
        return

    reply = (parsed.get("reply") or "").strip()
    ready = parsed.get("ready", False)
    params = parsed.get("params") or {}
    creative_brief = parsed.get("creative_brief") or {}
    user_image_requests = parsed.get("user_image_requests") or {}

    if not reply:
        log.warning("对话层返回空 reply，原始: %s", raw[:300])
        reply = "Could you tell me more about what you'd like the blog post to cover?"

    # 兜底：GPT 可能输出 Markdown **bold** 而非 Slack *bold*，自动转换
    reply = re.sub(r'\*\*(.+?)\*\*', r'*\1*', reply)

    # ── 5. 合并参数 + 创意简报 + 图片要求 + 记录回复 ──
    # ⚠️ 三个字段都可能被 GPT 返回成非 dict（list / str），旧代码直接 .items()
    #    会 AttributeError 并被 main.py 的 _safe_run 吞掉 → 用户等不到任何回复。
    if isinstance(params, dict):
        _merge_params(sess, params)
    elif params:
        log.warning("对话层 params 不是对象，已忽略: %r", params)

    # creative_brief: ready=true 时替换（完整简报），ready=false 时增量合并非空字段
    if isinstance(creative_brief, dict) and creative_brief:
        if ready:
            # ready=true: 整体替换为 GPT 综合的完整简报
            sess["creative_brief"] = creative_brief
        else:
            # 对话中: 逐步补充已确定的部分（非空字段覆盖）
            for k, v in creative_brief.items():
                if v:  # 非空值才覆盖
                    sess["creative_brief"][k] = v

    # user_image_requests: 始终增量合并（用户可能分多轮描述不同图片）
    if isinstance(user_image_requests, dict) and user_image_requests:
        for slot, desc in user_image_requests.items():
            if isinstance(desc, str) and desc.strip():
                sess["user_image_requests"][slot] = desc.strip()

    # 上面三处是直接改 sess dict（没走 session.py 的 setter），必须手动落盘，
    # 否则重启后 params/creative_brief 丢失，用户得从头再说一遍需求
    session.persist()

    session.add_message(key, "assistant", reply)

    # ── 6. 判断是否开始生成 ──
    if ready:
        # 进入确认状态，等用户点 Confirm 按钮或继续修改
        session.update_stage(key, CONFIRMING)

        from slack_ui.blocks import build_confirm_blocks
        blocks = build_confirm_blocks(reply, lang=lang)
        say(text=reply, blocks=blocks, thread_ts=thread_ts)
    else:
        say(text=reply, thread_ts=thread_ts)


def _merge_params(sess: dict, new_params: dict):
    """合并 GPT 提取的新参数到会话中

    规则：
    - 非空值才覆盖旧值（避免 GPT 返回空值清掉已有参数）
    - "__clear__" 特殊值：删除该字段（用户明确取消某参数时）
    - bool True / int > 0 / 非空 str / 非空 list / 非空 dict 才算有效值

    Args:
        sess:       会话字典
        new_params: GPT 本轮提取的参数

    示例:
        # 第一轮: GPT 提取到 topic
        _merge_params(sess, {"topic": "Tesla PPF Guide", "primary_keyword": ""})
        # → sess["params"] = {"topic": "Tesla PPF Guide"}  (空字符串不覆盖)

        # 第二轮: GPT 提取到 keyword
        _merge_params(sess, {"primary_keyword": "Tesla PPF Lakeview"})
        # → sess["params"] = {"topic": "Tesla PPF Guide", "primary_keyword": "Tesla PPF Lakeview"}

        # 用户说"不需要特殊风格了"
        _merge_params(sess, {"style": "__clear__"})
        # → sess["params"] 中 style 字段被删除
    """
    existing = sess["params"]
    for key, value in new_params.items():
        if value == "__clear__":
            existing.pop(key, None)
        elif isinstance(value, bool) and value:
            existing[key] = value
        elif isinstance(value, int) and value > 0:
            existing[key] = value
        elif isinstance(value, str) and value.strip():
            existing[key] = value
        elif isinstance(value, list) and len(value) > 0:
            existing[key] = value
        elif isinstance(value, dict) and len(value) > 0:
            existing[key] = value


def _format_draft_context(draft: dict) -> str:
    """把当前草稿格式化为上下文提示，注入到 GPT 对话中供修改时参考

    Args:
        draft: 会话中的 draft 字典
               示例: {"result": {pipeline 返回的完整 result dict}, "session_id": "..."}

    Returns:
        格式化的草稿上下文字符串

    输出示例:
        [Current draft (user may request modifications):]
        [Title]: Ultimate Guide to Tesla PPF Paint Protection Film
        [Excerpt]: Protect your Tesla with XPEL PPF...
        [Tags]: #PPF #Tesla #PaintProtection
        [Images]: 3 images (hero, mid, end)
        [Review Score]: 88/100
    """
    result = draft.get("result", {})
    if not result:
        return ""

    parts = ["[Current draft (user may request modifications):]"]

    blog_data = result.get("blog_data", {})
    if blog_data:
        if blog_data.get("title"):
            parts.append(f"[Title]: {blog_data['title']}")
        if blog_data.get("excerpt"):
            parts.append(f"[Excerpt]: {blog_data['excerpt']}")
        if blog_data.get("content_html"):
            # 只显示前 500 字符，避免 prompt 过长
            content_preview = blog_data["content_html"][:500].replace("<", "&lt;")
            parts.append(f"[Content preview]: {content_preview}...")
        if blog_data.get("tags"):
            parts.append(f"[Tags]: {' '.join('#' + t for t in blog_data['tags'])}")

    score = result.get("review_score", 0)
    if score:
        parts.append(f"[Review Score]: {score}/100")

    preview_url = result.get("preview_url", "")
    if preview_url:
        parts.append(f"[Preview]: {preview_url}")

    return "\n".join(parts)
