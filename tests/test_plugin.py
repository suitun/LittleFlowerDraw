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
        self.history: Dict[str, Dict[str, Any]] = {}
        self.fail_text_sends = 0
        self.fail_image_sends = 0

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
        if capability == "message.get_by_id":
            return {"success": True, "message": self.history.get(args["message_id"])}
        if capability == "api.get":
            return {"success": True, "api": None}
        raise AssertionError("测试未声明的宿主调用：" + capability)


class PluginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
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
        await self.plugin.on_load()
        self.addAsyncCleanup(self.plugin.on_unload)
        self.message_number = 0

    def message(
        self, text: str, images: Optional[List[bytes]] = None, *, group: str = "10001",
        user: str = "20001", message_id: str = "", reply_to: str = "",
    ) -> Dict[str, Any]:
        self.message_number += 1
        segments: List[Dict[str, Any]] = []
        if reply_to:
            segments.append({"type": "reply", "data": {"target_message_id": reply_to}})
        segments.append({"type": "text", "data": text})
        for image in images or []:
            segments.append({"type": "image", "data": "", "binary_data_base64": base64.b64encode(image).decode("ascii")})
        info: Dict[str, Any] = {"user_info": {"user_id": user, "user_nickname": "测试群友"}}
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
        self.rpc.calls.clear()

    async def test_first_message_opens_real_target_chat_and_keeps_only_the_first_image(self) -> None:
        images = [png("red"), png("green"), png("blue")]
        result = await self.submit(self.message("小花小花这是 小花", images))

        self.assertEqual(result["action"], "abort")
        self.assertEqual(self.rpc.calls[0]["capability"], "chat.open_session")
        open_args = self.rpc.calls[0]["args"]
        self.assertEqual((open_args["chat_type"], open_args["group_id"], open_args["account_id"]), ("group", "10001", "99999"))
        self.assertEqual(self.rpc.texts, [self.plugin.config.replies.registered.format(角色名="小花")])

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
        self.assertEqual(self.rpc.texts, [self.plugin.config.replies.overwritten.format(角色名="花花")])
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
                expected = self.config["replies"][reply_field].format(角色名=name)
                self.assertEqual(self.rpc.texts, [expected])
                self.assertFalse(self.rpc.images)
                self.clear_output()
                await self.submit(self.message("小花小花我要看" + name))
                self.assertEqual(self.rpc.texts, [self.config["replies"]["not_found"].format(角色名=name)])
                self.assertFalse(self.rpc.images)

    async def test_hot_config_changes_both_matching_and_replies_without_reloading(self) -> None:
        image = png("yellow")
        await self.submit(self.message("小花小花这是 原角色", [image]))
        self.clear_output()
        worker = self.plugin._worker
        config = deepcopy(self.config)
        config["keywords"]["register_templates"] = ["记住 {角色名}"]
        config["keywords"]["view_templates"] = ["看看{角色名}"]
        config["replies"]["registered"] = "收好{角色名}啦喵！"
        config["replies"]["view"] = ""
        self.plugin.set_plugin_config(config)
        await self.plugin.on_config_update("self", config, "test-update")

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
        self.assertEqual(self.rpc.texts, [self.config["replies"]["invalid_name"]])

    async def test_bot_own_messages_do_not_trigger_configured_replies(self) -> None:
        result = await self.submit(self.message("小花小花这是 自己", [png("red")], user="99999"))
        self.assertEqual(result["action"], "continue")
        self.assertFalse(self.rpc.calls)

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
        self.assertEqual(self.rpc.texts, [self.config["replies"]["invalid_image"]])
        self.clear_output()
        await self.submit(self.message("小花小花我要看红花"))
        self.assertEqual(self.rpc.images, [image])

    async def test_store_failure_is_logged_and_replied_as_an_error(self) -> None:
        with patch.object(self.plugin._store, "save", side_effect=OSError("模拟磁盘写入失败")):
            with self.assertLogs(self.plugin.ctx.logger, level="ERROR") as logs:
                await self.submit(self.message("小花小花这是 失败角色", [png("red")]))
        self.assertIn("模拟磁盘写入失败", "\n".join(logs.output))
        self.assertEqual(self.rpc.texts, [self.config["replies"]["error"]])
        self.clear_output()
        await self.submit(self.message("小花小花我要看失败角色"))
        self.assertEqual(self.rpc.texts, [self.config["replies"]["not_found"].format(角色名="失败角色")])
        self.assertFalse(self.rpc.images)

    async def test_failed_confirmation_is_logged_and_does_not_claim_delivery(self) -> None:
        self.rpc.fail_text_sends = 1
        with self.assertLogs(self.plugin.ctx.logger, level="ERROR") as logs:
            await self.submit(self.message("小花小花这是 红花", [png("red")]))
        self.assertIn("文字回复发送失败", "\n".join(logs.output))
        self.assertEqual(self.rpc.texts, [self.config["replies"]["error"]])

    async def test_failed_image_send_is_logged_and_gets_an_error_reply(self) -> None:
        await self.submit(self.message("小花小花这是 红花", [png("red")]))
        self.clear_output()
        self.rpc.fail_image_sends = 1
        with self.assertLogs(self.plugin.ctx.logger, level="ERROR") as logs:
            await self.submit(self.message("小花小花我要看红花"))
        self.assertIn("角色图片发送失败", "\n".join(logs.output))
        self.assertEqual(self.rpc.texts[-1], self.config["replies"]["error"])
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
            with self.assertRaises(ValueError):
                await self.plugin.webui_role_set_image("upload-bad", "坏上传")

        self.assertFalse(uploaded.exists())
        self.assertIsNone(self.plugin._store.get(PLUGIN_MODULE.GLOBAL_LIBRARY_SCOPE, "坏上传"))

    async def test_webui_upload_rejects_a_path_outside_the_upload_directory(self) -> None:
        outside = self.plugin.ctx.paths.data_dir / "outside.png"
        outside.write_bytes(png("red"))

        with patch.object(self.plugin.ctx.webui, "claim_upload", AsyncMock(return_value={"path": str(outside)})):
            with self.assertRaisesRegex(RuntimeError, "上传目录"):
                await self.plugin.webui_role_set_image("upload-outside", "越界")

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
