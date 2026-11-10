"""通过真实 SDK 能力代理验证角色库的消息处理与热配置。"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import asyncio
import base64
import importlib.util
import json
import sys
import threading
import unittest

from maibot_sdk.context import PluginContext, PluginPaths
from PIL import Image


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "little_flower_draw_integration_test"
SPEC = importlib.util.spec_from_file_location(
    MODULE_NAME, PLUGIN_ROOT / "plugin.py", submodule_search_locations=[str(PLUGIN_ROOT)]
)
assert SPEC is not None and SPEC.loader is not None
PLUGIN_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = PLUGIN_MODULE
SPEC.loader.exec_module(PLUGIN_MODULE)
MANIFEST = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))


def png(color: str) -> bytes:
    """生成用于真实图片解码、持久化和发送校验的小 PNG。"""
    with BytesIO() as stream:
        Image.new("RGB", (4, 3), color).save(stream, format="PNG")
        return stream.getvalue()


class FakeHostRPC:
    """只替代宿主能力响应；不连接 OneBot、QQ 或绘图接口。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.texts: List[str] = []
        self.images: List[bytes] = []
        self.image_streams: List[str] = []
        self.at_targets: List[str] = []
        self.history: Dict[str, Dict[str, Any]] = {}
        self.fail_text_sends = 0
        self.fail_image_sends = 0
        self.fail_hybrid_sends = 0

    @staticmethod
    def session(group_id: str = "10001", user_id: str = "20001") -> str:
        return "group-" + group_id if group_id else "private-" + user_id

    async def __call__(self, method: str, plugin_id: str, payload: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        if method != "cap.call" or plugin_id != MANIFEST["id"]:
            raise AssertionError("插件应通过绑定自身身份的能力代理访问宿主")
        capability = payload["capability"]
        if capability not in MANIFEST["capabilities"]:
            raise AssertionError("清单未声明调用的能力：" + capability)
        args = payload["args"]
        self.calls.append(deepcopy(payload))
        if capability == "chat.open_session":
            return {"success": True, "session_id": self.session(args["group_id"], args["user_id"])}
        if capability == "send.text":
            if self.fail_text_sends:
                self.fail_text_sends -= 1
                return {"success": False}
            self.texts.append(args["text"])
            return {"success": True}
        if capability == "send.image":
            if self.fail_image_sends:
                self.fail_image_sends -= 1
                return {"success": False}
            self.images.append(base64.b64decode(args["image_base64"], validate=True))
            self.image_streams.append(args["stream_id"])
            return {"success": True}
        if capability == "send.hybrid":
            if self.fail_hybrid_sends:
                self.fail_hybrid_sends -= 1
                return {"success": False}
            for segment in args["segments"]:
                if segment["type"] == "text":
                    self.texts.append(segment["data"])
                elif segment["type"] == "image":
                    self.images.append(base64.b64decode(segment["content"], validate=True))
                    self.image_streams.append(args["stream_id"])
                elif segment["type"] == "at":
                    self.at_targets.append(segment["data"]["target_user_id"])
            return {"success": True}
        if capability == "message.get_by_id":
            return {"success": True, "message": self.history.get(args["message_id"])}
        if capability == "api.get":
            return {"success": True, "api": None}
        raise AssertionError("测试未声明的宿主调用：" + capability)


class PluginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self._random_pick = patch(
            f"{MODULE_NAME}.role_patterns.random.choice", side_effect=lambda values: values[0]
        )
        self._random_pick.start()
        self.addCleanup(self._random_pick.stop)
        self.rpc = FakeHostRPC()
        self.plugin = PLUGIN_MODULE.create_plugin()
        self.config = self.plugin.build_default_config()
        self.config["plugin"]["enabled"] = True
        self.plugin.set_plugin_config(self.config)
        root = Path(self.temporary.name)
        self.plugin._set_context(
            PluginContext(
                MANIFEST["id"], rpc_call=self.rpc,
                paths=PluginPaths(data_dir=root / "data", runtime_dir=root / "temp"),
            )
        )
        with patch.object(PLUGIN_MODULE, "load_legacy_settings", return_value=None):
            await self.plugin.on_load()
        self.addAsyncCleanup(self.plugin.on_unload)
        self.message_number = 0

    def message(
        self, text: str, images: Optional[List[bytes]] = None, *, group: str = "10001",
        user: str = "20001", message_id: str = "", reply_to: str = "", at: str = "",
    ) -> Dict[str, Any]:
        self.message_number += 1
        segments: List[Dict[str, Any]] = []
        if at:
            segments.append({"type": "at", "data": {"target_user_id": at}})
        if reply_to:
            segments.append({"type": "reply", "data": {"target_message_id": reply_to}})
        segments.append({"type": "text", "data": text})
        for image in images or []:
            segments.append({"type": "image", "data": "", "binary_data_base64": base64.b64encode(image).decode("ascii")})
        info: Dict[str, Any] = {
            "user_info": {
                "user_id": user,
                "user_nickname": "测试群友",
                "user_cardname": "测试群名片" if group else None,
            }
        }
        if group:
            info["group_info"] = {"group_id": group, "group_name": "角色库测试群"}
        return {
            "platform": "qq", "account_id": "99999", "scope": "", "session_id": "",
            "message_id": message_id or str(self.message_number), "message_info": info,
            "raw_message": segments,
        }

    async def submit(self, message: Dict[str, Any]) -> Dict[str, Any]:
        result = await self.plugin.receive_role_message(message=message)
        await asyncio.wait_for(self.plugin._queue.join(), timeout=5)
        return result

    def clear_output(self) -> None:
        self.rpc.texts.clear()
        self.rpc.images.clear()
        self.rpc.image_streams.clear()
        self.rpc.at_targets.clear()
        self.rpc.calls.clear()

    def set_access(self, **values: Any) -> None:
        data = self.plugin._settings_store.current().model_dump(mode="python")
        data["access"].update(values)
        self.plugin._settings_store.save(PLUGIN_MODULE.WebUISettings.model_validate(data))

    def set_policy(self, command: str, **values: Any) -> None:
        data = self.plugin._settings_store.current().model_dump(mode="python")
        for policy in data["commands"]["policies"]:
            if policy["command"] == command:
                policy.update(values)
        self.plugin._settings_store.save(PLUGIN_MODULE.WebUISettings.model_validate(data))

    def guard_replies(self) -> Any:
        return self.plugin._settings_store.current().guard_replies

    def reply(self, name: str) -> str:
        return getattr(self.plugin._settings_store.current().replies, name)[0]

    def set_drawing_api(self) -> None:
        self.plugin._drawing_api_store.save(
            PLUGIN_MODULE.DrawingApiConfig(
                base_url="https://example.test/v1", api_key="test-token", model="gpt-image-1"
            )
        )

    async def wait_task(self, task_id: int) -> Any:
        for _ in range(100):
            task = await asyncio.to_thread(self.plugin._task_store.get, task_id)
            if task is not None and task.status in {"succeeded", "failed", "cancelled"}:
                return task
            await asyncio.sleep(0.05)
        raise AssertionError(f"任务 #{task_id} 未在超时前结束")

    async def current_task(self) -> Any:
        tasks = [
            task
            for task in await asyncio.to_thread(self.plugin._task_store.list_current)
            if task.requester_id == "20001"
        ]
        if tasks:
            return max(tasks, key=lambda task: task.id)
        history, _ = await asyncio.to_thread(self.plugin._task_store.list_history, 1, 100)
        tasks = [task for task in history if task.requester_id == "20001"]
        return max(tasks, key=lambda task: task.id)

    async def latest_task(self) -> Any:
        current = await asyncio.to_thread(self.plugin._task_store.list_current)
        history, _ = await asyncio.to_thread(self.plugin._task_store.list_history, 1, 100)
        tasks = [task for task in [*current, *history] if task.requester_id == "20001"]
        return max(tasks, key=lambda task: task.id)

    async def test_first_message_opens_real_target_chat_and_keeps_only_the_first_image(self) -> None:
        images = [png("red"), png("green"), png("blue")]
        result = await self.submit(self.message("小花小花这是 小花", images))

        self.assertEqual(result["action"], "abort")
        self.assertEqual(self.rpc.calls[0]["capability"], "chat.open_session")
        open_args = self.rpc.calls[0]["args"]
        self.assertEqual((open_args["chat_type"], open_args["group_id"], open_args["account_id"]), ("group", "10001", "99999"))
        self.assertEqual(self.rpc.texts, [self.reply("registered").format(角色名="小花")])

        self.clear_output()
        await self.submit(self.message("小花小花我要看小花"))
        self.assertEqual(self.rpc.images, [images[0]])
        self.assertEqual(self.rpc.image_streams, [self.rpc.session()])

    async def test_global_library_is_shared_across_users_and_groups_and_overwritten_globally(self) -> None:
        original, replacement = png("red"), png("blue")
        await self.submit(self.message("小花小花这是 花花", [original], group="10001", user="20001"))
        self.clear_output()

        await self.submit(self.message("小花小花我要看花花", group="10002", user="20002"))
        self.assertEqual(self.rpc.images, [original])
        self.assertEqual(self.rpc.image_streams, [self.rpc.session("10002")])
        self.clear_output()

        await self.submit(self.message("小花小花这是 花花", [replacement], group="10002", user="20002"))
        self.assertEqual(self.rpc.texts, [self.reply("overwritten").format(角色名="花花")])
        self.clear_output()

        await self.submit(self.message("小花小花我要看花花", group="10001", user="20003"))
        self.assertEqual(self.rpc.images, [replacement])

    async def test_quoted_history_uses_original_image_not_the_reply_preview(self) -> None:
        image = png("purple")
        source = self.message("别人上传的图片", [image], message_id="original-picture", user="20002")
        source["session_id"] = self.rpc.session()
        self.rpc.history["original-picture"] = source
        request = self.message("小花小花这是 紫花", reply_to="original-picture")
        request["raw_message"][0]["data"]["target_message_content"] = "[图片] 小花小花这是错误预览"

        await self.submit(request)
        history_calls = [call for call in self.rpc.calls if call["capability"] == "message.get_by_id"]
        self.assertEqual(len(history_calls), 1)
        self.assertTrue(history_calls[0]["args"]["include_binary_data"])
        self.assertEqual(history_calls[0]["args"]["stream_id"], self.rpc.session())
        self.clear_output()

        await self.submit(self.message("小花小花我要看紫花"))
        self.assertEqual(self.rpc.images, [image])

    async def test_help_command_returns_a_generated_image(self) -> None:
        self.clear_output()
        result = await self.submit(self.message("小花绘帮助"))
        self.assertEqual(result["action"], "abort")
        self.assertEqual(self.rpc.texts, [])
        self.assertEqual(len(self.rpc.images), 1)
        self.assertTrue(self.rpc.images[0].startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_list_command_returns_count_and_all_names(self) -> None:
        settings = self.plugin._settings_store.current()
        self.clear_output()
        await self.submit(self.message("小花记得谁"))
        self.assertEqual(self.rpc.texts, [PLUGIN_MODULE.pick_reply(settings.replies.list_empty_reply)])

        await self.submit(self.message("小花小花这是 角色B", [png("red")]))
        await self.submit(self.message("小花小花这是 角色A", [png("blue")]))
        self.clear_output()
        await self.submit(self.message("小花记得谁"))
        self.assertEqual(
            self.rpc.texts,
            [PLUGIN_MODULE.render_list_reply(settings.replies.list_reply, 2, "角色A、角色B")],
        )

    async def test_missing_or_invalid_images_do_not_report_success_or_create_a_role(self) -> None:
        for name, images, reply_to, reply_field in (
            ("无图", [], "", "missing_image"),
            ("无引用图", [], "missing-source", "missing_image"),
            ("坏图", [b"not-an-image"], "", "invalid_image"),
        ):
            with self.subTest(name=name):
                self.clear_output()
                request = self.message("小花小花这是 " + name, images, reply_to=reply_to)
                if reply_field == "invalid_image":
                    with self.assertLogs(self.plugin.ctx.logger, level="WARNING") as logs:
                        await self.submit(request)
                    self.assertIn("图片未通过校验", "\n".join(logs.output))
                else:
                    await self.submit(request)
                expected = (
                    self.reply("image_corrupt")
                    if reply_field == "invalid_image"
                    else self.reply(reply_field).format(角色名=name)
                )
                self.assertEqual(self.rpc.texts, [expected])
                self.assertFalse(self.rpc.images)
                self.clear_output()
                await self.submit(self.message("小花小花我要看" + name))
                self.assertEqual(self.rpc.texts, [self.reply("not_found").format(角色名=name)])
                self.assertFalse(self.rpc.images)

    async def test_image_error_replies_are_configurable(self) -> None:
        settings = self.plugin._settings_store.current()
        settings.replies.image_corrupt = ["自定义坏图提示喵"]
        settings.replies.image_too_large = ["太大啦：{实际大小}/{大小限制}"]
        settings.replies.image_invalid_format = ["格式是{格式}喵"]
        settings.library.max_image_mb = 1
        self.plugin._settings_store.save(settings)
        self.clear_output()

        await self.submit(self.message("小花小花这是 坏图", [b"not-an-image"]))
        self.assertEqual(self.rpc.texts, ["自定义坏图提示喵"])

        self.clear_output()
        await self.submit(self.message("小花小花这是 大图", [b"x" * (1024**2 + 1)]))
        self.assertEqual(self.rpc.texts, ["太大啦：1.0MB/1.0MB"])

        self.clear_output()
        bmp = BytesIO()
        Image.new("RGB", (4, 3), "red").save(bmp, format="BMP")
        await self.submit(self.message("小花小花这是 BMP", [bmp.getvalue()]))
        self.assertEqual(self.rpc.texts, ["格式是BMP喵"])

    async def test_at_prefixed_template_requires_a_bot_mention(self) -> None:
        settings = self.plugin._settings_store.current()
        settings.keywords.register_templates = ["@小花小花这是 {角色名}"]
        self.plugin._settings_store.save(settings)
        self.clear_output()

        ignored = await self.submit(self.message("小花小花这是 必须艾特", [png("red")]))
        self.assertEqual(ignored["action"], "continue")
        self.assertFalse(self.rpc.calls)

        accepted = await self.submit(self.message("小花小花这是 必须艾特", [png("red")], at="99999"))
        self.assertEqual(accepted["action"], "abort")
        self.assertIn("必须艾特", self.plugin._store.list_names(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE))

    async def test_hot_config_changes_both_matching_and_replies_without_reloading(self) -> None:
        image = png("yellow")
        await self.submit(self.message("小花小花这是 原角色", [image]))
        self.clear_output()
        worker = self.plugin._worker
        settings = self.plugin._settings_store.current()
        settings.keywords.register_templates = ["记住 {角色名}"]
        settings.keywords.view_templates = ["看看{角色名}"]
        settings.replies.registered = ["收好{角色名}啦喵！"]
        settings.replies.view = [""]
        self.plugin._settings_store.save(settings)

        result = await self.submit(self.message("小花小花这是 旧词不触发", [image]))
        self.assertEqual(result["action"], "continue")
        self.assertFalse(self.rpc.calls)
        await self.submit(self.message("记住 新角色", [image]))
        self.assertEqual(self.rpc.texts, ["收好新角色啦喵！"])
        self.clear_output()
        await self.submit(self.message("看看原角色"))
        self.assertFalse(self.rpc.texts)
        self.assertEqual(self.rpc.images, [image])
        self.assertIs(self.plugin._worker, worker)

    async def test_ordinary_chat_continues_and_same_message_is_processed_once(self) -> None:
        result = await self.submit(self.message("今天吃什么"))
        self.assertEqual(result["action"], "continue")
        self.assertFalse(self.rpc.calls)
        image = png("orange")
        message = self.message("小花小花这是 橙花", [image], message_id="same-event")
        await self.submit(message)
        await self.submit(message)
        self.assertEqual(len(self.rpc.texts), 1)

    async def test_invalid_name_does_not_create_a_role(self) -> None:
        await self.submit(self.message("小花小花这是 名字\n换行", [png("red")]))
        self.assertEqual(self.rpc.texts, [self.reply("invalid_name")])

    async def test_bot_own_messages_do_not_trigger_configured_replies(self) -> None:
        result = await self.submit(self.message("小花小花这是 自己", [png("red")], user="99999"))
        self.assertEqual(result["action"], "continue")
        self.assertFalse(self.rpc.calls)

    async def test_view_reply_merges_image_and_supports_optional_at(self) -> None:
        image = png("pink")
        await self.submit(self.message("小花小花这是 合并角色", [image]))
        self.clear_output()

        await self.submit(self.message("小花小花我要看合并角色"))
        self.assertEqual(self.rpc.images, [image])
        self.assertEqual(self.rpc.at_targets, [])
        hybrid = [call for call in self.rpc.calls if call["capability"] == "send.hybrid"]
        self.assertEqual(len(hybrid), 1)
        self.assertEqual([segment["type"] for segment in hybrid[0]["args"]["segments"]], ["text", "image"])

        settings = self.plugin._settings_store.current()
        settings.replies.at_replies = ["view"]
        self.plugin._settings_store.save(settings)
        self.clear_output()
        await self.submit(self.message("小花小花我要看合并角色"))
        self.assertEqual(self.rpc.images, [image])
        self.assertEqual(self.rpc.at_targets, ["20001"])
        hybrid = [call for call in self.rpc.calls if call["capability"] == "send.hybrid"]
        self.assertEqual(len(hybrid), 1)
        self.assertEqual(
            [segment["type"] for segment in hybrid[0]["args"]["segments"]],
            ["at", "text", "image"],
        )

    async def test_equal_message_ids_in_different_chats_are_not_duplicates(self) -> None:
        await self.submit(self.message("小花小花这是 跨群", [png("red")], group="111", message_id="local-id"))
        await self.submit(self.message("小花小花这是 跨群", [png("blue")], group="222", message_id="local-id"))
        self.assertEqual(len(self.rpc.texts), 2)
        self.clear_output()
        await self.submit(self.message("小花小花我要看跨群", group="333"))
        self.assertEqual(self.rpc.images, [png("blue")])
        self.assertEqual(self.rpc.image_streams, [self.rpc.session("333")])

    async def test_invalid_replacement_keeps_the_previous_picture(self) -> None:
        image = png("red")
        await self.submit(self.message("小花小花这是 红花", [image]))
        self.clear_output()
        with self.assertLogs(self.plugin.ctx.logger, level="WARNING"):
            await self.submit(self.message("小花小花这是 红花", [b"broken-picture"]))
        self.assertEqual(self.rpc.texts, [self.reply("image_corrupt")])
        self.clear_output()
        await self.submit(self.message("小花小花我要看红花"))
        self.assertEqual(self.rpc.images, [image])

    async def test_store_failure_is_logged_and_replied_as_an_error(self) -> None:
        with patch.object(self.plugin._store, "save", side_effect=OSError("模拟磁盘写入失败")):
            with self.assertLogs(self.plugin.ctx.logger, level="ERROR") as logs:
                await self.submit(self.message("小花小花这是 失败角色", [png("red")]))
        self.assertIn("模拟磁盘写入失败", "\n".join(logs.output))
        self.assertEqual(self.rpc.texts, [self.reply("error")])
        self.clear_output()
        await self.submit(self.message("小花小花我要看失败角色"))
        self.assertEqual(self.rpc.texts, [self.reply("not_found").format(角色名="失败角色")])
        self.assertFalse(self.rpc.images)

    async def test_failed_confirmation_is_logged_and_does_not_claim_delivery(self) -> None:
        self.rpc.fail_text_sends = 1
        with self.assertLogs(self.plugin.ctx.logger, level="ERROR") as logs:
            await self.submit(self.message("小花小花这是 红花", [png("red")]))
        self.assertIn("文字回复发送失败", "\n".join(logs.output))
        self.assertEqual(self.rpc.texts, [self.reply("error")])

    async def test_failed_image_send_is_logged_and_gets_an_error_reply(self) -> None:
        await self.submit(self.message("小花小花这是 红花", [png("red")]))
        self.clear_output()
        self.rpc.fail_hybrid_sends = 1
        with self.assertLogs(self.plugin.ctx.logger, level="ERROR") as logs:
            await self.submit(self.message("小花小花我要看红花"))
        self.assertIn("角色回复图文消息发送失败", "\n".join(logs.output))
        self.assertEqual(self.rpc.texts[-1], self.reply("error"))
        self.assertFalse(self.rpc.images)

    async def test_full_queue_never_falls_through_to_chat_when_busy_reply_fails(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def block_job(job: Any, stream_id: str) -> None:
            started.set()
            await release.wait()

        with patch.object(self.plugin, "_handle_job", side_effect=block_job):
            await self.plugin.receive_role_message(message=self.message("小花小花我要看排队"))
            await asyncio.wait_for(started.wait(), timeout=3)
            for index in range(self.plugin._queue.maxsize):
                await self.plugin.receive_role_message(message=self.message("小花小花我要看角色" + str(index)))
            self.rpc.fail_text_sends = 1
            with self.assertLogs(self.plugin.ctx.logger, level="ERROR"):
                result = await self.plugin.receive_role_message(message=self.message("小花小花我要看忙碌"))
            self.assertEqual(result["action"], "abort")
            release.set()
            await asyncio.wait_for(self.plugin._queue.join(), timeout=3)

    async def test_drawing_commands_generate_through_api_for_three_kinds(self) -> None:
        reference = png("white")
        for name in ("角色A", "角色B", "角色C"):
            await self.submit(self.message(f"小花小花这是 {name}", [png("red")]))
        self.set_drawing_api()
        settings = self.plugin._settings_store.current()
        settings.drawing.at_replies = ["completion_reply"]
        self.plugin._settings_store.save(settings)
        generated = png("blue")
        output = Path(self.temporary.name) / "generated.png"

        with patch.object(
            PLUGIN_MODULE.OpenAIImageClient,
            "generate",
            return_value=PLUGIN_MODULE.DrawingResult(generated, "prompt"),
        ), patch.object(PLUGIN_MODULE.LittleFlowerDrawPlugin, "_save_drawing_image", return_value=output):
            self.clear_output()
            result = await self.submit(self.message("变角色A 红色外套", [reference]))
            self.assertEqual(result["action"], "abort")
            task = await self.current_task()
            finished = await self.wait_task(task.id)
            self.assertEqual(finished.status, "succeeded")
            settings = self.plugin._settings_store.current()
            self.assertEqual(
                self.rpc.texts,
                [
                    PLUGIN_MODULE.render_task_reply(settings.drawing.submitted_reply, task.id, 0),
                    PLUGIN_MODULE.render_task_reply(settings.drawing.completion_reply, task.id),
                ],
            )
            self.assertEqual(self.rpc.images, [generated])
            self.assertEqual(self.rpc.at_targets, ["20001"])

            self.clear_output()
            await self.submit(self.message("角色A角色B一起穿 同框合影", [reference]))
            task = await self.current_task()
            self.assertEqual((await self.wait_task(task.id)).status, "succeeded")
            self.assertEqual(
                self.rpc.texts,
                [
                    PLUGIN_MODULE.render_task_reply(settings.drawing.submitted_reply, task.id, 0),
                    PLUGIN_MODULE.render_task_reply(settings.drawing.completion_reply, task.id),
                ],
            )
            self.assertEqual(self.rpc.images, [generated])
            self.assertEqual(self.rpc.at_targets, ["20001"])

            self.clear_output()
            await self.submit(self.message("多人穿@角色A@角色B@角色C 游乐园", [reference]))
            task = await self.current_task()
            self.assertEqual((await self.wait_task(task.id)).status, "succeeded")
            self.assertEqual(
                self.rpc.texts,
                [
                    PLUGIN_MODULE.render_task_reply(settings.drawing.submitted_reply, task.id, 0),
                    PLUGIN_MODULE.render_task_reply(settings.drawing.completion_reply, task.id),
                ],
            )
            self.assertEqual(self.rpc.images, [generated])
            self.assertEqual(self.rpc.at_targets, ["20001"])

    async def test_private_completion_omits_at_and_still_succeeds(self) -> None:
        await self.submit(self.message("小花小花这是 私聊角色", [png("red")], group=""))
        self.set_drawing_api()
        settings = self.plugin._settings_store.current()
        settings.drawing.at_replies = ["completion_reply"]
        self.plugin._settings_store.save(settings)
        generated = png("blue")
        with patch.object(
            PLUGIN_MODULE.OpenAIImageClient,
            "generate",
            return_value=PLUGIN_MODULE.DrawingResult(generated, "prompt"),
        ), patch.object(PLUGIN_MODULE.LittleFlowerDrawPlugin, "_save_drawing_image", return_value=Path(self.temporary.name) / "private.png"):
            self.clear_output()
            await self.submit(self.message("变私聊角色 红色外套", [png("white")], group=""))
            task = await self.current_task()
            finished = await self.wait_task(task.id)
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(self.rpc.images, [generated])
        self.assertEqual(self.rpc.at_targets, [])

    async def test_free_drawing_uses_fallback_prompt_and_no_reference(self) -> None:
        self.set_drawing_api()
        generated = png("orange")
        with patch.object(
            PLUGIN_MODULE.OpenAIImageClient,
            "generate_text",
            return_value=PLUGIN_MODULE.DrawingResult(generated, "prompt"),
        ) as generate_text, patch.object(
            PLUGIN_MODULE.LittleFlowerDrawPlugin,
            "_save_drawing_image",
            return_value=Path(self.temporary.name) / "free.png",
        ):
            self.clear_output()
            await self.submit(self.message("小花画"))
            task = await self.current_task()
            finished = await self.wait_task(task.id)
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.kind, "free")
        self.assertEqual(finished.role_names, [])
        self.assertEqual(finished.final_prompt, "发挥创意")
        self.assertEqual(finished.reference_path, "")
        self.assertEqual(self.rpc.images, [generated])
        generate_text.assert_called_once()

    async def test_webui_drawing_models_queries_configured_endpoint(self) -> None:
        with patch.object(PLUGIN_MODULE, "fetch_models", return_value=["gpt-image-1", "flux-image"]):
            result = await self.plugin.webui_drawing_models("https://relay.test/v1", "secret")
        self.assertEqual(result["models"], ["gpt-image-1", "flux-image"])
        missing = await self.plugin.webui_drawing_models()
        self.assertEqual(missing["models"], [])
        self.assertIn("API URL", missing["error"])

    async def test_task_history_detail_retry_and_bulk_delete(self) -> None:
        reference = png("white")
        await self.submit(self.message("小花小花这是 角色A", [png("red")]))
        self.set_drawing_api()
        generated = png("blue")
        output = Path(self.temporary.name) / "history-output.png"
        with patch.object(
            PLUGIN_MODULE.OpenAIImageClient,
            "generate",
            return_value=PLUGIN_MODULE.DrawingResult(generated, "prompt"),
        ), patch.object(PLUGIN_MODULE.LittleFlowerDrawPlugin, "_save_drawing_image", return_value=output):
            await self.submit(self.message("变角色A 红色外套", [reference]))
            first = await self.current_task()
            await self.wait_task(first.id)
            history = await self.plugin.webui_task_history()
            self.assertEqual(history["total"], 1)
            detail = await self.plugin.webui_task_detail(first.id)
            self.assertEqual(detail["id"], first.id)
            self.assertIn("角色A", detail["role_names"])

            retried = await self.plugin.webui_task_retry(first.id)
            self.assertTrue(retried["retried"])
            await self.wait_task(retried["task_id"])
            self.assertEqual((await self.plugin.webui_task_history())["total"], 2)

            deleted = await self.plugin.webui_task_delete(json.dumps([first.id, retried["task_id"]]))
            self.assertEqual(deleted["deleted"], 2)
            self.assertEqual((await self.plugin.webui_task_history())["total"], 0)

    async def test_one_active_task_per_user_blocks_second_submission(self) -> None:
        reference = png("white")
        await self.submit(self.message("小花小花这是 角色A", [png("red")]))
        self.set_drawing_api()
        gate = threading.Event()

        def blocking(*_args: Any, **_kwargs: Any) -> Any:
            gate.wait(5)
            return PLUGIN_MODULE.DrawingResult(png("blue"), "prompt")

        with patch.object(PLUGIN_MODULE.OpenAIImageClient, "generate", side_effect=blocking), patch.object(
            PLUGIN_MODULE.LittleFlowerDrawPlugin, "_save_drawing_image", return_value=Path(self.temporary.name) / "x.png"
        ):
            await self.submit(self.message("变角色A 红色外套", [reference]))
            first = await self.current_task()
            for _ in range(100):
                running = await asyncio.to_thread(self.plugin._task_store.get, first.id)
                if running is not None and running.status == "running":
                    break
                await asyncio.sleep(0.02)
            self.clear_output()
            await self.submit(self.message("变角色A 蓝色外套", [reference]))
            self.assertEqual(
                self.rpc.texts,
                [PLUGIN_MODULE.render_task_reply(self.plugin._settings_store.current().drawing.active_task_reply, first.id)],
            )
            gate.set()
            await self.wait_task(first.id)

    async def test_webui_drawing_api_round_trip(self) -> None:
        config = await self.plugin.webui_drawing_api()
        self.assertFalse(config["configured"])
        config.update(
            {
                "base_url": "https://relay.test/v1",
                "api_key": "secret",
                "model": "gpt-image-1",
                "size": "1024x1024",
                "timeout_seconds": 120,
            }
        )
        saved = await self.plugin.webui_drawing_api_save(json.dumps(config, ensure_ascii=False))
        loaded = await self.plugin.webui_drawing_api()
        self.assertTrue(saved["saved"])
        self.assertTrue(loaded["configured"])
        self.assertEqual(loaded["endpoint"], "https://relay.test/v1/images/edits")
        invalid = await self.plugin.webui_drawing_api_save(json.dumps({"base_url": "ftp://bad"}, ensure_ascii=False))
        self.assertFalse(invalid["saved"])
        self.assertIn("http", invalid["error"])

    def test_drawing_image_is_saved_under_plugin_drawings_directory(self) -> None:
        with TemporaryDirectory() as directory:
            plugin_file = Path(directory) / "plugin.py"
            with patch.object(PLUGIN_MODULE, "__file__", str(plugin_file)):
                path = PLUGIN_MODULE.LittleFlowerDrawPlugin._save_drawing_image(png("blue"), "replace")
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent.parent, (plugin_file.parent / "drawings").resolve())
            self.assertEqual(path.suffix, ".png")

    async def test_task_records_requester_names_and_reference_preview(self) -> None:
        await self.submit(self.message("小花小花这是 命名角色", [png("red")]))
        self.set_drawing_api()
        self.clear_output()
        with patch.object(
            PLUGIN_MODULE.OpenAIImageClient,
            "generate",
            side_effect=PLUGIN_MODULE.DrawingApiError("测试失败"),
        ):
            await self.submit(self.message("变命名角色 红色外套", [png("white")]))
            task = await self.current_task()
            self.assertEqual(task.requester_group_name, "测试群名片")
            self.assertEqual(task.requester_qq_name, "测试群友")
            detail = await self.plugin.webui_task_detail(task.id)
            self.assertEqual(detail["requester_group_name"], "测试群名片")
            self.assertEqual(detail["requester_qq_name"], "测试群友")
            preview = await self.plugin.webui_task_thumbnail(task.id, kind="reference", size="detail")
            self.assertTrue(preview["image"].startswith("data:image/webp;base64,"))

    async def test_drawing_reports_missing_or_failed_api_configuration(self) -> None:
        reference = png("white")
        await self.submit(self.message("小花小花这是 角色A", [png("red")]))
        drawing = self.plugin._settings_store.current().drawing
        self.clear_output()

        await self.submit(self.message("变角色A 红色外套", [reference]))
        self.assertEqual(self.rpc.texts, [drawing.api_missing_reply[0]])

        self.set_drawing_api()
        self.set_policy("单角色替换", limit=1)
        self.clear_output()
        with patch.object(
            PLUGIN_MODULE.OpenAIImageClient,
            "generate",
            side_effect=PLUGIN_MODULE.DrawingApiError("上游失败"),
        ):
            await self.submit(self.message("变角色A 红色外套", [reference]))
            task = await self.current_task()
            finished = await self.wait_task(task.id)
        self.assertEqual(finished.status, "failed")
        for _ in range(100):
            finished = await asyncio.to_thread(self.plugin._task_store.get, task.id)
            if finished is not None and finished.quota_refunded:
                break
            await asyncio.sleep(0.02)
        self.assertIsNone(await asyncio.to_thread(self.plugin._guard.check_quota, "单角色替换", "20001"))
        for _ in range(100):
            if len(self.rpc.texts) >= 2:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(
            self.rpc.texts,
            [
                PLUGIN_MODULE.render_task_reply(drawing.submitted_reply, task.id, 0),
                PLUGIN_MODULE.render_task_fail_reply(drawing.task_failed_reply, task.id, "上游失败"),
            ],
        )

    async def test_api_400_uses_short_image_rejected_reply(self) -> None:
        await self.submit(self.message("小花小花这是 不合规角色", [png("red")]))
        self.set_drawing_api()
        self.clear_output()
        long_detail = "上游返回了很长的错误详情" * 100
        with patch.object(
            PLUGIN_MODULE.OpenAIImageClient,
            "generate",
            side_effect=PLUGIN_MODULE.DrawingApiError(long_detail, status_code=400),
        ):
            await self.submit(self.message("变不合规角色 红色外套", [png("white")]))
            task = await self.current_task()
            finished = await self.wait_task(task.id)
            for _ in range(100):
                if len(self.rpc.texts) >= 2:
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(finished.status, "failed")
        self.assertEqual(finished.error, "图片不符合接口要求")
        self.assertEqual(
            self.rpc.texts[-1],
            PLUGIN_MODULE.render_task_reply(
                self.plugin._settings_store.current().drawing.image_rejected_reply, task.id
            ),
        )
        self.assertNotIn("很长的错误详情", self.rpc.texts[-1])

    async def test_drawing_rejects_missing_reference_missing_role_and_bad_group_count(self) -> None:
        await self.submit(self.message("小花小花这是 角色A", [png("red")]))
        await self.submit(self.message("小花小花这是 角色B", [png("red")]))
        self.clear_output()
        drawing = self.plugin._settings_store.current().drawing

        await self.submit(self.message("变角色A 红色外套"))
        self.assertEqual(self.rpc.texts, [drawing.missing_reference_reply[0]])

        self.clear_output()
        await self.submit(self.message("变不认识 红色外套", [png("white")]))
        self.assertEqual(self.rpc.texts, [drawing.missing_role_reply[0].format(角色名="不认识")])

        self.clear_output()
        await self.submit(self.message("多人穿@角色A@角色B 游乐园", [png("white")]))
        self.assertEqual(
            self.rpc.texts,
            [PLUGIN_MODULE.render_drawing_count_reply(drawing.group_count_reply, drawing.min_group_roles, drawing.max_group_roles)],
        )

    async def test_webui_settings_round_trip_applies_immediately(self) -> None:
        settings = await self.plugin.webui_settings()
        settings["access"]["whitelist_enabled"] = True
        settings["access"]["group_whitelist"] = ["10001"]
        settings["access"]["private_whitelist"] = ["20001"]
        settings["commands"]["policies"][0]["permission"] = "仅管理员"
        settings["library"]["max_image_mb"] = 12

        saved = await self.plugin.webui_settings_save(json.dumps(settings, ensure_ascii=False))
        loaded = await self.plugin.webui_settings()

        self.assertTrue(saved["saved"])
        self.assertTrue(loaded["access"]["whitelist_enabled"])
        self.assertEqual(loaded["commands"]["policies"][0]["permission"], "仅管理员")
        self.assertEqual(loaded["library"]["max_image_mb"], 12)
        self.assertTrue(self.plugin._settings_store.path.is_file())

    async def test_webui_settings_save_accepts_partial_payload_and_preserves_replies(self) -> None:
        before = await self.plugin.webui_settings()
        saved = await self.plugin.webui_settings_save(
            json.dumps({"library": {"max_image_mb": 9}}, ensure_ascii=False)
        )
        loaded = await self.plugin.webui_settings()
        self.assertTrue(saved["saved"])
        self.assertEqual(loaded["library"]["max_image_mb"], 9)
        self.assertEqual(loaded["replies"], before["replies"])
        self.assertEqual(loaded["drawing"]["completion_reply"], before["drawing"]["completion_reply"])

    async def test_webui_settings_reject_invalid_payload(self) -> None:
        invalid = await self.plugin.webui_settings_save("{")
        self.assertFalse(invalid["saved"])
        self.assertIn("JSON", invalid["error"])
        settings = await self.plugin.webui_settings()
        settings["access"]["admin_qq"] = "bad"
        invalid = await self.plugin.webui_settings_save(json.dumps(settings))
        self.assertFalse(invalid["saved"])
        self.assertIn("管理员", invalid["error"])

    async def test_webui_lists_and_serves_detail_and_thumbnail(self) -> None:
        image = png("red")
        await self.submit(self.message("小花小花这是 角色甲", [image]))

        listing = await self.plugin.webui_roles()

        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["register_template"], "小花小花这是 {角色名}")
        self.assertEqual([item["name"] for item in listing["items"]], ["角色甲"])
        detail = await self.plugin.webui_role_detail("角色甲")
        self.assertTrue(detail["preview"].startswith("data:image/webp;base64,"))
        thumbnail = await self.plugin.webui_role_thumbnail("角色甲")
        self.assertTrue(thumbnail["image"].startswith("data:image/webp;base64,"))

    async def test_webui_rename_and_delete_keep_the_store_consistent(self) -> None:
        await self.submit(self.message("小花小花这是 角色甲", [png("red")]))

        renamed = await self.plugin.webui_role_rename("角色甲", "角色乙")

        self.assertTrue(renamed["renamed"])
        self.assertEqual(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "角色乙"), png("red"))
        await self.plugin.webui_role_delete("角色乙")
        self.assertIsNone(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "角色乙"))
        with self.assertRaisesRegex(ValueError, "角色不存在"):
            await self.plugin.webui_role_delete("角色乙")

    async def test_webui_upload_creates_and_replaces_a_role_then_removes_the_temp_file(self) -> None:
        uploads = self.plugin.ctx.paths.data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        uploaded = uploads / "first.png"
        uploaded.write_bytes(png("blue"))

        with patch.object(self.plugin.ctx.webui, "claim_upload", AsyncMock(return_value={"path": str(uploaded)})):
            created = await self.plugin.webui_role_set_image("upload-1", "上传角色")

        self.assertTrue(created["saved"])
        self.assertFalse(created["overwritten"])
        self.assertFalse(uploaded.exists())
        self.assertEqual(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "上传角色"), png("blue"))
        self.assertEqual(self.plugin._store.get_owner(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "上传角色"), "webui")

        replacement = uploads / "second.png"
        replacement.write_bytes(png("green"))
        with patch.object(self.plugin.ctx.webui, "claim_upload", AsyncMock(return_value={"path": str(replacement)})):
            replaced = await self.plugin.webui_role_set_image("upload-2", "上传角色")

        self.assertTrue(replaced["overwritten"])
        self.assertFalse(replacement.exists())
        self.assertEqual(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "上传角色"), png("green"))
        self.assertEqual(self.plugin._store.get_owner(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "上传角色"), "webui")

    async def test_webui_upload_rejects_invalid_image_and_still_removes_the_temp_file(self) -> None:
        uploads = self.plugin.ctx.paths.data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        uploaded = uploads / "broken.png"
        uploaded.write_bytes(b"not-an-image")

        with patch.object(self.plugin.ctx.webui, "claim_upload", AsyncMock(return_value={"path": str(uploaded)})):
            result = await self.plugin.webui_role_set_image("upload-bad", "坏上传")

        self.assertFalse(result["saved"])
        self.assertIn("损坏", result["error"])
        self.assertFalse(uploaded.exists())
        self.assertIsNone(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "坏上传"))

    async def test_webui_upload_rejects_a_path_outside_the_upload_directory(self) -> None:
        outside = self.plugin.ctx.paths.data_dir / "outside.png"
        outside.write_bytes(png("red"))

        with patch.object(self.plugin.ctx.webui, "claim_upload", AsyncMock(return_value={"path": str(outside)})):
            with self.assertRaisesRegex(RuntimeError, "上传目录"):
                await self.plugin.webui_role_set_image("upload-outside", "越界")

    async def test_whitelist_denies_group_before_queueing(self) -> None:
        self.set_access(whitelist_enabled=True, group_whitelist=["99999"], private_whitelist=["20001"])

        result = await self.submit(self.message("小花小花这是 白名单外", [png("red")], group="10001"))

        self.assertEqual(result["action"], "abort")
        self.assertEqual(self.rpc.texts, [self.guard_replies().whitelist_denied[0]])
        self.assertIsNone(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "白名单外"))

    async def test_admin_bypasses_empty_whitelist(self) -> None:
        self.set_access(whitelist_enabled=True, group_whitelist=[], private_whitelist=[])

        await self.submit(self.message("小花小花这是 管理员角色", [png("red")], user="2685275264"))

        self.assertEqual(self.rpc.texts, [self.reply("registered").format(角色名="管理员角色")])
        self.assertIsNotNone(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "管理员角色"))

    async def test_admin_only_command_rejects_normal_users(self) -> None:
        self.set_policy("登记角色", permission="仅管理员")

        await self.submit(self.message("小花小花这是 普通角色", [png("red")], user="20001"))
        self.assertEqual(self.rpc.texts, [self.guard_replies().admin_only[0]])
        self.assertIsNone(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "普通角色"))
        self.clear_output()

        await self.submit(self.message("小花小花这是 管理员角色", [png("red")], user="2685275264"))
        self.assertEqual(self.rpc.texts, [self.reply("registered").format(角色名="管理员角色")])

    async def test_success_only_quota_counts_and_blocks_later_commands(self) -> None:
        self.set_policy("登记角色", limit=1)

        await self.submit(self.message("小花小花这是 第一次", [png("red")], user="20001"))
        self.assertEqual(self.rpc.texts, [self.reply("registered").format(角色名="第一次")])
        self.clear_output()

        await self.submit(self.message("小花小花这是 第二次", [png("blue")], user="20001"))
        self.assertIn("次数用完", self.rpc.texts[-1])
        self.assertIsNone(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "第二次"))

    async def test_failed_registration_does_not_consume_quota(self) -> None:
        self.set_policy("登记角色", limit=1)

        await self.submit(self.message("小花小花这是 先失败", [], user="20001"))
        self.assertEqual(self.rpc.texts, [self.reply("missing_image").format(角色名="先失败")])
        self.clear_output()

        await self.submit(self.message("小花小花这是 再成功", [png("green")], user="20001"))
        self.assertEqual(self.rpc.texts, [self.reply("registered").format(角色名="再成功")])
        self.assertIsNotNone(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "再成功"))

    async def test_unload_waits_for_an_in_flight_database_transaction(self) -> None:
        started = threading.Event()
        release = threading.Event()
        original_save = self.plugin._store.save
        image = png("red")

        def slow_save(*args: Any) -> bool:
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError("测试未释放图库写入")
            return original_save(*args)

        try:
            with patch.object(self.plugin._store, "save", side_effect=slow_save):
                await self.plugin.receive_role_message(message=self.message("小花小花这是 卸载中的角色", [image]))
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                unloading = asyncio.create_task(self.plugin.on_unload())
                await asyncio.sleep(0.02)
                self.assertFalse(unloading.done())
                release.set()
                await asyncio.wait_for(unloading, timeout=3)
        finally:
            release.set()
        self.assertEqual(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "卸载中的角色"), image)
        self.assertFalse(self.plugin._pending_writes)

    async def test_unload_cancels_active_work_and_drains_pending_jobs(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def block_job(job: Any, stream_id: str) -> None:
            started.set()
            await release.wait()

        with patch.object(self.plugin, "_handle_job", side_effect=block_job):
            await self.plugin.receive_role_message(message=self.message("小花小花我要看角色一"))
            await asyncio.wait_for(started.wait(), timeout=3)
            await self.plugin.receive_role_message(message=self.message("小花小花我要看角色二"))
            worker = self.plugin._worker
            await self.plugin.on_unload()
            await asyncio.wait_for(self.plugin._queue.join(), timeout=1)

        self.assertIsNone(self.plugin._worker)
        self.assertTrue(worker.cancelled())
        self.assertTrue(self.plugin._queue.empty())
        self.assertFalse(self.plugin._seen_messages)


if __name__ == "__main__":
    unittest.main()
