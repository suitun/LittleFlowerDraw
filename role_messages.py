"""从聊天消息或一层引用中读取角色图片。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, url2pathname

import asyncio
import base64
import binascii
import io
import re
import time

from PIL import Image, UnidentifiedImageError


_GET_MESSAGE_API = "adapter.snowluma.message.get_msg"
_GET_IMAGE_API = "adapter.snowluma.file.get_image"
_API_TIMEOUT_SECONDS = 8.0
_NETWORK_TIMEOUT_SECONDS = 5.0
_DOWNLOAD_TIMEOUT_SECONDS = 12.0
_MAX_REDIRECTS = 3
_ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
_ALLOWED_CDN_SUFFIXES = ("qpic.cn", "qq.com")
_ALLOWED_CDN_HOSTS = {"multimedia.nt.qq.com.cn"}


class ImageInputError(ValueError):
    """图片不可读取或不符合角色库限制。"""


class MissingImageError(ImageInputError):
    """消息及其引用中没有可用图片。"""


class _ImageFetchError(ImageInputError):
    """已确定的图片来源暂时不可读取。"""


class ImageTooLargeError(ImageInputError):
    """图片文件超过体积上限。"""

    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(_too_large_message(actual_bytes, max_bytes))


class UnsupportedImageFormatError(ImageInputError):
    """图片格式不在允许列表中。"""

    def __init__(self, image_format: str) -> None:
        self.image_format = image_format
        super().__init__(f"这张图片是 {image_format} 格式，小花只记得住 PNG、JPEG、WebP 和 GIF 喵～")


class CorruptImageError(ImageInputError):
    """图片无法解码或损坏。"""


@dataclass(frozen=True)
class ImageLimits:
    """单张角色图片的体积上限。"""

    max_image_bytes: int = 8 * 1024**2

    def __post_init__(self) -> None:
        if self.max_image_bytes <= 0:
            raise ValueError("图片体积限制必须大于零")


def _too_large_message(actual_bytes: Optional[int], max_bytes: int) -> str:
    limit_mb = max_bytes / 1024**2
    if actual_bytes is None:
        return f"这张图片超过小花能记住的 {limit_mb:.1f}MB 啦，压缩一下再发喵～"
    actual_mb = actual_bytes / 1024**2
    return f"这张图片有 {actual_mb:.1f}MB，超过小花能记住的 {limit_mb:.1f}MB 啦，压缩一下再发喵～"


def get_message_text(message: Dict[str, Any]) -> str:
    """只拼接顶层文字消息段，不将引用预览当成用户输入。"""
    texts: List[str] = []
    for segment in _message_segments(message):
        if segment.get("type") != "text":
            continue
        data = segment.get("data")
        text = data.get("text") if isinstance(data, dict) else data
        if isinstance(text, str):
            texts.append(text)
    return "".join(texts)


def _message_segments(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    segments = message.get("raw_message")
    if not isinstance(segments, list):
        segments = message.get("message")
        if isinstance(segments, str):
            return _onebot_string_segments(segments)
    if not isinstance(segments, list):
        return []
    return [segment for segment in segments if isinstance(segment, dict)]


def _unescape_cq(value: str) -> str:
    return value.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def _onebot_string_segments(message: str) -> List[Dict[str, Any]]:
    """兼容 get_msg 返回的 OneBot CQ 字符串，不解析 Host 文字段中的 CQ 字样。"""
    result: List[Dict[str, Any]] = []
    end = 0
    for match in re.finditer(r"\[CQ:([A-Za-z0-9_]+)((?:,[^\]]*)?)\]", message):
        if match.start() > end:
            result.append({"type": "text", "data": _unescape_cq(message[end:match.start()])})
        values: Dict[str, str] = {}
        for field in match.group(2).split(",")[1:]:
            key, separator, value = field.partition("=")
            if separator:
                values[key] = _unescape_cq(value)
        result.append({"type": match.group(1).lower(), "data": values})
        end = match.end()
    if end < len(message):
        result.append({"type": "text", "data": _unescape_cq(message[end:])})
    return result


def _image_segments(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [segment for segment in _message_segments(message) if segment.get("type") in {"image", "emoji"}]


def _reply_message_id(message: Dict[str, Any]) -> str:
    for segment in _message_segments(message):
        if segment.get("type") != "reply":
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            message_id = str(data.get("target_message_id") or data.get("id") or "").strip()
            if message_id and message_id != "0":
                return message_id
    reply_to = str(message.get("reply_to") or "").strip()
    return reply_to if reply_to != "0" else ""


def _decode_base64(value: str, max_bytes: int) -> bytes:
    if value.startswith("base64://"):
        value = value[len("base64://"):]
    if len(value) > ((max_bytes + 2) // 3) * 4:
        raise ImageTooLargeError(max_bytes + 1, max_bytes)
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ImageInputError("图片数据不完整，小花看不清，请重新发送图片喵") from None
    if not data:
        raise MissingImageError("这张图片没有内容，请重新发送图片喵")
    if len(data) > max_bytes:
        raise ImageTooLargeError(len(data), max_bytes)
    return data


def validate_image(data: bytes, limits: ImageLimits) -> None:
    if len(data) > limits.max_image_bytes:
        raise ImageTooLargeError(len(data), limits.max_image_bytes)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in _ALLOWED_FORMATS:
                format_name = str(image.format or "未知")
                raise UnsupportedImageFormatError(format_name)
            if image.width <= 0 or image.height <= 0:
                raise ImageInputError("这张图片尺寸怪怪的，小花读不懂，请换一张喵～")
            image.verify()
        # verify 不解码像素；再读首帧，避免将截断图片写入图库。
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except Image.DecompressionBombError:
        raise ImageInputError("这张图片尺寸太大啦，小花的内存装不下，请换一张小一点的喵～") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        if isinstance(error, ImageInputError):
            raise
        raise CorruptImageError("图片好像损坏了，小花看不清，请换一张图片喵") from None


def _validate_image_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        valid_host = host in _ALLOWED_CDN_HOSTS or any(
            host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_CDN_SUFFIXES
        )
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or port not in {80, 443}
            or not valid_host
            or any(ord(character) <= 32 or ord(character) == 127 for character in url)
        ):
            raise ValueError
    except ValueError:
        raise ImageInputError("图片地址不是受支持的 QQ 图片地址，请直接附图喵") from None
    return url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Any, response: Any, code: int, message: str, headers: Any, url: str) -> None:
        return None


def _download_image(url: str, max_bytes: int) -> bytes:
    """有界下载 QQ CDN 图片，并在每次跳转前重新检查地址。"""
    current_url = _validate_image_url(url)
    opener = build_opener(_NoRedirect())
    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT_SECONDS
    for redirect_count in range(_MAX_REDIRECTS + 1):
        _validate_image_url(current_url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ImageFetchError("图片下载超时了，请重新发送图片喵")
        request = Request(current_url, headers={"User-Agent": "LittleFlowerDraw/0.2", "Accept-Encoding": "identity"})
        try:
            with opener.open(request, timeout=min(_NETWORK_TIMEOUT_SECONDS, remaining)) as response:
                declared_length: Optional[int] = None
                header_length = response.headers.get("Content-Length")
                if header_length is not None:
                    try:
                        declared_length = int(header_length)
                    except (TypeError, ValueError):
                        raise ImageInputError("图片下载返回了无效长度，请重新发送图片喵") from None
                    if declared_length < 0:
                        raise ImageInputError("图片下载返回了无效长度，请重新发送图片喵")
                    if declared_length > max_bytes:
                        raise ImageTooLargeError(declared_length, max_bytes)
                result = bytearray()
                while True:
                    if time.monotonic() > deadline:
                        raise _ImageFetchError("图片下载超时了，请重新发送图片喵")
                    chunk = response.read(min(64 * 1024, max_bytes + 1 - len(result)))
                    if not chunk:
                        break
                    result.extend(chunk)
                    if len(result) > max_bytes:
                        raise ImageTooLargeError(len(result), max_bytes)
                if declared_length is not None and len(result) != declared_length:
                    raise _ImageFetchError("图片没有下载完整，请重新发送图片喵")
                if not result:
                    raise MissingImageError("这张图片没有内容，请重新发送图片喵")
                return bytes(result)
        except HTTPError as error:
            status = error.code
            location = error.headers.get("Location") if error.headers is not None else None
            error.close()
            if status in {301, 302, 303, 307, 308} and location and redirect_count < _MAX_REDIRECTS:
                current_url = _validate_image_url(urljoin(current_url, location))
                continue
            # 不把可能带临时凭据的完整 URL 放进异常或日志。
            raise _ImageFetchError("引用图片暂时下载不了，请重新发送图片喵") from None
        except (URLError, OSError, TimeoutError, UnicodeError):
            raise _ImageFetchError("引用图片暂时下载不了，请重新发送图片喵") from None
    raise _ImageFetchError("图片地址跳转太多了，请重新发送图片喵")


def _read_local_image(reference: str, max_bytes: int) -> bytes:
    """只用于读取已通过公开 get_image API 返回的绝对本地路径。"""
    if reference.startswith("file://"):
        parsed = urlsplit(reference)
        if parsed.netloc not in {"", "localhost"}:
            raise ImageInputError("QQ 客户端返回的本地图片路径无效，请直接附图喵")
        reference = url2pathname(unquote(parsed.path))
    path = Path(reference)
    if not path.is_absolute() or reference.startswith(("\\\\", "//")):
        raise ImageInputError("QQ 客户端返回的本地图片路径无效，请直接附图喵")
    try:
        if path.stat().st_size > max_bytes:
            raise ImageTooLargeError(path.stat().st_size, max_bytes)
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        raise _ImageFetchError("QQ 客户端的图片文件暂时读不到，请直接附图喵") from None
    if len(data) > max_bytes:
        raise ImageTooLargeError(len(data), max_bytes)
    if not data:
        raise MissingImageError("这张图片没有内容，请重新发送图片喵")
    return data


class MessageImageResolver:
    """读取当前附图，或从宿主历史和 SnowLuma 公开 API 解析一层引用。"""

    def __init__(self, ctx: Any, limits: ImageLimits) -> None:
        self.ctx = ctx
        self.limits = limits

    async def resolve(self, message: Dict[str, Any], stream_id: str) -> bytes:
        """读取角色唯一图片；附图有内容时只取第一张图片。"""
        attached = _image_segments(message)
        if attached:
            return await self._resolve_image(attached[0])
        target_id = _reply_message_id(message)
        if not target_id:
            raise MissingImageError("小花还没看到图片呢，请附上一张图片或引用图片消息喵")
        try:
            historical = await asyncio.wait_for(
                self.ctx.message.get_by_id(target_id, stream_id=stream_id, include_binary_data=True),
                timeout=_API_TIMEOUT_SECONDS,
            )
        except Exception:
            raise ImageInputError("小花读取引用消息失败了，请稍后再试喵") from None
        self._check_failure(historical)
        if isinstance(historical, dict):
            historical_stream = str(historical.get("session_id") or "")
            if historical_stream and historical_stream != stream_id:
                raise ImageInputError("引用的图片不属于当前聊天，请在这里重新发送喵")
            historical_images = _image_segments(historical)
            if historical_images:
                try:
                    return await self._resolve_image(historical_images[0])
                except MissingImageError:
                    # 历史记录中的媒体可能已被清理，再向连接器取这条消息的真实图片。
                    pass
        detail = await self._call_api(_GET_MESSAGE_API, message_id=target_id)
        if not isinstance(detail, dict):
            raise MissingImageError("那条引用消息已经读不到啦，请把图片重新发给小花喵")
        self._check_quoted_group(message, detail)
        images = _image_segments(detail)
        if not images:
            raise MissingImageError("引用的消息里没有图片，请附上一张图片喵")
        return await self._resolve_image(images[0])

    async def _resolve_image(self, segment: Dict[str, Any]) -> bytes:
        data = await self._load_segment(segment)
        if len(data) > self.limits.max_image_bytes:
            raise ImageTooLargeError(len(data), self.limits.max_image_bytes)
        await asyncio.to_thread(validate_image, data, self.limits)
        return data

    async def _load_segment(self, segment: Dict[str, Any]) -> bytes:
        encoded = segment.get("binary_data_base64")
        if isinstance(encoded, str) and encoded:
            return await asyncio.to_thread(_decode_base64, encoded, self.limits.max_image_bytes)
        details = segment.get("data")
        if not isinstance(details, dict):
            raise MissingImageError("这张图片的原始内容已经读不到啦，请重新发送图片喵")
        encoded = details.get("base64")
        if isinstance(encoded, str) and encoded:
            return await asyncio.to_thread(_decode_base64, encoded, self.limits.max_image_bytes)
        file_reference = str(details.get("file") or "").strip()
        if file_reference.startswith("base64://"):
            return await asyncio.to_thread(_decode_base64, file_reference, self.limits.max_image_bytes)
        url = str(details.get("url") or "").strip()
        if not url and file_reference.startswith(("http://", "https://")):
            url = file_reference
            file_reference = ""
        if url:
            _validate_image_url(url)
            try:
                return await asyncio.to_thread(_download_image, url, self.limits.max_image_bytes)
            except _ImageFetchError:
                if not file_reference:
                    raise
        if not file_reference or file_reference.startswith("file://") or Path(file_reference).is_absolute():
            raise MissingImageError("这张图片缺少可用内容，请直接把图片发给小花喵")
        response = await self._call_api(_GET_IMAGE_API, params={"file": file_reference})
        if not isinstance(response, dict):
            raise MissingImageError("QQ 客户端没有找到这张图片，请重新发送图片喵")
        if response.get("status", "ok") != "ok" or response.get("retcode", 0) not in {0, "0"}:
            raise ImageInputError("QQ 客户端暂时取不到这张图片，请重新发送图片喵")
        info = response.get("data")
        if not isinstance(info, dict):
            raise MissingImageError("QQ 客户端没有返回图片内容，请重新发送图片喵")
        return await self._load_image_info(info)

    async def _load_image_info(self, info: Dict[str, Any]) -> bytes:
        encoded = info.get("base64")
        if isinstance(encoded, str) and encoded:
            return await asyncio.to_thread(_decode_base64, encoded, self.limits.max_image_bytes)
        local_path = str(info.get("file") or "").strip()
        url = str(info.get("url") or "").strip()
        if local_path.startswith(("http://", "https://")):
            url = url or local_path
            local_path = ""
        if local_path and (Path(local_path).is_absolute() or local_path.startswith("file://")):
            try:
                return await asyncio.to_thread(_read_local_image, local_path, self.limits.max_image_bytes)
            except _ImageFetchError:
                if not url:
                    raise
        if url:
            return await asyncio.to_thread(_download_image, url, self.limits.max_image_bytes)
        raise MissingImageError("QQ 客户端没有提供可用图片，请直接附图喵")

    async def _call_api(self, api_name: str, **kwargs: Any) -> Any:
        try:
            metadata = await asyncio.wait_for(self.ctx.api.get(api_name, version="1"), timeout=_API_TIMEOUT_SECONDS)
            self._check_failure(metadata)
            if metadata is None:
                raise MissingImageError("请先启用 SnowLuma QQ 连接器，或直接把图片发给小花喵")
            result = await asyncio.wait_for(
                self.ctx.api.call(api_name, version="1", **kwargs), timeout=_API_TIMEOUT_SECONDS
            )
        except ImageInputError:
            raise
        except Exception:
            raise ImageInputError("QQ 连接器暂时读不到引用图片，请检查连接后再试喵") from None
        self._check_failure(result)
        return result

    @staticmethod
    def _check_failure(result: Any) -> None:
        if isinstance(result, dict) and result.get("success") is False:
            raise ImageInputError("读取图片失败了，请检查 QQ 连接器或重新发送图片喵")

    @staticmethod
    def _check_quoted_group(message: Dict[str, Any], detail: Dict[str, Any]) -> None:
        info = message.get("message_info")
        group = info.get("group_info") if isinstance(info, dict) else None
        group_id = str(group.get("group_id") or "") if isinstance(group, dict) else ""
        quoted_group_id = str(detail.get("group_id") or "")
        if quoted_group_id and quoted_group_id != group_id:
            raise ImageInputError("引用的图片不属于当前聊天，请在这里重新发送喵")
