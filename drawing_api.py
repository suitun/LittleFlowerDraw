"""OpenAI 兼容的图片编辑接口配置与客户端。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import base64
import io
import json
import math
import os
import re
import threading
import time
import uuid

from maibot_sdk import Field, PluginConfigBase
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import field_validator, model_validator


class DrawingApiError(RuntimeError):
    """绘图接口调用失败；status_code 保留上游 HTTP 状态。"""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.status_code = status_code
        self.response = response or {}
        super().__init__(message)


def _error_response(detail: str) -> Dict[str, Any]:
    try:
        payload = json.loads(detail)
    except (TypeError, json.JSONDecodeError):
        return {"body": detail[:4000]}
    return _sanitize_api_response(payload)


# 中转站不经过代理；使用独立 opener，避免系统代理关闭后 urllib 仍缓存旧代理。
_DIRECT_OPENER = build_opener(ProxyHandler({}))


def urlopen(request: Request, timeout: Optional[float] = None) -> Any:
    return _DIRECT_OPENER.open(request, timeout=timeout)


_TRANSIENT_HTTP_STATUSES = {429, 503}
_TRANSIENT_RETRY_LIMIT = 3


def _retry_delay(error: HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers is not None else None
    if retry_after:
        try:
            return min(60.0, max(0.5, float(retry_after)))
        except ValueError:
            pass
    return min(30.0, 1.0 * (2**attempt))


def urlopen_with_retry(request: Request, timeout: Optional[float] = None) -> Any:
    """对 429/503 做指数退避；优先使用上游 Retry-After。"""
    last_error: Optional[HTTPError] = None
    for attempt in range(_TRANSIENT_RETRY_LIMIT):
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError as error:
            if error.code not in _TRANSIENT_HTTP_STATUSES or attempt == _TRANSIENT_RETRY_LIMIT - 1:
                raise
            last_error = error
            delay = _retry_delay(error, attempt)
            error.close()
            time.sleep(delay)
    if last_error is not None:
        raise last_error
    raise DrawingApiError("绘图 API 请求重试失败")


class DrawingApiConfig(PluginConfigBase):
    profile_id: str = Field(default="default", max_length=64, description="API 配置标识")
    name: str = Field(default="默认 API", max_length=80, description="API 配置名称")
    base_url: str = Field(default="", max_length=2000, description="中转站地址，可填到 /v1 或完整 /v1/images/edits")
    api_key: str = Field(default="", max_length=4000, description="API Token，使用 Bearer 认证")
    model: str = Field(default="gpt-image-1", max_length=200, description="OpenAI 图片模型")
    size: str = Field(
        default="auto",
        max_length=20,
        description="生成尺寸：auto、reference，或 1024x1024 这类固定尺寸",
    )
    quality: str = Field(
        default="auto",
        max_length=20,
        description="输出质量：auto、low、medium、high、xhigh 或 max",
    )
    complexity: str = Field(
        default="auto",
        max_length=20,
        description="规划复杂度：auto、low、medium 或 high；auto 时不发送该字段",
    )
    timeout_seconds: int = Field(default=180, ge=30, le=600, description="单次 API 请求超时秒数")

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        value = value.strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("API 配置标识只能包含字母、数字、下划线和短横线")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("API 配置名称不能为空")
        return value

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
        if width > 3840 or height > 3840:
            raise ValueError("尺寸单边不能超过 3840 像素")
        if width % 16 or height % 16:
            raise ValueError("尺寸宽高必须是 16 的倍数")
        if max(width, height) / min(width, height) > 3:
            raise ValueError("尺寸长短边比例不能超过 3:1")
        if not 655_360 <= width * height <= 8_294_400:
            raise ValueError("尺寸总像素必须在 655360-8294400 之间")
        return value

    @field_validator("quality")
    @classmethod
    def validate_quality(cls, value: str) -> str:
        value = value.strip()
        if value not in {"auto", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("质量必须是 auto、low、medium、high、xhigh 或 max")
        return value

    @field_validator("complexity")
    @classmethod
    def validate_complexity(cls, value: str) -> str:
        value = value.strip()
        if value not in {"auto", "low", "medium", "high"}:
            raise ValueError("复杂度必须是 auto、low、medium 或 high")
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

    @property
    def generation_endpoint(self) -> str:
        value = self.base_url.rstrip("/")
        if value.endswith("/images/edits"):
            value = value[: -len("/images/edits")]
        if value.endswith("/v1"):
            return value + "/images/generations"
        return value + "/v1/images/generations"


class DrawingApiRoute(PluginConfigBase):
    """按群聊或私聊指定使用的绘图 API。"""

    name: str = Field(default="", max_length=80, description="规则名称，仅用于展示")
    scope: str = Field(..., max_length=10, description="group 或 private")
    target_id: str = Field(..., max_length=20, description="群号或 QQ 号")
    profile_id: str = Field(..., max_length=64, description="API 配置标识")

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"group", "private"}:
            raise ValueError("路由类型必须是 group 或 private")
        return value

    @field_validator("target_id")
    @classmethod
    def validate_target(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"\d{5,20}", value):
            raise ValueError("群号或 QQ 号必须是 5-20 位数字")
        return value


class DrawingApiStore:
    """多套 API 配置独立保存；每套配置有自己的模型标识。"""

    _VERSION = 2

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "drawing_api.json"
        self._lock = threading.Lock()
        self._profiles: Dict[str, DrawingApiConfig] = {}
        self._routes: Dict[tuple[str, str], DrawingApiRoute] = {}
        self._active_id = ""

    def load(self) -> DrawingApiConfig:
        with self._lock:
            changed = False
            raw_routes: List[object] = []
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("profiles"), list):
                    profiles = [DrawingApiConfig.model_validate(item) for item in raw["profiles"]]
                    active_id = str(raw.get("active_id") or "").strip()
                    raw_routes = raw.get("routes") if isinstance(raw.get("routes"), list) else []
                else:
                    legacy = DrawingApiConfig.model_validate(raw)
                    profiles = [legacy]
                    active_id = legacy.profile_id
                    changed = True
            else:
                profiles = [DrawingApiConfig()]
                active_id = "default"
                changed = True
            if not profiles:
                profiles = [DrawingApiConfig()]
                changed = True
            self._profiles = {}
            for profile in profiles:
                if profile.profile_id in self._profiles:
                    raise ValueError(f"API 配置标识重复: {profile.profile_id}")
                self._profiles[profile.profile_id] = profile
            if active_id not in self._profiles:
                active_id = next(iter(self._profiles))
                changed = True
            self._active_id = active_id
            routes: Dict[tuple[str, str], DrawingApiRoute] = {}
            for item in raw_routes:
                try:
                    route = DrawingApiRoute.model_validate(item)
                except ValueError:
                    changed = True
                    continue
                if route.profile_id not in self._profiles:
                    changed = True
                    continue
                routes[(route.scope, route.target_id)] = route
            self._routes = routes
            if changed:
                self._write()
            return self._profiles[self._active_id].model_copy(deep=True)

    def save(self, config: DrawingApiConfig) -> DrawingApiConfig:
        """兼容旧版单配置保存：保存后同时设为当前配置。"""
        with self._lock:
            self._profiles[config.profile_id] = config
            self._active_id = config.profile_id
            self._write()
            return config.model_copy(deep=True)

    def save_profile(self, config: DrawingApiConfig) -> DrawingApiConfig:
        """新增或更新一套配置，不改变当前选择。"""
        with self._lock:
            self._profiles[config.profile_id] = config
            if not self._active_id:
                self._active_id = config.profile_id
            self._write()
            return config.model_copy(deep=True)

    def current(self) -> DrawingApiConfig:
        with self._lock:
            if self._active_id not in self._profiles:
                raise RuntimeError("绘图 API 配置尚未加载")
            return self._profiles[self._active_id].model_copy(deep=True)

    def active_id(self) -> str:
        with self._lock:
            return self._active_id

    def list_profiles(self) -> List[DrawingApiConfig]:
        with self._lock:
            return [profile.model_copy(deep=True) for profile in self._profiles.values()]

    def get(self, profile_id: str) -> Optional[DrawingApiConfig]:
        with self._lock:
            profile = self._profiles.get(profile_id)
            return profile.model_copy(deep=True) if profile is not None else None

    def set_active(self, profile_id: str) -> DrawingApiConfig:
        with self._lock:
            if profile_id not in self._profiles:
                raise ValueError("API 配置不存在")
            self._active_id = profile_id
            self._write()
            return self._profiles[profile_id].model_copy(deep=True)

    def list_routes(self) -> List[DrawingApiRoute]:
        with self._lock:
            return [route.model_copy(deep=True) for route in self._routes.values()]

    def save_routes(self, routes: List[DrawingApiRoute]) -> List[DrawingApiRoute]:
        with self._lock:
            next_routes: Dict[tuple[str, str], DrawingApiRoute] = {}
            for route in routes:
                if route.profile_id not in self._profiles:
                    raise ValueError(f"API 配置不存在: {route.profile_id}")
                key = (route.scope, route.target_id)
                if key in next_routes:
                    raise ValueError(f"群号或 QQ 号重复: {route.target_id}")
                next_routes[key] = route
            self._routes = next_routes
            self._write()
            return [route.model_copy(deep=True) for route in self._routes.values()]

    def resolve(self, scope: str, target_id: str) -> Optional[DrawingApiConfig]:
        """按群聊/私聊返回指定配置；没有匹配时返回 None。"""
        route = self._routes.get((scope, target_id))
        if route is None:
            return None
        profile = self._profiles.get(route.profile_id)
        return profile.model_copy(deep=True) if profile is not None else None

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            if profile_id not in self._profiles:
                raise ValueError("API 配置不存在")
            if len(self._profiles) <= 1:
                raise ValueError("至少保留一套 API 配置")
            del self._profiles[profile_id]
            self._routes = {
                key: route for key, route in self._routes.items() if route.profile_id != profile_id
            }
            if self._active_id == profile_id:
                self._active_id = next(iter(self._profiles))
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self._VERSION,
            "active_id": self._active_id,
            "profiles": [profile.model_dump(mode="json") for profile in self._profiles.values()],
            "routes": [route.model_dump(mode="json") for route in self._routes.values()],
        }
        temporary = self.path.with_name(self.path.name + f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
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


def _upload_format(image: bytes) -> tuple[bytes, str, str]:
    """能直接上传就保留原格式；不能直接上传的格式再转 PNG。"""
    try:
        with Image.open(io.BytesIO(image)) as source:
            source_format = (source.format or "").upper()
            formats = {
                "PNG": ("png", "image/png"),
                "JPEG": ("jpg", "image/jpeg"),
                "WEBP": ("webp", "image/webp"),
            }
            if source_format in formats:
                extension, content_type = formats[source_format]
                return image, extension, content_type
    except (UnidentifiedImageError, OSError) as error:
        raise DrawingApiError("参考图或角色图不是有效图片") from error
    return _png_bytes(image), "png", "image/png"


def _resized_upload(image: bytes, edge: int) -> tuple[bytes, str, str]:
    try:
        with Image.open(io.BytesIO(image)) as source:
            source = ImageOps.exif_transpose(source)
            has_alpha = source.mode in {"RGBA", "LA"} or (
                source.mode == "P" and "transparency" in source.info
            )
            converted = source.convert("RGBA" if has_alpha else "RGB")
            converted.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            if has_alpha:
                converted.save(output, format="PNG", optimize=True)
                return output.getvalue(), "png", "image/png"
            converted.save(output, format="JPEG", quality=92, optimize=True)
            return output.getvalue(), "jpg", "image/jpeg"
    except (UnidentifiedImageError, OSError) as error:
        raise DrawingApiError("参考图或角色图不是有效图片") from error


def _prepare_uploads(images: List[bytes]) -> List[tuple[bytes, str, str]]:
    """优先原格式上传；总请求超过约 18MB 时才按比例压缩，给接口留余量。"""
    prepared = [_upload_format(image) for image in images]
    total = sum(len(data) for data, _, _ in prepared)
    target = 18 * 1024**2
    for _ in range(5):
        if total <= target:
            return prepared
        ratio = max(0.35, min(1.0, (target / total) ** 0.5))
        updated: List[tuple[bytes, str, str]] = []
        for data, extension, content_type in prepared:
            try:
                with Image.open(io.BytesIO(data)) as source:
                    edge = max(source.size)
            except (UnidentifiedImageError, OSError) as error:
                raise DrawingApiError("参考图或角色图不是有效图片") from error
            next_edge = max(512, int(edge * ratio))
            if next_edge >= edge:
                updated.append((data, extension, content_type))
            else:
                updated.append(_resized_upload(data, next_edge))
        prepared = updated
        total = sum(len(data) for data, _, _ in prepared)
    if total > target:
        raise DrawingApiError("图片文件过大，无法压到接口可接受的大小")
    return prepared


def _multipart(
    fields: Dict[str, str],
    files: List[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = "----LittleFlower" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, filename, content, content_type in files:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


@dataclass(frozen=True)
class DrawingResult:
    image: bytes
    prompt: str
    response: Dict[str, Any] = field(default_factory=dict)


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


def _sanitize_api_response(payload: Any) -> Dict[str, Any]:
    """保留完整响应结构，但省略重复的大段 base64 图片数据。"""
    if not isinstance(payload, dict):
        return {"payload": str(payload)[:2000]}
    try:
        result = json.loads(json.dumps(payload))
    except (TypeError, ValueError):
        return {"payload": str(payload)[:2000]}
    items = result.get("data")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            encoded = item.get("b64_json")
            if isinstance(encoded, str):
                item["b64_json"] = f"<omitted {len(encoded)} chars>"
    return result


def _bad_request_message(detail: str, status_code: int = 400) -> str:
    """把上游 400/451 的文本归类成用户能看懂的原因。"""
    lowered = detail.lower()
    content_tokens = (
        "内容政策",
        "内容审核",
        "审核",
        "违反",
        "敏感",
        "content policy",
        "safety",
        "unsafe",
        "moderation",
    )
    if any(token in lowered for token in content_tokens):
        return f"接口返回 {status_code}：图片或提示词内容未通过接口审核"
    if any(token in lowered for token in ("too large", "size", "maximum", "4mb", "文件过大")):
        return f"接口返回 {status_code}：图片文件太大或尺寸不符合接口要求"
    if any(token in lowered for token in ("格式", "format", "图片有误", "invalid image", "unsupported", "supported")):
        return f"接口返回 {status_code}：图片格式或输入不符合接口要求"
    return f"接口返回 {status_code}，但没有提供明确原因"


class OpenAIImageClient:
    """调用 OpenAI 兼容的 /v1/images/edits，多张图片按顺序上传。"""

    def generate(self, config: DrawingApiConfig, prompt: str, images: List[bytes]) -> DrawingResult:
        if not config.configured:
            raise DrawingApiError("绘图 API 尚未配置")
        if not images:
            raise DrawingApiError("绘图 API 需要至少一张图片")
        prepared = _prepare_uploads(images)
        fields = {
            "model": config.model,
            "prompt": prompt,
            "size": _resolve_size(config, prepared[0][0]),
            "n": "1",
            "quality": config.quality,
            "output_format": "png",
        }
        if config.complexity != "auto":
            fields["complexity"] = config.complexity
        files = [
            ("image[]", f"image{index + 1}.{extension}", data, content_type)
            for index, (data, extension, content_type) in enumerate(prepared)
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
            with urlopen_with_retry(request, timeout=config.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read(1200).decode("utf-8", "replace")
            if error.code in (400, 451):
                error.close()
                raise DrawingApiError(
                    _bad_request_message(detail, error.code),
                    status_code=error.code,
                    response=_error_response(detail),
                ) from error
            raise DrawingApiError(
                f"绘图 API 返回 HTTP {error.code}: {detail}",
                status_code=error.code,
                response=_error_response(detail),
            ) from error
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
        return DrawingResult(
            image=image_bytes,
            prompt=prompt,
            response=_sanitize_api_response(payload),
        )

    def generate_text(self, config: DrawingApiConfig, prompt: str) -> DrawingResult:
        """没有参考图时走 /v1/images/generations 的纯文生图。"""
        if not config.configured:
            raise DrawingApiError("绘图 API 尚未配置")
        payload = {
            "model": config.model,
            "prompt": prompt,
            "size": "auto" if config.size == "reference" else config.size,
            "n": 1,
            "quality": config.quality,
            "output_format": "png",
        }
        if config.complexity != "auto":
            payload["complexity"] = config.complexity
        endpoint = config.generation_endpoint
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "LittleFlowerDraw/0.6",
            },
        )
        try:
            with urlopen_with_retry(request, timeout=config.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read(1200).decode("utf-8", "replace")
            if error.code in (400, 451):
                error.close()
                raise DrawingApiError(
                    _bad_request_message(detail, error.code),
                    status_code=error.code,
                    response=_error_response(detail),
                ) from error
            raise DrawingApiError(
                f"绘图 API 返回 HTTP {error.code}: {detail}",
                status_code=error.code,
                response=_error_response(detail),
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise DrawingApiError(f"绘图 API 请求失败: {error}") from error

        try:
            payload_data: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DrawingApiError("绘图 API 返回的不是有效 JSON") from error
        items = payload_data.get("data") if isinstance(payload_data, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            message = payload_data.get("error") if isinstance(payload_data, dict) else None
            raise DrawingApiError(f"绘图 API 没有返回图片: {message or str(payload_data)[:500]}")
        image_bytes = self._decode_item(items[0], config, endpoint)
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as error:
            raise DrawingApiError("绘图 API 返回的图片无法识别") from error
        return DrawingResult(
            image=image_bytes,
            prompt=prompt,
            response=_sanitize_api_response(payload_data),
        )

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
            download_url = urljoin(endpoint, remote)
            return self._download_image(download_url, config, endpoint)
        raise DrawingApiError("绘图 API 返回中没有 b64_json 或 url")

    @staticmethod
    def _download_image(download_url: str, config: DrawingApiConfig, endpoint: str) -> bytes:
        target = urlsplit(download_url)
        source = urlsplit(endpoint)
        headers = {
            "Accept": "image/*",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Referer": f"{source.scheme}://{source.netloc}/",
        }
        attempts: List[Dict[str, str]] = []
        if (target.scheme, target.netloc) == (source.scheme, source.netloc):
            authorized = dict(headers)
            authorized["Authorization"] = f"Bearer {config.api_key}"
            attempts.extend((authorized, headers))
        else:
            attempts.append(headers)
        last_error: Optional[BaseException] = None
        for attempt, request_headers in enumerate(attempts):
            request = Request(download_url, headers=request_headers)
            try:
                with urlopen(request, timeout=config.timeout_seconds) as response:
                    return response.read()
            except HTTPError as error:
                last_error = error
                if error.code != 403 or attempt == len(attempts) - 1:
                    error.close()
                    break
                error.close()
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                break
        host = target.hostname or target.netloc or "未知主机"
        raise DrawingApiError(f"下载绘图结果失败: {last_error or '未知错误'}（{host}）") from last_error

    @staticmethod
    def _decode_data_uri(value: str) -> bytes:
        try:
            return base64.b64decode(value.split(",", 1)[-1], validate=True)
        except (ValueError, TypeError) as error:
            raise DrawingApiError("绘图 API 返回的 data URI 无效") from error
