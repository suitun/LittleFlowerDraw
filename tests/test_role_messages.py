"""角色消息的真实图片验证、引用解析与有界下载测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch
from urllib.error import HTTPError, URLError

import base64
import io
import unittest

from PIL import Image

import role_messages
from role_messages import ImageInputError, ImageLimits, MessageImageResolver, MissingImageError, get_message_text


def image_bytes(color: str = "red", image_format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    with Image.new("RGB", (2, 2), color) as image:
        image.save(buffer, format=image_format)
    return buffer.getvalue()


def image_segment(data: bytes) -> dict:
    return {"type": "image", "binary_data_base64": base64.b64encode(data).decode("ascii"), "data": ""}


def quoted_message() -> dict:
    return {
        "message_info": {"group_info": {"group_id": "123"}},
        "raw_message": [
            {"type": "reply", "data": {"target_message_id": "-42", "target_message_content": "引用预览"}},
            {"type": "text", "data": "小花小花这是 测试角色"},
        ],
    }


def fake_context() -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(get_by_id=AsyncMock(return_value=None)),
        api=SimpleNamespace(get=AsyncMock(return_value={"enabled": True}), call=AsyncMock(return_value=None)),
    )


class MessageTextTests(unittest.TestCase):
    def test_only_top_level_text_is_part_of_the_trigger(self) -> None:
        message = quoted_message()
        message["raw_message"].extend([
            {"type": "image", "data": "图片描述"},
            {"type": "text", "data": {"text": " 后半句"}},
        ])
        self.assertEqual(get_message_text(message), "小花小花这是 测试角色 后半句")
        self.assertEqual(get_message_text({"processed_plain_text": "引用描述"}), "")

    def test_onebot_text_array_is_supported(self) -> None:
        message = {"message": [{"type": "text", "data": {"text": "前半句"}}, {"type": "text", "data": "后半句"}]}
        self.assertEqual(get_message_text(message), "前半句后半句")

    def test_cq_escape_sequences_and_literal_host_text_are_kept_separate(self) -> None:
        segments = role_messages._message_segments({"message": "[CQ:image,file=opaque&#44;id,url=https://gchat.qpic.cn/a?x=1&amp;y=2]"})
        self.assertEqual(segments[0]["data"], {"file": "opaque,id", "url": "https://gchat.qpic.cn/a?x=1&y=2"})
        host = {"raw_message": [{"type": "text", "data": "[CQ:image,file=not-a-real-image]"}]}
        self.assertEqual(get_message_text(host), "[CQ:image,file=not-a-real-image]")
        self.assertEqual(role_messages._image_segments(host), [])


class ImageResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ctx = fake_context()
        self.resolver = MessageImageResolver(self.ctx, ImageLimits())
        self.red = image_bytes("red")
        self.blue = image_bytes("blue")

    async def test_attached_images_override_quoted_image_and_are_deduplicated(self) -> None:
        message = quoted_message()
        message["raw_message"].extend([image_segment(self.red), image_segment(self.blue), image_segment(self.red)])
        self.assertEqual(await self.resolver.resolve(message, "chat"), [self.red, self.blue])
        self.ctx.message.get_by_id.assert_not_awaited()
        self.ctx.api.call.assert_not_awaited()

    async def test_duplicate_images_do_not_consume_extra_slots(self) -> None:
        resolver = MessageImageResolver(self.ctx, ImageLimits(max_images=1))
        self.assertEqual(await resolver.resolve({"raw_message": [image_segment(self.red)] * 7}, "chat"), [self.red])

    async def test_history_query_requests_binary_data_and_current_stream(self) -> None:
        self.ctx.message.get_by_id.return_value = {"session_id": "chat", "raw_message": [image_segment(self.red)]}
        self.assertEqual(await self.resolver.resolve(quoted_message(), "chat"), [self.red])
        self.ctx.message.get_by_id.assert_awaited_once_with("-42", stream_id="chat", include_binary_data=True)
        self.ctx.api.call.assert_not_awaited()

    async def test_unseen_quote_is_read_through_snowluma_public_api(self) -> None:
        self.ctx.api.call.return_value = {
            "group_id": 123,
            "message": [{"type": "image", "data": {"file": "base64://" + base64.b64encode(self.red).decode()}}],
        }
        self.assertEqual(await self.resolver.resolve(quoted_message(), "chat"), [self.red])
        self.ctx.api.get.assert_awaited_once_with("adapter.snowluma.message.get_msg", version="1")
        self.ctx.api.call.assert_awaited_once_with("adapter.snowluma.message.get_msg", version="1", message_id="-42")

    async def test_quoted_snowluma_cq_string_can_supply_an_image(self) -> None:
        encoded = base64.b64encode(self.red).decode("ascii")
        self.ctx.api.call.return_value = {"group_id": 123, "message": f"原消息[CQ:image,file=base64://{encoded}]"}
        self.assertEqual(await self.resolver.resolve(quoted_message(), "chat"), [self.red])

    async def test_history_without_stored_binary_uses_the_public_message_api(self) -> None:
        self.ctx.message.get_by_id.return_value = {"session_id": "chat", "raw_message": [{"type": "image", "data": "描述"}]}
        self.ctx.api.call.return_value = {"group_id": 123, "message": [image_segment(self.red)]}
        self.assertEqual(await self.resolver.resolve(quoted_message(), "chat"), [self.red])

    async def test_get_image_can_read_a_trusted_absolute_path(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reference.png"
            path.write_bytes(self.red)
            self.ctx.api.call.side_effect = [
                {"group_id": 123, "message": [{"type": "image", "data": {"file": "opaque-image-id"}}]},
                {"status": "ok", "retcode": 0, "data": {"file": str(path)}},
            ]
            self.assertEqual(await self.resolver.resolve(quoted_message(), "chat"), [self.red])
        self.assertEqual(self.ctx.api.call.await_args_list, [
            call("adapter.snowluma.message.get_msg", version="1", message_id="-42"),
            call("adapter.snowluma.file.get_image", version="1", params={"file": "opaque-image-id"}),
        ])

    async def test_an_incoming_absolute_path_is_not_opened(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "private.png"
            path.write_bytes(self.red)
            with self.assertRaises(MissingImageError):
                await self.resolver.resolve({"raw_message": [{"type": "image", "data": {"file": str(path)}}]}, "chat")
        self.ctx.api.call.assert_not_awaited()

    async def test_cdn_images_are_downloaded_then_validated(self) -> None:
        url = "https://gchat.qpic.cn/image?rkey=temporary"
        with patch.object(role_messages, "_download_image", return_value=self.red) as download:
            result = await self.resolver.resolve({"message": [{"type": "image", "data": {"url": url}}]}, "chat")
        self.assertEqual(result, [self.red])
        download.assert_called_once_with(url, self.resolver.limits.max_image_bytes)

    async def test_expired_cdn_url_can_be_refreshed_with_get_image(self) -> None:
        self.ctx.api.call.return_value = {"status": "ok", "retcode": 0, "data": {"base64": base64.b64encode(self.red).decode()}}
        with patch.object(role_messages, "_download_image", side_effect=role_messages._ImageFetchError("无法下载")):
            result = await self.resolver.resolve({"message": [{"type": "image", "data": {
                "url": "https://gchat.qpic.cn/old", "file": "opaque-id",
            }}]}, "chat")
        self.assertEqual(result, [self.red])
        self.ctx.api.call.assert_awaited_once_with("adapter.snowluma.file.get_image", version="1", params={"file": "opaque-id"})

    async def test_remote_client_local_path_can_use_its_cdn_url(self) -> None:
        self.ctx.api.call.return_value = {"status": "ok", "retcode": 0, "data": {
            "file": str(Path.cwd() / "nonexistent-role-image.png"), "url": "https://gchat.qpic.cn/reference",
        }}
        with patch.object(role_messages, "_download_image", return_value=self.red):
            result = await self.resolver.resolve({"message": [{"type": "image", "data": {"file": "opaque-id"}}]}, "chat")
        self.assertEqual(result, [self.red])

    async def test_missing_or_old_quote_reports_a_readable_error(self) -> None:
        self.ctx.api.get.return_value = None
        with self.assertRaisesRegex(MissingImageError, "QQ 连接器"):
            await self.resolver.resolve(quoted_message(), "chat")
        self.ctx.api.call.assert_not_awaited()

    async def test_unavailable_quoted_message_reports_a_missing_image(self) -> None:
        with self.assertRaisesRegex(MissingImageError, "引用消息"):
            await self.resolver.resolve(quoted_message(), "chat")

    async def test_nested_reply_is_not_followed(self) -> None:
        self.ctx.api.call.return_value = {"group_id": 123, "message": [{"type": "reply", "data": {"id": 99}}]}
        with self.assertRaises(MissingImageError):
            await self.resolver.resolve(quoted_message(), "chat")
        self.assertEqual(self.ctx.api.call.await_count, 1)

    async def test_cross_chat_history_and_raw_quote_are_rejected(self) -> None:
        self.ctx.message.get_by_id.return_value = {"session_id": "other-chat", "raw_message": [image_segment(self.red)]}
        with self.assertRaisesRegex(ImageInputError, "当前聊天"):
            await self.resolver.resolve(quoted_message(), "chat")
        self.ctx.message.get_by_id.return_value = None
        self.ctx.api.call.return_value = {"group_id": 999, "message": [image_segment(self.red)]}
        with self.assertRaisesRegex(ImageInputError, "当前聊天"):
            await self.resolver.resolve(quoted_message(), "chat")

    async def test_api_failures_are_not_treated_as_message_or_image_payloads(self) -> None:
        self.ctx.api.call.return_value = {"success": False, "error": "https://gchat.qpic.cn/?rkey=secret"}
        with self.assertRaises(ImageInputError) as error:
            await self.resolver.resolve(quoted_message(), "chat")
        self.assertNotIn("secret", str(error.exception))
        self.ctx.api.call.return_value = {"status": "failed", "retcode": 100, "data": {}}
        with self.assertRaisesRegex(ImageInputError, "QQ 客户端"):
            await self.resolver.resolve({"message": [{"type": "image", "data": {"file": "opaque-id"}}]}, "chat")

    async def test_transport_exception_does_not_expose_url_credentials(self) -> None:
        self.ctx.api.call.side_effect = RuntimeError("https://gchat.qpic.cn/?rkey=secret")
        with self.assertRaises(ImageInputError) as error:
            await self.resolver.resolve(quoted_message(), "chat")
        self.assertNotIn("secret", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    async def test_image_count_and_total_size_are_rejected_without_truncation(self) -> None:
        message = {"raw_message": [image_segment(self.red), image_segment(self.blue)]}
        for limits in (
            ImageLimits(max_images=1),
            ImageLimits(max_total_bytes=len(self.red) + len(self.blue) - 1),
        ):
            with self.subTest(limits=limits), self.assertRaises(ImageInputError):
                await MessageImageResolver(self.ctx, limits).resolve(message, "chat")

    async def test_individual_size_pixel_limit_format_and_corruption_are_rejected(self) -> None:
        cases = [
            (self.red, ImageLimits(max_image_bytes=len(self.red) - 1)),
            (self.red, ImageLimits(max_pixels=3)),
            (image_bytes(image_format="BMP"), ImageLimits()),
            (b"this is not an image", ImageLimits()),
            (self.red[:-8], ImageLimits()),
        ]
        for data, limits in cases:
            with self.subTest(size=len(data), limits=limits), self.assertRaises(ImageInputError):
                await MessageImageResolver(self.ctx, limits).resolve({"raw_message": [image_segment(data)]}, "chat")

    async def test_corrupt_base64_is_not_replaced_with_a_quoted_image(self) -> None:
        message = quoted_message()
        message["raw_message"].append({"type": "image", "binary_data_base64": "not base64!"})
        with self.assertRaises(ImageInputError):
            await self.resolver.resolve(message, "chat")
        self.ctx.message.get_by_id.assert_not_awaited()

    async def test_supported_image_formats_are_preserved(self) -> None:
        for image_format in ("PNG", "JPEG", "WEBP", "GIF"):
            with self.subTest(image_format=image_format):
                data = image_bytes(image_format=image_format)
                self.assertEqual(await self.resolver.resolve({"raw_message": [image_segment(data)]}, "chat"), [data])


class _Response:
    def __init__(self, data: bytes, headers: dict | None = None) -> None:
        self.body = io.BytesIO(data)
        self.headers = headers or {}
        self.read_calls = 0

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.body.close()

    def read(self, length: int) -> bytes:
        self.read_calls += 1
        return self.body.read(length)


class DownloadBoundaryTests(unittest.TestCase):
    def test_only_qq_image_cdn_hosts_and_standard_ports_are_allowed(self) -> None:
        for url in (
            "https://gchat.qpic.cn/a", "http://qpic.cn:80/a", "https://image.qq.com:443/a",
            "https://multimedia.nt.qq.com.cn/download?rkey=temporary",
        ):
            with self.subTest(url=url):
                self.assertEqual(role_messages._validate_image_url(url), url)
        for url in (
            "https://evil.qq.com.cn/a", "https://qq.com.evil/a", "https://qpic.cn.evil/a",
            "https://qq.com@evil.example/a", "https://user:pass@qq.com/a", "http://127.0.0.1/a",
            "http://localhost/a", "file:///C:/private.png", "https://qpic.cn:8080/a", "https://qpic.cn:0/a",
        ):
            with self.subTest(url=url), self.assertRaises(ImageInputError):
                role_messages._validate_image_url(url)

    def test_allowed_redirect_is_checked_and_downloaded(self) -> None:
        image = image_bytes()
        start = "https://gchat.qpic.cn/a?rkey=secret"
        target = "https://multimedia.nt.qq.com.cn/b?rkey=temporary"
        redirect = HTTPError(start, 302, "Moved", {"Location": target}, io.BytesIO())
        opener = Mock()
        opener.open.side_effect = [redirect, _Response(image, {"Content-Length": str(len(image))})]
        with patch.object(role_messages, "build_opener", return_value=opener):
            self.assertEqual(role_messages._download_image(start, 1024), image)
        self.assertEqual([entry.args[0].full_url for entry in opener.open.call_args_list], [start, target])

    def test_redirect_to_an_untrusted_host_is_never_requested(self) -> None:
        start = "https://gchat.qpic.cn/a?rkey=secret"
        opener = Mock()
        opener.open.side_effect = HTTPError(start, 302, "Moved", {"Location": "http://127.0.0.1/private"}, io.BytesIO())
        with patch.object(role_messages, "build_opener", return_value=opener), self.assertRaises(ImageInputError) as error:
            role_messages._download_image(start, 1024)
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn("secret", str(error.exception))

    def test_content_length_and_streamed_length_are_both_bounded(self) -> None:
        for response in (_Response(b"abc", {"Content-Length": "1000"}), _Response(b"a" * 11)):
            with self.subTest(headers=response.headers):
                opener = Mock()
                opener.open.return_value = response
                with patch.object(role_messages, "build_opener", return_value=opener), self.assertRaises(ImageInputError):
                    role_messages._download_image("https://gchat.qpic.cn/a", 10)

    def test_redirect_loops_have_a_finite_budget(self) -> None:
        url = "https://gchat.qpic.cn/loop"
        opener = Mock()
        opener.open.side_effect = [HTTPError(url, 302, "Moved", {"Location": url}, io.BytesIO()) for _ in range(4)]
        with patch.object(role_messages, "build_opener", return_value=opener), self.assertRaises(ImageInputError):
            role_messages._download_image(url, 1024)
        self.assertEqual(opener.open.call_count, 4)

    def test_truncated_download_is_rejected(self) -> None:
        opener = Mock()
        opener.open.return_value = _Response(b"abc", {"Content-Length": "10"})
        with patch.object(role_messages, "build_opener", return_value=opener), self.assertRaises(ImageInputError):
            role_messages._download_image("https://gchat.qpic.cn/a", 1024)

    def test_download_failures_hide_temporary_url_credentials(self) -> None:
        for failure in (
            HTTPError("https://gchat.qpic.cn/?rkey=secret", 404, "Missing", {}, io.BytesIO()),
            URLError("https://gchat.qpic.cn/?rkey=secret"),
            TimeoutError("https://gchat.qpic.cn/?rkey=secret"),
        ):
            with self.subTest(kind=type(failure)):
                opener = Mock()
                opener.open.side_effect = failure
                with patch.object(role_messages, "build_opener", return_value=opener), self.assertRaises(ImageInputError) as error:
                    role_messages._download_image("https://gchat.qpic.cn/?rkey=secret", 1024)
                self.assertNotIn("secret", str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)

    def test_unc_and_relative_local_paths_are_rejected(self) -> None:
        for reference in ("relative.png", "file://server/share/private.png", "\\\\server\\share\\private.png"):
            with self.subTest(reference=reference), self.assertRaises(ImageInputError):
                role_messages._read_local_image(reference, 1024)


if __name__ == "__main__":
    unittest.main()
