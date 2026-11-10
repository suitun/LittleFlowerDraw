"""小花绘：角色参考图库与聊天入口。"""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncio
import base64

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from .role_messages import ImageInputError, ImageLimits, MessageImageResolver, MissingImageError, get_message_text
from .role_patterns import RoleRequest, match_request, render_reply, valid_role_name
from .role_store import RoleStore
from .settings import LittleFlowerDrawConfig


GLOBAL_LIBRARY_SCOPE = "global"


@dataclass(frozen=True)
class LibraryJob:
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
        self._queue: asyncio.Queue[LibraryJob] = asyncio.Queue(maxsize=8)
        self._worker: Optional[asyncio.Task[None]] = None
        self._pending_writes: Set[asyncio.Task[bool]] = set()
        self._seen_messages: OrderedDict[Tuple[str, ...], None] = OrderedDict()

    async def on_load(self) -> None:
        self._store = await asyncio.to_thread(RoleStore, self.ctx.paths.data_dir)
        self._worker = asyncio.create_task(self._run_jobs(), name="little-flower-draw.role-library")
        self.ctx.logger.info("小花绘角色库已加载")

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
        job = LibraryJob(request, message, config)
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
                stream_id = await self._open_chat(job.message)
                await asyncio.wait_for(self._handle_job(job, stream_id), timeout=90)
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

    async def _handle_job(self, job: LibraryJob, stream_id: str) -> None:
        config = job.config
        role_name = job.request.role_name
        if not valid_role_name(role_name, config.library.max_name_length):
            await self._send_reply(config.replies.invalid_name, stream_id, role_name)
            return
        store = self._store
        if store is None:
            raise RuntimeError("角色库尚未初始化")

        if job.request.action == "register":
            limits = ImageLimits(
                max_images=config.library.max_images,
                max_image_bytes=config.library.max_image_mb * 1024**2,
                max_total_bytes=config.library.max_total_mb * 1024**2,
                max_pixels=config.library.max_pixels,
            )
            try:
                images = await MessageImageResolver(self.ctx, limits).resolve(job.message, stream_id)
            except MissingImageError:
                await self._send_reply(config.replies.missing_image, stream_id, role_name)
                return
            except ImageInputError:
                self.ctx.logger.warning("小花绘登记图片未通过校验", exc_info=True)
                await self._send_reply(config.replies.invalid_image, stream_id, role_name)
                return
            owner_id = str(job.message["message_info"]["user_info"]["user_id"])
            overwritten = await self._save_role(store, role_name, owner_id, images)
            template = config.replies.overwritten if overwritten else config.replies.registered
            await self._send_reply(template, stream_id, role_name, len(images))
            return

        images = await asyncio.to_thread(store.get, GLOBAL_LIBRARY_SCOPE, role_name)
        if images is None:
            await self._send_reply(config.replies.not_found, stream_id, role_name)
            return
        await self._send_reply(config.replies.view, stream_id, role_name, len(images))
        for image in images:
            if not await self.ctx.send.image(base64.b64encode(image).decode("ascii"), stream_id):
                raise ReplyDeliveryError("角色图片发送失败")

    async def _save_role(self, store: RoleStore, role_name: str, owner_id: str, images: List[bytes]) -> bool:
        # 线程中的 SQLite 写入无法通过取消协程停止；卸载前必须等待事务收尾。
        write = asyncio.create_task(asyncio.to_thread(store.save, GLOBAL_LIBRARY_SCOPE, role_name, owner_id, images))
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

    async def _send_reply(self, template: str, stream_id: str, role_name: str, image_count: int = 0) -> None:
        text = render_reply(template, role_name, image_count)
        if text and not await self.ctx.send.text(text, stream_id):
            raise ReplyDeliveryError("角色库文字回复发送失败")

    @Command(
        "little_flower_draw_status",
        description="查看小花绘状态与当前角色库触发词",
        pattern=r"^/(?:小花绘|小花画图|lfdraw)\s*$",
    )
    async def show_status(self, stream_id: str, **kwargs: Any) -> Tuple[bool, str, int]:
        message = (
            "小花绘 0.1 · 让小花帮你画\n角色参考图库已就绪喵～\n登记/覆盖："
            + " / ".join(self.config.keywords.register_templates)
            + "\n查看："
            + " / ".join(self.config.keywords.view_templates)
        )
        if not await self.ctx.send.text(message, stream_id):
            return False, "小花绘状态消息发送失败，请检查 MaiBot 日志。", 2
        return True, message, 2


def create_plugin() -> LittleFlowerDrawPlugin:
    return LittleFlowerDrawPlugin()
