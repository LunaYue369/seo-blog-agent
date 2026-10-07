"""WordPress 发布服务 — 通过 WP REST API 发布博客文章

功能：
1. 上传图片到 WordPress 媒体库（返回媒体 ID 和 URL）
2. 创建/查找 WordPress 标签（tags）
3. 替换文章内容中的 base64 图片为 WP 媒体库 URL
4. 发布文章（支持 private/draft/publish 状态）
5. 设置 featured image（特色图片）

认证方式：WordPress Application Passwords
凭据从 .env 读取，格式：WP_{MERCHANT_ID}_USER / WP_{MERCHANT_ID}_PASSWORD
WP 站点 URL 从 merchant.json 的 wordpress_url 字段读取

使用方式：
    publisher = WordPressPublisher(merchant_id, merchant_cfg)
    result = publisher.publish_blog(blog_data, image_paths)
"""

import logging
import mimetypes
import os
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ── 请求超时（秒）──────────────────────────────────────────
_TIMEOUT = 30
_UPLOAD_TIMEOUT = 60  # 图片上传可能较慢


class WordPressPublisher:
    """WordPress REST API 客户端 — 负责图片上传和文章发布"""

    def __init__(self, merchant_id: str, merchant_cfg: dict):
        """初始化 WordPress 发布器

        从 merchant_cfg 获取 WP 站点 URL，从环境变量获取认证凭据。

        Args:
            merchant_id: 商家标识（如 "harbor_auto_spa"）
            merchant_cfg: 商家配置字典（必须包含 wordpress_url）
        """
        self.merchant_id = merchant_id

        # WordPress 站点 URL（从 merchant.json 读取）
        self.wp_url = merchant_cfg.get("wordpress_url", "").rstrip("/")
        if not self.wp_url:
            raise ValueError(f"[{merchant_id}] merchant.json 缺少 wordpress_url 配置")

        # REST API 基础路径
        self.api_base = f"{self.wp_url}/wp-json/wp/v2"

        # 认证凭据从环境变量读，不放进 merchant.json（那个文件会进仓库）
        # 例：merchant_id = "harbor_auto_spa" -> WP_HARBOR_AUTO_SPA_USER / WP_HARBOR_AUTO_SPA_PASSWORD
        prefix = f"WP_{merchant_id.upper()}"
        self.wp_user = os.getenv(f"{prefix}_USER", "")
        self.wp_password = os.getenv(f"{prefix}_PASSWORD", "")

        if not self.wp_user or not self.wp_password:
            raise ValueError(
                f"[{merchant_id}] 缺少 WordPress 凭据，请在 .env 中配置 {prefix}_USER 和 {prefix}_PASSWORD"
            )

        # requests 的 HTTP Basic Auth（WP Application Password 用这种方式认证）
        self._auth = (self.wp_user, self.wp_password)

        log.info("[%s] WordPress 发布器初始化完成: %s", merchant_id, self.wp_url)

    # ── 图片上传 ────────────────────────────────────────────

    def upload_image(self, image_path: Path, alt_text: str = "") -> dict:
        """上传一张图片到 WordPress 媒体库

        Args:
            image_path: 本地图片文件路径
            alt_text: 图片的 alt 文本（SEO 用）

        Returns:
            {"id": 媒体ID, "url": 图片URL, "success": True/False}
        """
        if not image_path or not image_path.exists():
            log.warning("[%s] 图片文件不存在: %s", self.merchant_id, image_path)
            return {"id": 0, "url": "", "success": False}

        # 读取图片文件
        filename = image_path.name
        mime_type = mimetypes.guess_type(filename)[0] or "image/png"

        try:
            with open(image_path, "rb") as f:
                image_data = f.read()

            # 通过 WP REST API 上传
            # 请求头里的 Content-Disposition 告诉 WP 文件名
            headers = {
                "Content-Type": mime_type,
                "Content-Disposition": f'attachment; filename="{filename}"',
            }

            resp = requests.post(
                f"{self.api_base}/media",
                auth=self._auth,
                headers=headers,
                data=image_data,
                timeout=_UPLOAD_TIMEOUT,
            )
            resp.raise_for_status()

            media = resp.json()
            media_id = media.get("id", 0)
            media_url = media.get("source_url", "")

            # 如果提供了 alt_text，更新图片的 alt 属性（需要额外一次请求）
            if alt_text and media_id:
                try:
                    requests.post(
                        f"{self.api_base}/media/{media_id}",
                        auth=self._auth,
                        json={"alt_text": alt_text},
                        timeout=_TIMEOUT,
                    )
                except Exception:
                    pass  # alt_text 更新失败不影响主流程

            log.info("[%s] 图片上传成功: %s → ID=%d URL=%s",
                     self.merchant_id, filename, media_id, media_url)

            return {"id": media_id, "url": media_url, "success": True}

        except requests.RequestException as exc:
            log.error("[%s] 图片上传失败 (%s): %s", self.merchant_id, filename, exc)
            return {"id": 0, "url": "", "success": False}

    # ── 标签处理 ────────────────────────────────────────────

    def _get_or_create_tag(self, tag_name: str) -> int:
        """获取已有的 WordPress 标签 ID，如果不存在则创建

        WordPress 标签是全局的，同名标签只会有一个。
        先搜索，找到就返回 ID；找不到就创建新的。

        Args:
            tag_name: 标签名称（如 "ceramic coating"）

        Returns:
            标签 ID（失败返回 0）
        """
        try:
            # 先搜索已有标签
            resp = requests.get(
                f"{self.api_base}/tags",
                auth=self._auth,
                params={"search": tag_name, "per_page": 5},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            tags = resp.json()

            # 精确匹配（搜索是模糊的，需要二次过滤）
            for tag in tags:
                if tag.get("name", "").lower() == tag_name.lower():
                    return tag["id"]

            # 没找到 → 创建新标签
            resp = requests.post(
                f"{self.api_base}/tags",
                auth=self._auth,
                json={"name": tag_name},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            new_tag = resp.json()
            tag_id = new_tag.get("id", 0)
            log.info("[%s] 创建新标签: %s → ID=%d", self.merchant_id, tag_name, tag_id)
            return tag_id

        except requests.RequestException as exc:
            log.error("[%s] 标签处理失败 (%s): %s", self.merchant_id, tag_name, exc)
            return 0

    def _resolve_tags(self, tag_names: list[str]) -> list[int]:
        """将标签名称列表转换为 WordPress 标签 ID 列表

        Args:
            tag_names: 标签名称列表（如 ["ppf", "ceramic coating", "tesla"]）

        Returns:
            标签 ID 列表
        """
        tag_ids = []
        for name in tag_names:
            name = name.strip()
            if not name:
                continue
            tag_id = self._get_or_create_tag(name)
            if tag_id:
                tag_ids.append(tag_id)
        return tag_ids

    # ── 内容处理 ────────────────────────────────────────────

    @staticmethod
    def _insert_images(content_html: str, image_url_map: dict[str, str],
                       title: str = "", image_alts: dict[str, str] | None = None) -> str:
        """将 WP 媒体库图片 URL 插入文章内容

        兼容两种占位符格式：
        - Auto 模式: <!-- BLOG_IMAGE:hero/mid/end -->
        - Chat 模式: <!-- BLOG_IMAGE:img_1/img_2/.../img_N -->
        替换为带 WP 图片 URL 的 <img> 标签。
        """
        alts = image_alts or {}

        inserted_count = 0
        for slot, wp_url in image_url_map.items():
            if not wp_url:
                continue

            # 动态生成 CSS class: hero → hero-inline, img_1 → img_1-image
            if slot in ("hero", "mid", "end"):
                css_class = {"hero": "hero-inline", "mid": "mid-image", "end": "end-image"}[slot]
            else:
                css_class = f"{slot}-image"

            # 优先用 Copywriter 提供的 SEO alt text，fallback 用标题
            alt_text = alts.get(slot, f"{title} - {slot}" if title else "Blog post image")

            # 第一张图 eager loading（hero 或 img_1）
            loading = "eager" if slot in ("hero", "img_1") else "lazy"

            # 方式 1：替换 <!-- BLOG_IMAGE:slot --> 注释占位符（主要路径）
            placeholder = f"<!-- BLOG_IMAGE:{slot} -->"
            if placeholder in content_html:
                img_html = (
                    f'<div class="blog-image {css_class}">'
                    f'<img src="{wp_url}" alt="{alt_text}" loading="{loading}">'
                    f'</div>'
                )
                content_html = content_html.replace(placeholder, img_html, 1)
                inserted_count += 1
                continue

            # 方式 2：替换已嵌入的 base64 data URI（兼容旧路径）
            if css_class:
                pattern = (
                    rf'(<div\s+class="blog-image\s+{re.escape(css_class)}">'
                    rf'\s*<img\s+src=")data:image/[^"]*(")'
                )
                replacement = rf'\g<1>{wp_url}\g<2>'
                new_html = re.sub(pattern, replacement, content_html, count=1)
                if new_html != content_html:
                    content_html = new_html
                    inserted_count += 1

        # 安全网：仅在占位符/base64 都没匹配到时才触发
        # 防止场景 C（占位符 + 假标签同时存在）导致图片重复
        fake_img_pattern = re.compile(r'<img\s+[^>]*src="(/images/[^"]+)"[^>]*/?\s*>')
        if inserted_count > 0:
            # 占位符已成功插入真图 → 假标签直接删掉，不替换
            content_html, fake_count = fake_img_pattern.subn("", content_html)
            if fake_count:
                log.warning("[%s] 删除了 %d 个多余的假 <img> 标签（真图已通过占位符插入）", "wp", fake_count)
        else:
            # 没有占位符 → 假标签是唯一的图片位置，替换为真图
            fake_matches = list(fake_img_pattern.finditer(content_html))
            if fake_matches:
                slots = list(image_url_map.keys())
                for i, match in enumerate(reversed(fake_matches)):
                    slot_idx = len(fake_matches) - 1 - i
                    if slot_idx < len(slots):
                        slot = slots[slot_idx]
                        wp_url = image_url_map[slot]
                        css_class = f"{slot}-image" if slot not in ("hero", "mid", "end") else {"hero": "hero-inline", "mid": "mid-image", "end": "end-image"}[slot]
                        alt_text = alts.get(slot, f"{title} - {slot}" if title else "Blog post image")
                        loading = "eager" if slot in ("hero", "img_1") else "lazy"
                        replacement = (
                            f'<div class="blog-image {css_class}">'
                            f'<img src="{wp_url}" alt="{alt_text}" loading="{loading}">'
                            f'</div>'
                        )
                    else:
                        replacement = ""
                    content_html = content_html[:match.start()] + replacement + content_html[match.end():]
                log.warning("[%s] 安全网触发：替换了 %d 个假 <img> 标签为真图", "wp", len(fake_matches))

        return content_html

    # ── 发布文章 ────────────────────────────────────────────

    def publish_blog(
        self,
        blog_data: dict,
        image_paths: dict[str, Path],
        status: str = "private",
    ) -> dict:
        """发布一篇博客文章到 WordPress

        完整流程：
        1. 上传 3 张图片（hero/mid/end）到媒体库
        2. 替换 content_html 中的 base64 为 WP 图片 URL
        3. 创建/查找 WordPress 标签
        4. 创建文章（设置 featured image + tags + status）

        Args:
            blog_data: 博客数据字典
                {title, content_html, excerpt, tags, seo_slug, image_prompts}
            image_paths: 本地图片路径
                {"hero": Path, "mid": Path, "end": Path}
            status: 文章状态
                "private"  — 仅登录用户可见（默认）
                "draft"    — 草稿
                "publish"  — 公开发布

        Returns:
            {
                "success": True/False,
                "post_id": WordPress 文章 ID,
                "post_url": 文章 URL,
                "edit_url": 后台编辑 URL,
                "error": 错误信息（失败时）,
            }
        """
        title = blog_data.get("title", "Untitled")
        content_html = blog_data.get("content_html", "")
        excerpt = blog_data.get("excerpt", "")
        tags = blog_data.get("tags", [])
        seo_slug = blog_data.get("seo_slug", "")
        image_alts = blog_data.get("image_alts", {})

        log.info("[%s] 开始发布到 WordPress: %s", self.merchant_id, title)

        # ── Step 1: 上传图片到媒体库 ──────────────────────
        log.info("[%s] Step 1: 上传图片到媒体库...", self.merchant_id)
        uploaded_images = {}   # {slot: {"id": int, "url": str}}
        image_url_map = {}     # {slot: url} — 用于替换 content 中的 base64

        for slot, img_path in image_paths.items():
            if not img_path:
                continue

            # 用 Copywriter 提供的 SEO alt text（没有就用标题）
            alt_text = image_alts.get(slot, f"{title} - {slot} image")
            result = self.upload_image(img_path, alt_text=alt_text)

            if result["success"]:
                uploaded_images[slot] = result
                image_url_map[slot] = result["url"]
            else:
                log.warning("[%s] %s 图片上传失败，跳过", self.merchant_id, slot)

        log.info("[%s] 图片上传完成: %d/%d 成功", self.merchant_id, len(uploaded_images), len(image_paths))

        # ── Step 2: 将 WP 图片 URL 插入 content 占位符 ─────
        if image_url_map:
            content_html = self._insert_images(content_html, image_url_map, title, image_alts)
            log.info("[%s] Step 2: 已插入 %d 张图片到文章内容", self.merchant_id, len(image_url_map))

        # ── Step 3: 处理标签 ──────────────────────────────
        log.info("[%s] Step 3: 处理标签...", self.merchant_id)
        tag_ids = self._resolve_tags(tags)
        log.info("[%s] 标签处理完成: %d 个标签", self.merchant_id, len(tag_ids))

        # ── Step 3.5: 将 tags 追加到正文末尾（链接到 WP tag 归档页）──
        if tags:
            tag_links = []
            for t in tags:
                # WP tag slug: 小写 + 空格转连字符
                tag_slug = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
                tag_url = f"{self.wp_url}/tag/{tag_slug}/"
                tag_links.append(
                    f'<a href="{tag_url}" rel="tag" style="display:inline-block;'
                    f'background:#f0f0f0;color:#333;padding:4px 14px;'
                    f'border-radius:20px;margin:4px 6px 4px 0;font-size:0.85em;'
                    f'text-decoration:none;">{t}</a>'
                )
            content_html += (
                f'\n<div style="margin-top:40px;padding-top:24px;'
                f'border-top:1px solid #e0e0e0;">{"".join(tag_links)}</div>'
            )

        # ── Step 4: 创建文章 ──────────────────────────────
        log.info("[%s] Step 4: 创建文章 (status=%s)...", self.merchant_id, status)

        post_data = {
            "title": title,
            "content": content_html,
            "excerpt": excerpt,
            "slug": seo_slug,
            "status": status,
            "tags": tag_ids,
            "comment_status": "open",   # 允许评论（SEO 互动信号）
            "ping_status": "open",      # 允许 pingback
        }

        # 设置 featured image（特色图片）— 用第一张图（auto 模式是 hero，chat 模式是 img_1）
        first_slot = next(iter(uploaded_images), None)
        if first_slot:
            post_data["featured_media"] = uploaded_images[first_slot]["id"]

        try:
            resp = requests.post(
                f"{self.api_base}/posts",
                auth=self._auth,
                json=post_data,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()

            post = resp.json()
            post_id = post.get("id", 0)
            post_url = post.get("link", "")
            edit_url = f"{self.wp_url}/wp-admin/post.php?post={post_id}&action=edit"

            # 将上传的图片附加到文章（WP 媒体库显示"已附加"而非"尚未附加"）
            if post_id and uploaded_images:
                for slot, media_info in uploaded_images.items():
                    media_id = media_info.get("id", 0)
                    if media_id:
                        try:
                            requests.post(
                                f"{self.api_base}/media/{media_id}",
                                auth=self._auth,
                                json={"post": post_id},
                                timeout=_TIMEOUT,
                            )
                        except Exception:
                            pass  # 附加失败不影响主流程

            log.info("[%s] 文章发布成功: ID=%d URL=%s", self.merchant_id, post_id, post_url)

            # 收集已上传图片的文件名（slot → filename）
            image_names = {}
            for slot, img_path in image_paths.items():
                if img_path and slot in uploaded_images:
                    image_names[slot] = Path(img_path).name if hasattr(img_path, 'name') else str(img_path).split("/")[-1].split("\\")[-1]

            return {
                "success": True,
                "post_id": post_id,
                "post_url": post_url,
                "edit_url": edit_url,
                "status": status,
                "images_uploaded": len(uploaded_images),
                "image_names": image_names,
                "tags_count": len(tag_ids),
            }

        except requests.RequestException as exc:
            error_msg = str(exc)
            # 尝试从响应体获取更详细的错误信息
            try:
                error_detail = exc.response.json()
                error_msg = error_detail.get("message", error_msg)
            except Exception:
                pass

            log.error("[%s] 文章发布失败: %s", self.merchant_id, error_msg)
            return {
                "success": False,
                "post_id": 0,
                "post_url": "",
                "edit_url": "",
                "error": error_msg,
            }

    # ── 连接测试 ────────────────────────────────────────────

    def test_connection(self) -> dict:
        """测试 WordPress 连接是否正常

        验证凭据有效性和 REST API 可访问性。

        Returns:
            {"success": True/False, "user": 用户名, "error": 错误信息}
        """
        try:
            resp = requests.get(
                f"{self.api_base}/users/me",
                auth=self._auth,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            user = resp.json()
            name = user.get("name", "Unknown")
            log.info("[%s] WordPress 连接测试成功: %s", self.merchant_id, name)
            return {"success": True, "user": name}

        except requests.RequestException as exc:
            log.error("[%s] WordPress 连接测试失败: %s", self.merchant_id, exc)
            return {"success": False, "user": "", "error": str(exc)}
