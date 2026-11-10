"""小花绘：角色参考图库与聊天入口。"""

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import asyncio
import base64
import json
import unicodedata

from maibot_sdk import API, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from .command_guard import CommandGuard
from .command_registry import ACTION_COMMANDS
from .command_usage import CommandUsageStore
from .role_messages import (
    ImageInputError,
    ImageLimits,
    MessageImageResolver,
    MissingImageError,
    get_message_text,
    validate_image,
)
from .role_patterns import RoleRequest, match_request, render_reply, valid_role_name
from .role_store import RoleStore
from .role_webui import CACHE_DIR_NAME, detail_payload, list_payload, thumbnail_payload
from .settings import LittleFlowerDrawConfig
from .webui_settings import WebUISettings, WebUISettingsStore, load_legacy_settings


GLOBAL_LIBRARY_SCOPE = "global"


@dataclass(frozen=True)
class LibraryJob:
    command_id: str
    request: RoleRequest
    message: Dict[str, Any]
    config: LittleFlowerDrawConfig


class ReplyDeliveryError(RuntimeError):
    """业务结果未能发送到聊天平台。"""


class LittleFlowerDrawPlugin(MaiBotPlugin):
    """所有群和用户共享的角色图片库。"""

    config_model = LittleFlowerDrawConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[RoleStore] = None
        self._usage_store: Optional[CommandUsageStore] = None
        self._settings_store: Optional[WebUISettingsStore] = None
        self._guard: Optional[CommandGuard] = None
        self._queue: asyncio.Queue[LibraryJob] = asyncio.Queue(maxsize=8)
        self._worker: Optional[asyncio.Task[None]] = None
        self._pending_writes: Set[asyncio.Task[bool]] = set()
        self._seen_messages: OrderedDict[Tuple[str, ...], None] = OrderedDict()

    async def on_load(self) -> None:
        self._store = await asyncio.to_thread(RoleStore, self.ctx.paths.data_dir)
        self._usage_store = await asyncio.to_thread(CommandUsageStore, self.ctx.paths.data_dir)
        self._settings_store = await asyncio.to_thread(WebUISettingsStore, self.ctx.paths.data_dir)
        legacy = await asyncio.to_thread(load_legacy_settings, Path(__file__).with_name("config.toml"))
        await asyncio.to_thread(self._settings_store.load, legacy)
        self._guard = CommandGuard(self._usage_store, self._settings_store)
        self._worker = asyncio.create_task(self._run_jobs(), name="little-flower-draw.role-library")
        self.ctx.logger.info("小花绘角色库与权限守卫已加载")

    async def on_unload(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        await self._finish_pending_writes()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._seen_messages.clear()
        self._guard = None
        self._usage_store = None
        self._settings_store = None
        self.ctx.logger.info("小花绘角色库已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("小花绘角色库配置已更新，后续请求立即使用新触发词与回复文案：%s", version)

    @HookHandler(
        "chat.receive.before_process",
        name="role_library_receive",
        description="识别角色入库、覆盖和查看消息，保留入站图片并交给后台处理",
        mode=HookMode.BLOCKING,
        timeout_ms=10000,
    )
    async def receive_role_message(self, **kwargs: Any) -> Dict[str, Any]:
        message = kwargs.get("message")
        if not isinstance(message, dict) or message.get("platform") != "qq":
            return {"action": "continue"}
        info = message.get("message_info") or {}
        if not isinstance(info, dict):
            return {"action": "continue"}
        user = info.get("user_info") or {}
        group = info.get("group_info") or {}
        if not isinstance(user, dict) or not isinstance(group, dict) or not user.get("user_id"):
            return {"action": "continue"}
        account_id = str(message.get("account_id") or "")
        user_id = str(user["user_id"])
        if account_id and user_id == account_id:
            return {"action": "continue"}
        config = self.config.model_copy(deep=True)
        request = match_request(get_message_text(message), config.keywords.register_templates, config.keywords.view_templates)
        if request is None:
            return {"action": "continue"}

        message_id = str(message.get("message_id") or "")
        group_id = str(group.get("group_id") or "")
        key = (
            "qq", account_id, str(message.get("scope") or ""),
            "group" if group_id else "private", group_id or user_id, message_id,
        )
        if message_id and key in self._seen_messages:
            return {"action": "abort", "custom_result": {"handled_by": "小花绘角色库"}}
        command_id = ACTION_COMMANDS[request.action]
        guard = self._guard
        if guard is None:
            raise RuntimeError("小花绘权限守卫尚未初始化")
        decision = guard.check_access(command_id, user_id, group_id)
        if not decision.allowed:
            try:
                await self._send_guard_reply(message, decision.reply)
            except Exception:
                self.ctx.logger.exception("小花绘权限提示未能送达")
            return {"action": "abort", "custom_result": {"handled_by": "小花绘角色库"}}
        job = LibraryJob(command_id, request, message, config)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            try:
                await asyncio.wait_for(self._send_busy(job), timeout=2)
            except Exception:
                self.ctx.logger.exception("小花绘图库队列已满，忙碌提示未能送达")
        else:
            if message_id:
                self._seen_messages[key] = None
                while len(self._seen_messages) > 500:
                    self._seen_messages.popitem(last=False)
        # 已识别的图库请求由插件完整处理，不再交给主聊天模型生成额外回复。
        return {"action": "abort", "custom_result": {"handled_by": "小花绘角色库"}}

    async def _send_busy(self, job: LibraryJob) -> None:
        stream_id = await self._open_chat(job.message)
        await self._send_reply(job.config.replies.busy, stream_id, job.request.role_name)

    async def _send_guard_reply(self, message: Dict[str, Any], text: str) -> None:
        if not text:
            return
        stream_id = await self._open_chat(message)
        await self._send_text(text, stream_id)

    async def _send_text(self, text: str, stream_id: str) -> None:
        if text and not await self.ctx.send.text(text, stream_id):
            raise ReplyDeliveryError("角色库文字回复发送失败")

    async def _open_chat(self, message: Dict[str, Any]) -> str:
        info = message["message_info"]
        user = info["user_info"]
        group = info.get("group_info") or {}
        result = await self.ctx.chat.open_session(
            platform=message["platform"],
            chat_type="group" if group.get("group_id") else "private",
            user_id=str(user["user_id"]),
            group_id=str(group.get("group_id") or ""),
            account_id=str(message.get("account_id") or ""),
            scope=str(message.get("scope") or ""),
            user_nickname=str(user.get("user_nickname") or ""),
            group_name=str(group.get("group_name") or ""),
        )
        if not isinstance(result, dict) or not result.get("success") or not result.get("session_id"):
            raise RuntimeError("无法打开角色库请求对应的聊天流")
        return str(result["session_id"])

    async def _run_jobs(self) -> None:
        while True:
            job = await self._queue.get()
            stream_id = ""
            try:
                user_id = str(job.message["message_info"]["user_info"]["user_id"])
                stream_id = await self._open_chat(job.message)
                guard = self._guard
                if guard is None:
                    raise RuntimeError("小花绘权限守卫尚未初始化")
                denied = await asyncio.to_thread(guard.check_quota, job.command_id, user_id)
                if denied:
                    await self._send_text(denied, stream_id)
                    continue
                succeeded = await asyncio.wait_for(self._handle_job(job, stream_id), timeout=90)
                if succeeded:
                    try:
                        await asyncio.to_thread(guard.record_success, job.command_id, user_id)
                    except Exception:
                        self.ctx.logger.exception("小花绘次数记录失败：指令=%s", job.command_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("小花绘角色库请求处理失败：操作=%s", job.request.action)
                # 超时不会终止线程事务；下一条请求开始前也要等旧写入结束。
                await self._finish_pending_writes()
                if stream_id:
                    try:
                        await self._send_reply(job.config.replies.error, stream_id, job.request.role_name)
                    except Exception:
                        self.ctx.logger.exception("小花绘角色库错误提示也未能发送")
            finally:
                self._queue.task_done()

    async def _handle_job(self, job: LibraryJob, stream_id: str) -> bool:
        """执行一次角色库请求，返回是否真正成功；失败或未找到不消耗次数。"""
        config = job.config
        role_name = job.request.role_name
        if not valid_role_name(role_name, config.library.max_name_length):
            await self._send_reply(config.replies.invalid_name, stream_id, role_name)
            return False
        store = self._store
        if store is None:
            raise RuntimeError("角色库尚未初始化")

        if job.request.action == "register":
            limits = ImageLimits(
                max_image_bytes=config.library.max_image_mb * 1024**2,
                max_pixels=config.library.max_pixels,
            )
            try:
                image = await MessageImageResolver(self.ctx, limits).resolve(job.message, stream_id)
            except MissingImageError:
                await self._send_reply(config.replies.missing_image, stream_id, role_name)
                return False
            except ImageInputError:
                self.ctx.logger.warning("小花绘登记图片未通过校验", exc_info=True)
                await self._send_reply(config.replies.invalid_image, stream_id, role_name)
                return False
            owner_id = str(job.message["message_info"]["user_info"]["user_id"])
            overwritten = await self._save_role(store, role_name, owner_id, image)
            template = config.replies.overwritten if overwritten else config.replies.registered
            await self._send_reply(template, stream_id, role_name)
            return True

        image = await asyncio.to_thread(store.get, GLOBAL_LIBRARY_SCOPE, role_name)
        if image is None:
            await self._send_reply(config.replies.not_found, stream_id, role_name)
            return False
        await self._send_reply(config.replies.view, stream_id, role_name)
        if not await self.ctx.send.image(base64.b64encode(image).decode("ascii"), stream_id):
            raise ReplyDeliveryError("角色图片发送失败")
        return True

    async def _save_role(self, store: RoleStore, role_name: str, owner_id: str, image: bytes) -> bool:
        # 线程中的 SQLite 写入无法通过取消协程停止；卸载前必须等待事务收尾。
        write = asyncio.create_task(asyncio.to_thread(store.save, GLOBAL_LIBRARY_SCOPE, role_name, owner_id, image))
        self._pending_writes.add(write)
        try:
            return await asyncio.shield(write)
        finally:
            if write.done():
                self._pending_writes.discard(write)

    async def _finish_pending_writes(self) -> None:
        if self._pending_writes:
            results = await asyncio.gather(*self._pending_writes, return_exceptions=True)
            self._pending_writes.clear()
            for result in results:
                if isinstance(result, BaseException):
                    self.ctx.logger.error("小花绘等待中的图库事务写入失败：%s", result)

    async def _send_reply(self, template: str, stream_id: str, role_name: str) -> None:
        await self._send_text(render_reply(template, role_name), stream_id)

    def _require_store(self) -> RoleStore:
        if self._store is None:
            raise RuntimeError("角色库尚未初始化")
        return self._store

    def _require_settings_store(self) -> WebUISettingsStore:
        if self._settings_store is None:
            raise RuntimeError("WebUI 设置尚未初始化")
        return self._settings_store

    @property
    def _webui_cache_dir(self) -> Path:
        return self.ctx.paths.runtime_dir / CACHE_DIR_NAME

    @API("webui_settings", description="读取 WebUI 管理的权限、白名单和提示文案", version="1")
    async def webui_settings(self) -> Dict[str, Any]:
        settings = await asyncio.to_thread(self._require_settings_store().current)
        return settings.model_dump(mode="json")

    @API("webui_settings_save", description="保存 WebUI 管理的权限、白名单和提示文案", version="1")
    async def webui_settings_save(self, settings_json: str) -> Dict[str, Any]:
        try:
            raw = json.loads(settings_json)
        except json.JSONDecodeError:
            raise ValueError("设置数据不是有效的 JSON") from None
        if not isinstance(raw, dict) or not {"access", "commands", "guard_replies"} <= raw.keys():
            raise ValueError("设置数据缺少必要分组")
        settings = WebUISettings.model_validate(raw)
        await asyncio.to_thread(self._require_settings_store().save, settings)
        return {"saved": True}

    @API("webui_roles", description="分页读取角色库列表与统计", version="1")
    async def webui_roles(
        self, query: str = "", page: int = 1, page_size: int = 20, sort: str = "name"
    ) -> Dict[str, Any]:
        payload = await asyncio.to_thread(
            list_payload, self._require_store(), GLOBAL_LIBRARY_SCOPE, query, page, page_size, sort
        )
        payload["register_template"] = self.config.keywords.register_templates[0]
        return payload

    @API("webui_role_detail", description="读取角色详情与压缩预览", version="1")
    async def webui_role_detail(self, name: str) -> Dict[str, Any]:
        return await asyncio.to_thread(
            detail_payload, self._require_store(), GLOBAL_LIBRARY_SCOPE, name, self._webui_cache_dir
        )

    @API("webui_role_thumbnail", description="按需读取角色列表缩略图", version="1")
    async def webui_role_thumbnail(self, name: str) -> Dict[str, Any]:
        return await asyncio.to_thread(
            thumbnail_payload, self._require_store(), GLOBAL_LIBRARY_SCOPE, name, self._webui_cache_dir
        )

    @API("webui_role_delete", description="删除角色及其唯一图片", version="1")
    async def webui_role_delete(self, name: str) -> Dict[str, Any]:
        deleted = await asyncio.to_thread(self._require_store().delete, GLOBAL_LIBRARY_SCOPE, name)
        if not deleted:
            raise ValueError("角色不存在")
        return {"deleted": True}

    @API("webui_role_rename", description="重命名角色并保留创建者和图片", version="1")
    async def webui_role_rename(self, old_name: str, new_name: str) -> Dict[str, Any]:
        config = self.config.model_copy(deep=True)
        new_name = unicodedata.normalize("NFC", new_name.strip())
        if not valid_role_name(new_name, config.library.max_name_length):
            raise ValueError("新角色名不合法")
        renamed = await asyncio.to_thread(self._require_store().rename, GLOBAL_LIBRARY_SCOPE, old_name, new_name)
        if not renamed:
            raise ValueError("角色不存在")
        return {"renamed": old_name != new_name}

    @API("webui_role_set_image", description="用 WebUI 上传的图片新建或替换角色", version="1")
    async def webui_role_set_image(self, upload_id: str, name: str) -> Dict[str, Any]:
        config = self.config.model_copy(deep=True)
        name = unicodedata.normalize("NFC", name.strip())
        if not valid_role_name(name, config.library.max_name_length):
            raise ValueError("角色名不合法")
        uploads_dir = (self.ctx.paths.data_dir / "uploads").resolve()
        upload = await self.ctx.webui.claim_upload(upload_id)
        upload_path = Path(str(upload.get("path") or "")).resolve()
        if not upload_path.is_relative_to(uploads_dir) or not upload_path.is_file():
            raise RuntimeError("WebUI 上传文件不在插件上传目录内")
        try:
            image = await asyncio.to_thread(upload_path.read_bytes)
            limits = ImageLimits(
                max_image_bytes=config.library.max_image_mb * 1024**2,
                max_pixels=config.library.max_pixels,
            )
            await asyncio.to_thread(validate_image, image, limits)
            overwritten = await asyncio.to_thread(
                self._require_store().save, GLOBAL_LIBRARY_SCOPE, name, "webui", image
            )
        finally:
            await asyncio.to_thread(upload_path.unlink, missing_ok=True)
        return {"saved": True, "overwritten": overwritten}


def create_plugin() -> LittleFlowerDrawPlugin:
    return LittleFlowerDrawPlugin()
