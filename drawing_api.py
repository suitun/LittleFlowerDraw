"""OpenAI 兼容的图片编辑接口配置与客户端。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

import base64
import io
import json
import math
import os
import re
import threading
import uuid

from maibot_sdk import Field, PluginConfigBase
from PIL import Image, UnidentifiedImageError
from pydantic import field_validator, model_validator


class DrawingApiError(RuntimeError):
    pass


class DrawingApiConfig(PluginConfigBase):
    base_url: str = Field(default="", max_length=2000, description="中转站地址，可填到 /v1 或完整 /v1/images/edits")
    api_key: str = Field(default="", max_length=4000, description="API Token，使用 Bearer 认证")
    model: str = Field(default="gpt-image-1", max_length=200, description="OpenAI 图片模型")
    size: str = Field(
        default="auto",
        max_length=20,
        description="生成尺寸：auto、reference，或 1024x1024 这类固定尺寸",
    )
    timeout_seconds: int = Field(default=180, ge=30, le=600, description="单次 API 请求超时秒数")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("API URL 必须是 http 或 https 地址")
        return value

    @field_validator("api_key", "model")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("size")
    @classmethod
    def validate_size(cls, value: str) -> str:
        value = value.strip()
        if value in {"auto", "reference"}:
            return value
        match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", value)
        if match is None:
            raise ValueError("尺寸应为 auto、reference 或 1024x1024 格式")
        width, height = int(match.group(1)), int(match.group(2))
        if not (256 <= width <= 2048 and 256 <= height <= 2048):
            raise ValueError("尺寸每条边必须在 256-2048 像素之间")
        if width * height > 4_194_304:
            raise ValueError("尺寸不能超过 4 百万像素")
        return value

    @model_validator(mode="after")
    def validate_model(self) -> "DrawingApiConfig":
        if self.base_url and not self.model:
            raise ValueError("配置了 API URL 时模型不能为空")
        return self

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def endpoint(self) -> str:
        value = self.base_url.rstrip("/")
        if value.endswith("/images/edits"):
            return value
        if value.endswith("/v1"):
            return value + "/images/edits"
        return value + "/v1/images/edits"


class DrawingApiStore:
    """API 配置独立保存，避免和普通插件设置一起被覆盖。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "drawing_api.json"
        self._lock = threading.Lock()
        self._config: Optional[DrawingApiConfig] = None

    def load(self) -> DrawingApiConfig:
        with self._lock:
            if self.path.is_file():
                config = DrawingApiConfig.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
            else:
                config = DrawingApiConfig()
                self._write(config)
            self._config = config
            return config.model_copy(deep=True)

    def save(self, config: DrawingApiConfig) -> DrawingApiConfig:
        with self._lock:
            self._write(config)
            self._config = config
            return config.model_copy(deep=True)

    def current(self) -> DrawingApiConfig:
        with self._lock:
            if self._config is None:
                raise RuntimeError("绘图 API 配置尚未加载")
            return self._config.model_copy(deep=True)

    def _write(self, config: DrawingApiConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


def _png_bytes(image: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(image)) as source:
            if source.mode in {"RGBA", "LA"}:
                converted = source.convert("RGBA")
            else:
                converted = source.convert("RGB")
            output = io.BytesIO()
            converted.save(output, format="PNG")
            return output.getvalue()
    except (UnidentifiedImageError, OSError) as error:
        raise DrawingApiError("参考图或角色图不是有效图片") from error


def _multipart(
    fields: Dict[str, str],
    files: List[tuple[str, str, bytes]],
) -> tuple[bytes, str]:
    boundary = "----LittleFlower" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, filename, content in files:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(b"Content-Type: image/png\r\n\r\n")
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


@dataclass(frozen=True)
class DrawingResult:
    image: bytes
    prompt: str


def models_endpoint(base_url: str) -> str:
    value = base_url.rstrip("/")
    if value.endswith("/images/edits"):
        value = value[: -len("/images/edits")]
    if value.endswith("/v1"):
        return value + "/models"
    return value + "/v1/models"


def fetch_models(base_url: str, api_key: str, timeout: int = 30) -> List[str]:
    """查询 OpenAI 兼容的 /v1/models，供 WebUI 下拉选择。"""
    if not base_url:
        raise DrawingApiError("请先填写 API URL")
    request = Request(
        models_endpoint(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "LittleFlowerDraw/0.7",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as error:
        detail = error.read(1200).decode("utf-8", "replace")
        raise DrawingApiError(f"模型列表返回 HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise DrawingApiError(f"模型列表请求失败: {error}") from error
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DrawingApiError("模型列表返回的不是有效 JSON") from error
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise DrawingApiError("模型列表没有 data 数组")
    models = sorted(
        {
            str(item["id"])
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
        }
    )
    if not models:
        raise DrawingApiError("模型列表为空")
    return models


def _resolve_size(config: DrawingApiConfig, reference: bytes) -> str:
    if config.size == "auto":
        return "auto"
    if config.size != "reference":
        return config.size
    try:
        with Image.open(io.BytesIO(reference)) as image:
            ratio = image.width / image.height
    except (UnidentifiedImageError, OSError) as error:
        raise DrawingApiError("参考图不是有效图片") from error
    choices = {"1024x1024": 1.0, "1536x1024": 1.5, "1024x1536": 2 / 3}
    return min(choices, key=lambda value: abs(math.log(ratio / choices[value])))


class OpenAIImageClient:
    """调用 OpenAI 兼容的 /v1/images/edits，多张图片按顺序上传。"""

    def generate(self, config: DrawingApiConfig, prompt: str, images: List[bytes]) -> DrawingResult:
        if not config.configured:
            raise DrawingApiError("绘图 API 尚未配置")
        if not images:
            raise DrawingApiError("绘图 API 需要至少一张图片")
        png_images = [_png_bytes(image) for image in images]
        fields = {
            "model": config.model,
            "prompt": prompt,
            "size": _resolve_size(config, png_images[0]),
            "n": "1",
        }
        files = [
            ("image[]", f"image{index + 1}.png", image)
            for index, image in enumerate(png_images)
        ]
        body, boundary = _multipart(fields, files)
        request = Request(
            config.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": "LittleFlowerDraw/0.6",
            },
        )
        try:
            with urlopen(request, timeout=config.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read(1200).decode("utf-8", "replace")
            raise DrawingApiError(f"绘图 API 返回 HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise DrawingApiError(f"绘图 API 请求失败: {error}") from error

        try:
            payload: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DrawingApiError("绘图 API 返回的不是有效 JSON") from error
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            message = payload.get("error") if isinstance(payload, dict) else None
            raise DrawingApiError(f"绘图 API 没有返回图片: {message or str(payload)[:500]}")
        item = items[0]
        image_bytes = self._decode_item(item, config, request.full_url)
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as error:
            raise DrawingApiError("绘图 API 返回的图片无法识别") from error
        return DrawingResult(image=image_bytes, prompt=prompt)

    def _decode_item(self, item: Dict[str, Any], config: DrawingApiConfig, endpoint: str) -> bytes:
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            if encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[-1]
            try:
                return base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as error:
                raise DrawingApiError("绘图 API 返回的 base64 图片无效") from error
        remote = item.get("url")
        if isinstance(remote, str) and remote:
            if remote.startswith("data:"):
                return self._decode_data_uri(remote)
            try:
                download_url = urljoin(endpoint, remote)
                headers = {"Accept": "image/*"}
                target = urlsplit(download_url)
                source = urlsplit(endpoint)
                if (target.scheme, target.netloc) == (source.scheme, source.netloc):
                    headers["Authorization"] = f"Bearer {config.api_key}"
                request = Request(download_url, headers=headers)
                with urlopen(request, timeout=config.timeout_seconds) as response:
                    return response.read()
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                raise DrawingApiError(f"下载绘图结果失败: {error}") from error
        raise DrawingApiError("绘图 API 返回中没有 b64_json 或 url")

    @staticmethod
    def _decode_data_uri(value: str) -> bytes:
        try:
            return base64.b64decode(value.split(",", 1)[-1], validate=True)
        except (ValueError, TypeError) as error:
            raise DrawingApiError("绘图 API 返回的 data URI 无效") from error
