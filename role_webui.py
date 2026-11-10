"""角色库 WebUI 的图片压缩、缓存与页面数据组装。"""

from pathlib import Path
from typing import Any, Dict, Optional

import base64
import hashlib
import io
import threading

from PIL import Image, ImageOps, UnidentifiedImageError

from .role_store import RoleStore, RoleSummary


THUMBNAIL_EDGE = 160
PREVIEW_EDGE = 1280
MAX_THUMBNAIL_BYTES = 12 * 1024
MAX_PREVIEW_BYTES = 300 * 1024
CACHE_DIR_NAME = "webui-images"


class ImageRenderError(ValueError):
    """角色图片无法解码或压缩到 WebUI 体积预算内。"""


def _open_rgba(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            return ImageOps.exif_transpose(image).convert("RGBA")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as error:
        raise ImageRenderError("角色图片无法解码") from error


def _encode_webp(image: Image.Image, edge: int, quality: int) -> bytes:
    resized = image.copy()
    resized.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    stream = io.BytesIO()
    resized.save(stream, format="WEBP", quality=quality, method=4)
    return stream.getvalue()


def _render_limited(image: Image.Image, edge: int, max_bytes: int) -> bytes:
    """逐级降低尺寸与质量，直到结果符合页面返回预算。"""
    data = b""
    for current_edge in (edge, max(96, edge * 3 // 4), max(64, edge // 2), 64):
        for quality in (78, 68, 58, 48):
            data = _encode_webp(image, current_edge, quality)
            if len(data) <= max_bytes:
                return data
    raise ImageRenderError("图片压缩后仍然超过 WebUI 体积限制")


def render_thumbnail(data: bytes) -> bytes:
    """生成列表卡片使用的方形预览。"""
    image = _open_rgba(data)
    try:
        return _render_limited(image, THUMBNAIL_EDGE, MAX_THUMBNAIL_BYTES)
    finally:
        image.close()


def render_preview(data: bytes) -> bytes:
    """生成详情页使用的大图预览，不返回原始分辨率。"""
    image = _open_rgba(data)
    try:
        return _render_limited(image, PREVIEW_EDGE, MAX_PREVIEW_BYTES)
    finally:
        image.close()


def data_url(data: bytes) -> str:
    return "data:image/webp;base64," + base64.b64encode(data).decode("ascii")


def _render_cached(cache_dir: Path, data: bytes, kind: str) -> bytes:
    digest = hashlib.sha256(data).hexdigest()
    cache_path = cache_dir / f"{kind}-{digest}.webp"
    if cache_path.is_file():
        return cache_path.read_bytes()
    rendered = render_thumbnail(data) if kind == "thumb" else render_preview(data)
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f"{cache_path.name}.{threading.get_ident()}.tmp")
    try:
        temporary.write_bytes(rendered)
        temporary.replace(cache_path)
    finally:
        temporary.unlink(missing_ok=True)
    return rendered


def _summary_payload(summary: RoleSummary) -> Dict[str, Any]:
    return {
        "name": summary.name,
        "owner": summary.owner_id,
        "image_bytes": summary.image_bytes,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
    }


def list_payload(store: RoleStore, scope_id: str, query: str, page: int, page_size: int, sort: str) -> Dict[str, Any]:
    """组装角色列表、分页和统计；列表不带图片，缩略图由页面按需查询。"""
    total = store.count_roles(scope_id, query)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 1), pages)
    summaries = store.list_roles(scope_id, query, page_size, (page - 1) * page_size, sort)
    stats = store.stats(scope_id)
    return {
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "stats": {
            "roles": stats.role_count,
            "image_bytes": stats.image_bytes,
            "database_bytes": stats.database_bytes,
        },
        "items": [_summary_payload(summary) for summary in summaries],
    }


def detail_payload(store: RoleStore, scope_id: str, name: str, cache_dir: Path) -> Dict[str, Any]:
    """组装角色详情与唯一图片的压缩预览。"""
    summary = store.get_role(scope_id, name)
    if summary is None:
        raise ValueError("角色不存在")
    payload = _summary_payload(summary)
    image = store.get(scope_id, name)
    preview: Optional[str] = None
    preview_error = ""
    if image is None:
        preview_error = "角色图片缺失"
    else:
        try:
            preview = data_url(_render_cached(cache_dir, image, "preview"))
        except ImageRenderError as error:
            preview_error = str(error)
    payload.update({"preview": preview, "preview_error": preview_error})
    return payload


def thumbnail_payload(store: RoleStore, scope_id: str, name: str, cache_dir: Path) -> Dict[str, Any]:
    """按需生成单个角色缩略图；图片损坏时返回可读错误而不是伪造图片。"""
    image = store.get(scope_id, name)
    if image is None:
        raise ValueError("角色不存在")
    try:
        return {"image": data_url(_render_cached(cache_dir, image, "thumb"))}
    except ImageRenderError as error:
        return {"image": None, "error": str(error)}
