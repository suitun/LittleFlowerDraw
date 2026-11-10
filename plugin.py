"""小花绘：角色参考图库与聊天入口。"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import asyncio
import base64
import io
import json
import unicodedata
import uuid

from PIL import Image, UnidentifiedImageError
from maibot_sdk import API, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from .command_guard import CommandGuard
from .command_registry import ACTION_COMMANDS, DRAWING_COMMANDS
from .command_usage import CommandUsageStore
from .drawing_api import (
    DrawingApiConfig,
    DrawingApiError,
    DrawingApiStore,
    DrawingResult,
    OpenAIImageClient,
    fetch_models,
)
from .drawing_patterns import DRAWING_KIND_LABELS, DrawingCandidate, match_drawing_request
from .drawing_tasks import (
    ActiveTaskError,
    DuplicateTaskError,
    DrawingTaskStore,
    QueueFullError,
    TaskRecord,
)
from .role_messages import (
    ImageInputError,
    ImageLimits,
    MessageImageResolver,
    MissingImageError,
    get_message_text,
    validate_image,
)
from .role_patterns import (
    RoleRequest,
    match_request,
    render_drawing_count_reply,
    render_drawing_error_reply,
    render_drawing_reply,
    render_reply,
    render_task_fail_reply,
    render_task_reply,
    valid_role_name,
)
from .role_store import RoleStore
from .role_webui import CACHE_DIR_NAME, data_url, detail_payload, list_payload, render_thumbnail, thumbnail_payload
from .settings import LittleFlowerDrawConfig
from .webui_settings import WebUISettings, WebUISettingsStore, load_legacy_settings


GLOBAL_LIBRARY_SCOPE = "global"


@dataclass(frozen=True)
class LibraryJob:
    command_id: str
    request: RoleRequest
    message: Dict[str, Any]
    settings: WebUISettings


@dataclass(frozen=True)
class DrawingSubmitJob:
    command_id: str
    candidate: DrawingCandidate
    message: Dict[str, Any]
    settings: WebUISettings


Job = Union[LibraryJob, DrawingSubmitJob]


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
        self._drawing_api_store: Optional[DrawingApiStore] = None
        self._task_store: Optional[DrawingTaskStore] = None
        self._guard: Optional[CommandGuard] = None
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=8)
        self._worker: Optional[asyncio.Task[None]] = None
        self._task_scheduler: Optional[asyncio.Task[None]] = None
        self._task_maintenance: Optional[asyncio.Task[None]] = None
        self._task_running: Set[asyncio.Task[None]] = set()
        self._task_wakeup = asyncio.Event()
        self._task_submit_lock = asyncio.Lock()
        self._pending_writes: Set[asyncio.Task[bool]] = set()
        self._seen_messages: OrderedDict[Tuple[str, ...], None] = OrderedDict()

    async def on_load(self) -> None:
        self._store = await asyncio.to_thread(RoleStore, self.ctx.paths.data_dir)
        self._usage_store = await asyncio.to_thread(CommandUsageStore, self.ctx.paths.data_dir)
        self._settings_store = await asyncio.to_thread(WebUISettingsStore, self.ctx.paths.data_dir)
        legacy = await asyncio.to_thread(load_legacy_settings, Path(__file__).with_name("config.toml"))
        await asyncio.to_thread(self._settings_store.load, legacy)
        self._drawing_api_store = DrawingApiStore(self.ctx.paths.data_dir)
        await asyncio.to_thread(self._drawing_api_store.load)
        self._guard = CommandGuard(self._usage_store, self._settings_store)
        self._task_store = await asyncio.to_thread(DrawingTaskStore, self.ctx.paths.data_dir)
        recovered = await asyncio.to_thread(self._task_store.recover_running)
        for task in recovered:
            await self._refund_task(task)
        self._worker = asyncio.create_task(self._run_jobs(), name="little-flower-draw.submission")
        self._task_scheduler = asyncio.create_task(
            self._run_task_scheduler(), name="little-flower-draw.task-queue"
        )
        self._task_maintenance = asyncio.create_task(
            self._run_task_maintenance(), name="little-flower-draw.task-maintenance"
        )
        self.ctx.logger.info("小花绘角色库、任务队列与权限守卫已加载")

    async def on_unload(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._task_scheduler is not None:
            self._task_scheduler.cancel()
            await asyncio.gather(self._task_scheduler, return_exceptions=True)
            self._task_scheduler = None
        if self._task_maintenance is not None:
            self._task_maintenance.cancel()
            await asyncio.gather(self._task_maintenance, return_exceptions=True)
            self._task_maintenance = None
        for task in list(self._task_running):
            task.cancel()
        if self._task_running:
            await asyncio.gather(*self._task_running, return_exceptions=True)
            self._task_running.clear()
        # 等待已经被取消但仍在线程池中收尾的 SQLite 操作结束，避免 Windows 上数据库文件被锁。
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, lambda: None), timeout=3
            )
        except asyncio.TimeoutError:
            pass
        await self._finish_pending_writes()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._seen_messages.clear()
        self._guard = None
        self._usage_store = None
        self._settings_store = None
        self._drawing_api_store = None
        self._task_store = None
        self.ctx.logger.info("小花绘角色库与任务队列已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("小花绘角色库配置已更新，后续请求立即使用新触发词与回复文案：%s", version)

    @HookHandler(
        "chat.receive.before_process",
        name="role_library_receive",
        description="识别角色库和绘图关键词，保留入站图片并交给后台处理",
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
        settings = self._settings_store.current() if self._settings_store is not None else None
        if settings is None:
            raise RuntimeError("WebUI 设置尚未初始化")
        text = get_message_text(message)
        request = match_request(text, settings.keywords.register_templates, settings.keywords.view_templates)
        drawing = None
        if request is None:
            drawing = match_drawing_request(
                text,
                settings.drawing.replace_templates,
                settings.drawing.together_templates,
                settings.drawing.group_templates,
            )
        if request is None and drawing is None:
            return {"action": "continue"}

        message_id = str(message.get("message_id") or "")
        group_id = str(group.get("group_id") or "")
        key = (
            "qq", account_id, str(message.get("scope") or ""),
            "group" if group_id else "private", group_id or user_id, message_id,
        )
        if message_id and key in self._seen_messages:
            return {"action": "abort", "custom_result": {"handled_by": "小花绘"}}
        command_id = ACTION_COMMANDS[request.action] if request is not None else DRAWING_COMMANDS[drawing.kind]
        guard = self._guard
        if guard is None:
            raise RuntimeError("小花绘权限守卫尚未初始化")
        decision = guard.check_access(command_id, user_id, group_id)
        if not decision.allowed:
            try:
                await self._send_guard_reply(message, decision.reply)
            except Exception:
                self.ctx.logger.exception("小花绘权限提示未能送达")
            return {"action": "abort", "custom_result": {"handled_by": "小花绘"}}
        job = (
            LibraryJob(command_id, request, message, settings)
            if request is not None
            else DrawingSubmitJob(command_id, drawing, message, settings)
        )
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
        return {"action": "abort", "custom_result": {"handled_by": "小花绘"}}

    async def _send_busy(self, job: Job) -> None:
        stream_id = await self._open_chat(job.message)
        await self._send_text(render_reply(job.settings.replies.busy, ""), stream_id)

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
                if isinstance(job, DrawingSubmitJob):
                    succeeded = await self._handle_drawing_submission(job, stream_id)
                else:
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
                self.ctx.logger.exception("小花绘请求处理失败：指令=%s", job.command_id)
                # 超时不会终止线程事务；下一条请求开始前也要等旧写入结束。
                await self._finish_pending_writes()
                if stream_id:
                    try:
                        if isinstance(job, DrawingSubmitJob):
                            await self._send_text(job.settings.replies.error, stream_id)
                        else:
                            await self._send_reply(job.settings.replies.error, stream_id, job.request.role_name)
                    except Exception:
                        self.ctx.logger.exception("小花绘错误提示也未能发送")
            finally:
                self._queue.task_done()

    @staticmethod
    def _split_role_pair(text: str, names: Set[str]) -> Optional[Tuple[str, str]]:
        for index in range(len(text) - 1, 0, -1):
            first, second = text[:index], text[index:]
            if first in names and second in names:
                return first, second
        return None

    async def _drawing_role_names(
        self, candidate: DrawingCandidate, settings: WebUISettings, stream_id: str
    ) -> Optional[List[str]]:
        store = self._store
        if store is None:
            raise RuntimeError("角色库尚未初始化")
        role_names = list(candidate.role_names)
        if candidate.kind == "together":
            names = set(await asyncio.to_thread(store.list_names, GLOBAL_LIBRARY_SCOPE))
            pair = self._split_role_pair(candidate.roles_text, names)
            if pair is None:
                await self._send_reply(settings.drawing.missing_role_reply, stream_id, candidate.roles_text)
                return None
            role_names = list(pair)
        if candidate.kind == "group":
            minimum = settings.drawing.min_group_roles
            maximum = settings.drawing.max_group_roles
            if not minimum <= len(role_names) <= maximum:
                await self._send_text(
                    render_drawing_count_reply(settings.drawing.group_count_reply, minimum, maximum), stream_id
                )
                return None
        for role_name in role_names:
            if await asyncio.to_thread(store.get_role, GLOBAL_LIBRARY_SCOPE, role_name) is None:
                await self._send_reply(settings.drawing.missing_role_reply, stream_id, role_name)
                return None
        return role_names

    async def _handle_drawing_submission(self, job: DrawingSubmitJob, stream_id: str) -> bool:
        """提交前校验素材并写入持久队列，不在这里调用绘图 API。"""
        settings = job.settings
        candidate = job.candidate
        store = self._store
        task_store = self._task_store
        guard = self._guard
        if store is None or task_store is None or guard is None:
            raise RuntimeError("绘图任务系统尚未初始化")
        user_id = str(job.message["message_info"]["user_info"]["user_id"])
        group = job.message["message_info"].get("group_info") or {}
        group_id = str(group.get("group_id") or "")

        active = next(
            (task for task in await asyncio.to_thread(task_store.list_current, 1000) if task.requester_id == user_id),
            None,
        )
        if active is not None:
            await self._send_text(
                render_task_reply(settings.drawing.active_task_reply, active.id), stream_id
            )
            return False
        denied = await asyncio.to_thread(guard.check_quota, job.command_id, user_id)
        if denied:
            await self._send_text(denied, stream_id)
            return False
        queue_config = settings.task_queue
        if queue_config.max_waiting_tasks > 0:
            if await asyncio.to_thread(task_store.count_queued) >= queue_config.max_waiting_tasks:
                await self._send_text(settings.drawing.queue_full_reply, stream_id)
                return False

        role_names = await self._drawing_role_names(candidate, settings, stream_id)
        if role_names is None:
            return False
        limits = ImageLimits(
            max_image_bytes=settings.library.max_image_mb * 1024**2,
            max_pixels=settings.library.max_pixels,
        )
        try:
            reference = await MessageImageResolver(self.ctx, limits).resolve(job.message, stream_id)
        except MissingImageError:
            await self._send_text(settings.drawing.missing_reference_reply, stream_id)
            return False
        except ImageInputError:
            self.ctx.logger.warning("小花绘绘图参考图未通过校验", exc_info=True)
            await self._send_reply(settings.replies.invalid_image, stream_id, "")
            return False

        api = await asyncio.to_thread(self._require_drawing_api_store().current)
        if not api.configured:
            await self._send_text(settings.drawing.api_missing_reply, stream_id)
            return False
        base_prompt = {
            "replace": settings.drawing.replace_prompt,
            "together": settings.drawing.together_prompt,
            "group": settings.drawing.group_prompt,
        }[candidate.kind]
        prompt = base_prompt.strip()
        if candidate.extra_prompt:
            prompt = f"{prompt}\n{candidate.extra_prompt}"
        message_id = str(job.message.get("message_id") or "")
        account_id = str(job.message.get("account_id") or "")
        scope = str(job.message.get("scope") or "")
        source_key = ":".join(
            (
                "qq",
                account_id,
                scope,
                "group" if group_id else "private",
                group_id or user_id,
                message_id or uuid.uuid4().hex,
            )
        )
        try:
            async with self._task_submit_lock:
                task = await asyncio.to_thread(
                    task_store.create_task,
                    command_id=job.command_id,
                    kind=candidate.kind,
                    requester_id=user_id,
                    group_id=group_id,
                    account_id=account_id,
                    scope=scope,
                    source_message_id=message_id,
                    source_key=source_key,
                    role_names=role_names,
                    extra_prompt=candidate.extra_prompt,
                    final_prompt=prompt,
                    reference_image=reference,
                    api_model=api.model,
                    api_size=api.size,
                    max_waiting_tasks=queue_config.max_waiting_tasks,
                )
                try:
                    window = await asyncio.to_thread(guard.record_success, job.command_id, user_id)
                    if window:
                        await asyncio.to_thread(task_store.set_quota, task.id, window)
                except Exception:
                    self.ctx.logger.exception("小花绘任务次数记录失败：任务 #%s", task.id)
        except ActiveTaskError:
            active = next(
                (
                    item
                    for item in await asyncio.to_thread(task_store.list_current, 1000)
                    if item.requester_id == user_id
                ),
                None,
            )
            await self._send_text(
                render_task_reply(settings.drawing.active_task_reply, active.id if active else -1), stream_id
            )
            return False
        except QueueFullError:
            await self._send_text(settings.drawing.queue_full_reply, stream_id)
            return False
        except DuplicateTaskError:
            await self._send_text(settings.drawing.duplicate_task_reply, stream_id)
            return False

        position = await asyncio.to_thread(task_store.queue_position, task.id)
        await self._send_text(
            render_task_reply(settings.drawing.submitted_reply, task.id, position), stream_id
        )
        self._task_wakeup.set()
        self.ctx.logger.info(
            "小花绘任务已入队：#%s 类型=%s 角色=%s 排队位置=%s",
            task.id,
            DRAWING_KIND_LABELS[candidate.kind],
            "、".join(role_names),
            position,
        )
        return True

    async def _run_task_scheduler(self) -> None:
        """按配置并发数领取持久队列中的任务；配置可热更新。"""
        while True:
            try:
                settings = self._settings_store.current() if self._settings_store is not None else None
                task_store = self._task_store
                if settings is None or task_store is None:
                    await asyncio.sleep(2)
                    continue
                config = settings.task_queue
                if not config.queue_paused and len(self._task_running) < config.max_concurrent_tasks:
                    async with self._task_submit_lock:
                        task = await asyncio.to_thread(task_store.claim_next)
                    if task is not None:
                        worker = asyncio.create_task(
                            self._process_drawing_task(task), name=f"little-flower-draw.task-{task.id}"
                        )
                        self._task_running.add(worker)
                        worker.add_done_callback(self._task_running.discard)
                        continue
                try:
                    await asyncio.wait_for(self._task_wakeup.wait(), timeout=max(1, config.poll_interval_seconds))
                except asyncio.TimeoutError:
                    pass
                self._task_wakeup.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("小花绘任务调度器异常")
                await asyncio.sleep(2)

    async def _run_task_maintenance(self) -> None:
        """按配置定期清理历史记录和过期输出图。"""
        while True:
            try:
                settings = self._settings_store.current() if self._settings_store is not None else None
                store = self._task_store
                if settings is not None and store is not None:
                    config = settings.task_queue
                    if config.history_retention_days > 0:
                        removed = await asyncio.to_thread(store.purge_history, config.history_retention_days)
                        if removed:
                            self.ctx.logger.info("小花绘清理了 %s 个过期历史任务", removed)
                    if config.output_retention_days > 0:
                        removed = await asyncio.to_thread(store.purge_outputs, config.output_retention_days)
                        if removed:
                            self.ctx.logger.info("小花绘清理了 %s 个过期任务输出图", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("小花绘任务历史清理失败")
            await asyncio.sleep(3600)

    async def _process_drawing_task(self, task: TaskRecord) -> None:
        settings = self._settings_store.current() if self._settings_store is not None else None
        timeout = settings.task_queue.task_timeout_seconds if settings is not None else 600
        try:
            await asyncio.wait_for(self._execute_drawing_task(task), timeout=timeout)
        except asyncio.TimeoutError:
            await self._fail_task(task, "任务执行超时")
        except DrawingApiError as error:
            self.ctx.logger.warning("小花绘任务 #%s API 调用失败：%s", task.id, error)
            await self._fail_task(task, str(error))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.ctx.logger.exception("小花绘任务 #%s 执行失败", task.id)
            await self._fail_task(task, str(error))

    def _task_message(self, task: TaskRecord) -> Dict[str, Any]:
        info: Dict[str, Any] = {"user_info": {"user_id": task.requester_id, "user_nickname": ""}}
        if task.group_id:
            info["group_info"] = {"group_id": task.group_id, "group_name": ""}
        return {
            "platform": "qq",
            "account_id": task.account_id,
            "scope": task.scope,
            "message_id": task.source_message_id,
            "message_info": info,
        }

    async def _execute_drawing_task(self, task: TaskRecord) -> None:
        store = self._store
        task_store = self._task_store
        if store is None or task_store is None:
            raise RuntimeError("绘图任务系统尚未初始化")
        message = self._task_message(task)
        stream_id = await self._open_chat(message)
        await asyncio.to_thread(task_store.set_stage, task.id, "preparing")
        try:
            reference = await asyncio.to_thread(Path(task.reference_path).read_bytes)
        except OSError as error:
            raise DrawingApiError("任务参考图已丢失") from error
        role_images: List[bytes] = []
        for role_name in task.role_names:
            image = await asyncio.to_thread(store.get, GLOBAL_LIBRARY_SCOPE, role_name)
            if image is None:
                raise DrawingApiError(f"角色「{role_name}」已不存在")
            role_images.append(image)
        api = await asyncio.to_thread(self._require_drawing_api_store().current)
        if not api.configured:
            raise DrawingApiError("绘图 API 尚未配置")
        run_config = api.model_copy(update={"model": task.api_model, "size": task.api_size})
        await asyncio.to_thread(task_store.set_stage, task.id, "calling_api")
        result = await asyncio.to_thread(
            OpenAIImageClient().generate,
            run_config,
            task.final_prompt,
            [reference, *role_images],
        )
        await asyncio.to_thread(task_store.set_stage, task.id, "saving")
        path = await asyncio.to_thread(self._save_drawing_image, result.image, task.kind)
        if not await self.ctx.send.image(base64.b64encode(result.image).decode("ascii"), stream_id):
            raise ReplyDeliveryError("绘图结果发送失败")
        await asyncio.to_thread(task_store.succeed, task.id, str(path))
        await self._send_text(
            render_drawing_reply(
                self._settings_store.current().drawing.success_reply,
                DRAWING_KIND_LABELS[task.kind],
                len(task.role_names),
            ),
            stream_id,
        )
        self.ctx.logger.info(
            "小花绘任务 #%s 完成：类型=%s 角色=%s 输出=%s",
            task.id,
            DRAWING_KIND_LABELS[task.kind],
            "、".join(task.role_names),
            path,
        )

    async def _refund_task(self, task: TaskRecord) -> None:
        if not task.quota_counted or task.quota_refunded:
            return
        guard = self._guard
        task_store = self._task_store
        if guard is None or task_store is None:
            return
        try:
            await asyncio.to_thread(
                guard.refund_task, task.id, task.command_id, task.requester_id, task.quota_window_start
            )
            await asyncio.to_thread(task_store.mark_refunded, task.id)
        except Exception:
            self.ctx.logger.exception("小花绘任务 #%s 退还次数失败", task.id)

    async def _fail_task(self, task: TaskRecord, error: str, notify: bool = True) -> None:
        task_store = self._task_store
        if task_store is None:
            return
        current = await asyncio.to_thread(task_store.get, task.id)
        if current is None or current.status not in {"queued", "running"}:
            return
        record = await asyncio.to_thread(task_store.fail, task.id, error)
        await self._refund_task(record)
        if notify:
            try:
                stream_id = await self._open_chat(self._task_message(record))
                await self._send_text(
                    render_task_fail_reply(
                        self._settings_store.current().drawing.task_failed_reply,
                        record.id,
                        error[:800],
                    ),
                    stream_id,
                )
            except Exception:
                self.ctx.logger.exception("小花绘任务 #%s 失败提示未能送达", record.id)

    @staticmethod
    def _save_drawing_image(image: bytes, kind: str) -> Path:
        root = (Path(__file__).resolve().parent / "drawings").resolve()
        now = datetime.now()
        directory = root / now.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(io.BytesIO(image)) as source:
                suffix = {"JPEG": "jpg", "WEBP": "webp", "PNG": "png"}.get((source.format or "PNG").upper(), "png")
        except (UnidentifiedImageError, OSError) as error:
            raise RuntimeError("绘图结果不是有效图片") from error
        path = (directory / f"{now:%H%M%S-%f}-{kind}.{suffix}").resolve()
        if not path.is_relative_to(root):
            raise RuntimeError("绘图结果保存路径越界")
        path.write_bytes(image)
        return path

    async def _handle_job(self, job: LibraryJob, stream_id: str) -> bool:
        """执行一次角色库请求，返回是否真正成功；失败或未找到不消耗次数。"""
        settings = job.settings
        role_name = job.request.role_name
        if not valid_role_name(role_name, settings.library.max_name_length):
            await self._send_reply(settings.replies.invalid_name, stream_id, role_name)
            return False
        store = self._store
        if store is None:
            raise RuntimeError("角色库尚未初始化")

        if job.request.action == "register":
            limits = ImageLimits(
                max_image_bytes=settings.library.max_image_mb * 1024**2,
                max_pixels=settings.library.max_pixels,
            )
            try:
                image = await MessageImageResolver(self.ctx, limits).resolve(job.message, stream_id)
            except MissingImageError:
                await self._send_reply(settings.replies.missing_image, stream_id, role_name)
                return False
            except ImageInputError:
                self.ctx.logger.warning("小花绘登记图片未通过校验", exc_info=True)
                await self._send_reply(settings.replies.invalid_image, stream_id, role_name)
                return False
            owner_id = str(job.message["message_info"]["user_info"]["user_id"])
            overwritten = await self._save_role(store, role_name, owner_id, image)
            template = settings.replies.overwritten if overwritten else settings.replies.registered
            await self._send_reply(template, stream_id, role_name)
            return True

        image = await asyncio.to_thread(store.get, GLOBAL_LIBRARY_SCOPE, role_name)
        if image is None:
            await self._send_reply(settings.replies.not_found, stream_id, role_name)
            return False
        await self._send_reply(settings.replies.view, stream_id, role_name)
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

    def _require_drawing_api_store(self) -> DrawingApiStore:
        if self._drawing_api_store is None:
            raise RuntimeError("绘图 API 设置尚未初始化")
        return self._drawing_api_store

    def _require_task_store(self) -> DrawingTaskStore:
        if self._task_store is None:
            raise RuntimeError("绘图任务存储尚未初始化")
        return self._task_store

    @staticmethod
    def _task_summary(record: TaskRecord, queue_position: int = 0) -> Dict[str, Any]:
        return {
            "id": record.id,
            "status": record.status,
            "stage": record.stage,
            "command_id": record.command_id,
            "kind": record.kind,
            "requester_id": record.requester_id,
            "group_id": record.group_id,
            "role_names": record.role_names,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "attempts": record.attempts,
            "error": record.error,
            "queue_position": queue_position,
            "output_available": bool(record.output_path and Path(record.output_path).is_file()),
            "reference_available": bool(record.reference_path and Path(record.reference_path).is_file()),
            "can_cancel": record.status == "queued",
            "can_retry": record.status in {"failed", "cancelled", "succeeded"},
        }

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
        if not isinstance(raw, dict) or not {
            "keywords",
            "replies",
            "library",
            "drawing",
            "task_queue",
            "access",
            "commands",
            "guard_replies",
        } <= raw.keys():
            raise ValueError("设置数据缺少必要分组")
        settings = WebUISettings.model_validate(raw)
        await asyncio.to_thread(self._require_settings_store().save, settings)
        return {"saved": True}

    @API("webui_drawing_api", description="读取 OpenAI 兼容绘图 API 配置", version="1")
    async def webui_drawing_api(self) -> Dict[str, Any]:
        config = await asyncio.to_thread(self._require_drawing_api_store().current)
        payload = config.model_dump(mode="json")
        payload["configured"] = config.configured
        payload["endpoint"] = config.endpoint if config.base_url else ""
        return payload

    @API("webui_drawing_api_save", description="保存 OpenAI 兼容绘图 API 配置", version="1")
    async def webui_drawing_api_save(self, api_json: str) -> Dict[str, Any]:
        try:
            raw = json.loads(api_json)
            if not isinstance(raw, dict):
                raise ValueError("API 配置必须是 JSON 对象")
            config = DrawingApiConfig.model_validate(raw)
        except (json.JSONDecodeError, ValueError) as error:
            return {"saved": False, "error": str(error)}
        await asyncio.to_thread(self._require_drawing_api_store().save, config)
        return {"saved": True, "configured": config.configured}

    @API("webui_drawing_models", description="查询 OpenAI 兼容接口的可用模型", version="1")
    async def webui_drawing_models(self, base_url: str = "", api_key: str = "") -> Dict[str, Any]:
        try:
            config = DrawingApiConfig(base_url=base_url, api_key=api_key)
            models = await asyncio.to_thread(fetch_models, config.base_url, config.api_key)
        except (ValueError, DrawingApiError) as error:
            return {"models": [], "error": str(error)}
        return {"models": models}

    @API("webui_current_tasks", description="读取排队中和执行中的绘图任务", version="1")
    async def webui_current_tasks(self) -> Dict[str, Any]:
        store = self._require_task_store()
        tasks = await asyncio.to_thread(store.list_current, 100)
        payload = []
        for task in tasks:
            position = await asyncio.to_thread(store.queue_position, task.id) if task.status == "queued" else 0
            payload.append(self._task_summary(task, position))
        settings = await asyncio.to_thread(self._require_settings_store().current)
        return {
            "tasks": payload,
            "counts": {
                "queued": sum(1 for task in tasks if task.status == "queued"),
                "running": sum(1 for task in tasks if task.status == "running"),
            },
            "config": settings.task_queue.model_dump(mode="json"),
        }

    @API("webui_task_history", description="分页读取已完成、失败和取消的任务", version="1")
    async def webui_task_history(
        self, page: int = 1, page_size: int = 20, status: str = "", query: str = ""
    ) -> Dict[str, Any]:
        page = max(1, page)
        page_size = min(50, max(5, page_size))
        store = self._require_task_store()
        tasks, total = await asyncio.to_thread(store.list_history, page, page_size, status, query)
        pages = max(1, (total + page_size - 1) // page_size)
        return {
            "tasks": [self._task_summary(task) for task in tasks],
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
        }

    @API("webui_task_detail", description="读取单个绘图任务详情", version="1")
    async def webui_task_detail(self, task_id: int) -> Dict[str, Any]:
        task = await asyncio.to_thread(self._require_task_store().get, task_id)
        if task is None:
            raise ValueError("任务不存在")
        payload = self._task_summary(task)
        payload.update(
            {
                "extra_prompt": task.extra_prompt,
                "final_prompt": task.final_prompt,
                "api_model": task.api_model,
                "api_size": task.api_size,
                "quota_counted": task.quota_counted,
                "quota_refunded": task.quota_refunded,
                "source_message_id": task.source_message_id,
            }
        )
        return payload

    @API("webui_task_cancel", description="取消排队中的绘图任务并退还次数", version="1")
    async def webui_task_cancel(self, task_id: int) -> Dict[str, Any]:
        task = await asyncio.to_thread(self._require_task_store().cancel, task_id)
        if task is None:
            return {"cancelled": False, "error": "任务不存在或已开始执行"}
        await self._refund_task(task)
        return {"cancelled": True}

    @API("webui_task_retry", description="手动重新提交一个历史任务，生成新任务 ID", version="1")
    async def webui_task_retry(self, task_id: int) -> Dict[str, Any]:
        store = self._require_task_store()
        old = await asyncio.to_thread(store.get, task_id)
        if old is None:
            return {"retried": False, "error": "任务不存在"}
        if old.status not in {"failed", "cancelled", "succeeded"}:
            return {"retried": False, "error": "任务尚未结束"}
        guard = self._guard
        if guard is None:
            raise RuntimeError("绘图权限守卫尚未初始化")
        denied = await asyncio.to_thread(guard.check_quota, old.command_id, old.requester_id)
        if denied:
            return {"retried": False, "error": denied}
        try:
            reference = await asyncio.to_thread(Path(old.reference_path).read_bytes)
        except OSError:
            return {"retried": False, "error": "原任务参考图已丢失"}
        settings = await asyncio.to_thread(self._require_settings_store().current)
        try:
            async with self._task_submit_lock:
                task = await asyncio.to_thread(
                    store.create_task,
                    command_id=old.command_id,
                    kind=old.kind,
                    requester_id=old.requester_id,
                    group_id=old.group_id,
                    account_id=old.account_id,
                    scope=old.scope,
                    source_message_id=old.source_message_id,
                    source_key=f"retry:{old.id}:{uuid.uuid4().hex}",
                    role_names=old.role_names,
                    extra_prompt=old.extra_prompt,
                    final_prompt=old.final_prompt,
                    reference_image=reference,
                    api_model=old.api_model,
                    api_size=old.api_size,
                    max_waiting_tasks=settings.task_queue.max_waiting_tasks,
                )
                try:
                    window = await asyncio.to_thread(guard.record_success, old.command_id, old.requester_id)
                    if window:
                        await asyncio.to_thread(store.set_quota, task.id, window)
                except Exception:
                    self.ctx.logger.exception("小花绘重试任务次数记录失败：任务 #%s", task.id)
        except ActiveTaskError as error:
            return {"retried": False, "error": str(error)}
        except QueueFullError as error:
            return {"retried": False, "error": str(error)}
        except DuplicateTaskError as error:
            return {"retried": False, "error": str(error)}
        self._task_wakeup.set()
        return {"retried": True, "task_id": task.id}

    @API("webui_task_delete", description="批量删除终态历史任务及其文件", version="1")
    async def webui_task_delete(self, ids_json: str) -> Dict[str, Any]:
        try:
            raw = json.loads(ids_json)
            ids = [int(value) for value in raw] if isinstance(raw, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"deleted": 0, "error": "任务 ID 列表无效"}
        if not ids:
            return {"deleted": 0, "error": "没有选择任务"}
        deleted = await asyncio.to_thread(self._require_task_store().delete_tasks, ids)
        return {"deleted": deleted}

    @API("webui_task_thumbnail", description="读取任务输出图的压缩缩略图", version="1")
    async def webui_task_thumbnail(self, task_id: int) -> Dict[str, Any]:
        task = await asyncio.to_thread(self._require_task_store().get, task_id)
        if task is None or not task.output_path or not Path(task.output_path).is_file():
            return {"image": None, "error": "任务没有可预览的图片"}
        try:
            data = await asyncio.to_thread(Path(task.output_path).read_bytes)
            thumb = await asyncio.to_thread(render_thumbnail, data)
            return {"image": data_url(thumb)}
        except Exception as error:
            return {"image": None, "error": str(error)}

    @API("webui_roles", description="分页读取角色库列表与统计", version="1")
    async def webui_roles(
        self, query: str = "", page: int = 1, page_size: int = 20, sort: str = "name"
    ) -> Dict[str, Any]:
        payload = await asyncio.to_thread(
            list_payload, self._require_store(), GLOBAL_LIBRARY_SCOPE, query, page, page_size, sort
        )
        settings = await asyncio.to_thread(self._require_settings_store().current)
        payload["register_template"] = settings.keywords.register_templates[0]
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
        settings = await asyncio.to_thread(self._require_settings_store().current)
        new_name = unicodedata.normalize("NFC", new_name.strip())
        if not valid_role_name(new_name, settings.library.max_name_length):
            raise ValueError("新角色名不合法")
        renamed = await asyncio.to_thread(self._require_store().rename, GLOBAL_LIBRARY_SCOPE, old_name, new_name)
        if not renamed:
            raise ValueError("角色不存在")
        return {"renamed": old_name != new_name}

    @API("webui_role_set_image", description="用 WebUI 上传的图片新建或替换角色", version="1")
    async def webui_role_set_image(self, upload_id: str, name: str) -> Dict[str, Any]:
        settings = await asyncio.to_thread(self._require_settings_store().current)
        name = unicodedata.normalize("NFC", name.strip())
        if not valid_role_name(name, settings.library.max_name_length):
            raise ValueError("角色名不合法")
        uploads_dir = (self.ctx.paths.data_dir / "uploads").resolve()
        upload = await self.ctx.webui.claim_upload(upload_id)
        upload_path = Path(str(upload.get("path") or "")).resolve()
        if not upload_path.is_relative_to(uploads_dir) or not upload_path.is_file():
            raise RuntimeError("WebUI 上传文件不在插件上传目录内")
        try:
            image = await asyncio.to_thread(upload_path.read_bytes)
            limits = ImageLimits(
                max_image_bytes=settings.library.max_image_mb * 1024**2,
                max_pixels=settings.library.max_pixels,
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
