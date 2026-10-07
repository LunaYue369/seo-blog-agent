"""Seedream 4.5 图片生成客户端 — 火山引擎 API

从 digital-media-worker 项目复制并适配。
支持：文生图、图生图、多图融合、组图生成。
"""

import logging
import os
import base64
import random
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

log = logging.getLogger(__name__)

load_dotenv()


def _unique_name(prefix: str = "image") -> str:
    """生成唯一文件名，避免同目录下多次调用互相覆盖"""
    ts = int(time.time() * 1000)
    rand = random.randint(1000, 9999)
    return f"{prefix}_{ts}_{rand}"


class SeedreamClient:
    """火山引擎 Seedream 图片生成 API 客户端"""

    BASE_URL = "https://ark.cn-beijing.volces.com"
    ENDPOINT = "/api/v3/images/generations"

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or os.getenv("VOLCENGINE_API_KEY")
        self.model = model or os.getenv("SEEDREAM_MODEL", "doubao-seedream-4-5-251128")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _encode_image_base64(self, image_path: Path) -> str:
        """将图片编码为 data URI 格式（Seedream API 要求）"""
        suffix = image_path.suffix.lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp"}.get(suffix.lstrip("."), "image/png")
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    def _prepare_image_param(self, image_paths: list) -> str | list:
        """准备 image 参数：本地文件转 base64，URL 直接使用"""
        images = []
        for p in image_paths:
            p_str = str(p)
            if p_str.startswith("http://") or p_str.startswith("https://"):
                images.append(p_str)
            else:
                path = Path(p)
                if not path.exists():
                    raise FileNotFoundError(f"参考图不存在: {path}")
                images.append(self._encode_image_base64(path))
        return images[0] if len(images) == 1 else images

    def generate_image(self, prompt: str, images: list = None,
                       size: str = "2K", watermark: bool = False,
                       multi_image: bool = False, max_images: int = 4) -> list[str]:
        """统一图片生成方法，返回图片 URL 列表"""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "watermark": watermark,
            "response_format": "url",
            "stream": False,
        }

        if images:
            payload["image"] = self._prepare_image_param(images)
            if not multi_image:
                payload["sequential_image_generation"] = "disabled"

        if multi_image:
            payload["sequential_image_generation"] = "auto"
            payload["sequential_image_generation_options"] = {"max_images": max_images}

        # 记录请求参数（不含 base64 图片数据）
        debug_payload = {k: (f"<base64 {len(v) if isinstance(v, str) else 'list'}>" if k == "image" else v)
                         for k, v in payload.items()}
        log.info("Seedream request: %s", debug_payload)

        response = self.session.post(
            f"{self.BASE_URL}{self.ENDPOINT}",
            json=payload,
            timeout=120,
        )
        if response.status_code != 200:
            log.error("Seedream API error %s:\n%s", response.status_code, response.text[:1000])
            response.raise_for_status()
        result = response.json()

        if "error" in result:
            err = result["error"]
            raise ValueError(f"API 错误 (code={err.get('code')}): {err.get('message')}")

        data = result.get("data", [])
        urls = [item["url"] for item in data if item.get("url")]
        if not urls:
            raise ValueError(f"API 未返回图片: {result}")
        return urls

    def download_image(self, url: str, output_path: Path) -> Path:
        """下载图片到本地"""
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return output_path

    # ── 便捷方法 ──────────────────────────────────────────────

    def text_to_image(self, prompt: str, output_dir: Path,
                      size: str = "2K") -> list[Path]:
        """文生图：纯文本 → 单图"""
        urls = self.generate_image(prompt=prompt, size=size)
        name = _unique_name("txt2img")
        paths = []
        for i, url in enumerate(urls):
            out = output_dir / f"{name}_{i + 1}.png"
            self.download_image(url, out)
            paths.append(out)
        return paths
