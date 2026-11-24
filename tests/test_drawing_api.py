import base64
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempdir import TemporaryDirectory
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError

from PIL import Image

from drawing_api import (
    DrawingApiConfig,
    DrawingApiError,
    DrawingApiRoute,
    DrawingApiStore,
    OpenAIImageClient,
    _bad_request_message,
    fetch_models,
    models_endpoint,
)


def png(width: int = 4, height: int = 3) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height), "red").save(stream, format="PNG")
    return stream.getvalue()


class DrawingApiTests(unittest.TestCase):
    def test_endpoint_accepts_base_or_full_url(self) -> None:
        self.assertEqual(
            DrawingApiConfig(base_url="https://relay.test/v1").endpoint,
            "https://relay.test/v1/images/edits",
        )
        self.assertEqual(
            DrawingApiConfig(base_url="https://relay.test/v1/images/edits").endpoint,
            "https://relay.test/v1/images/edits",
        )
        self.assertEqual(models_endpoint("https://relay.test/v1"), "https://relay.test/v1/models")
        self.assertEqual(
            models_endpoint("https://relay.test/v1/images/edits"),
            "https://relay.test/v1/models",
        )
        self.assertEqual(DrawingApiConfig().configured, False)

    def test_invalid_url_and_size_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DrawingApiConfig(base_url="ftp://relay.test")
        with self.assertRaises(ValueError):
            DrawingApiConfig(size="large")
        with self.assertRaises(ValueError):
            DrawingApiConfig(size="4096x4096")
        self.assertEqual(DrawingApiConfig(size="auto").size, "auto")
        self.assertEqual(DrawingApiConfig(size="reference").size, "reference")
        self.assertEqual(DrawingApiConfig(size="2048x2048").size, "2048x2048")
        self.assertEqual(DrawingApiConfig(size="3840x2160").size, "3840x2160")
        self.assertEqual(DrawingApiConfig(quality="high").quality, "high")
        self.assertEqual(DrawingApiConfig(complexity="medium").complexity, "medium")

    def test_fetch_models_reads_openai_model_list(self) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"data": [{"id": "gpt-image-1"}, {"id": "other-model"}, {"id": "gpt-image-1"}]}
        ).encode()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch("drawing_api.urlopen", return_value=response) as mocked:
            models = fetch_models("https://relay.test/v1", "secret")

        self.assertEqual(models, ["gpt-image-1", "other-model"])
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://relay.test/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    def test_store_round_trips_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            store = DrawingApiStore(Path(directory))
            loaded = store.load()
            self.assertFalse(loaded.configured)

            saved = store.save(
                DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-1")
            )
            self.assertTrue(saved.configured)
            reopened = DrawingApiStore(Path(directory))
            self.assertEqual(reopened.load().api_key, "secret")

    def test_store_supports_multiple_profiles_and_active_switch(self) -> None:
        with TemporaryDirectory() as directory:
            store = DrawingApiStore(Path(directory))
            store.load()
            first = DrawingApiConfig(
                profile_id="first", name="主力", base_url="https://first.test/v1", api_key="one", model="gpt-image-2"
            )
            second = DrawingApiConfig(
                profile_id="second", name="备用", base_url="https://second.test/v1", api_key="two", model="gpt-image-2.5"
            )
            store.save_profile(first)
            store.save_profile(second)
            store.set_active("second")
            self.assertEqual(store.current().model, "gpt-image-2.5")
            self.assertEqual([item.name for item in store.list_profiles()], ["默认 API", "主力", "备用"])
            reopened = DrawingApiStore(Path(directory))
            reopened.load()
            self.assertEqual(reopened.current().profile_id, "second")
            reopened.set_active("first")
            self.assertEqual(reopened.current().api_key, "one")
            reopened.delete_profile("second")
            self.assertEqual([item.profile_id for item in reopened.list_profiles()], ["default", "first"])

    def test_download_retries_without_authorization_after_403(self) -> None:
        image = png()
        response = MagicMock()
        response.read.return_value = image
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        forbidden = HTTPError(
            "https://relay.test/v1/files/result.png",
            403,
            "Forbidden",
            None,
            BytesIO(b"forbidden"),
        )
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-2.6")
        with patch("drawing_api.urlopen", side_effect=[forbidden, response]) as mocked:
            result = OpenAIImageClient()._decode_item(
                {"url": "https://relay.test/v1/files/result.png"},
                config,
                config.endpoint,
            )

        self.assertEqual(result, image)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(
            mocked.call_args_list[0].args[0].get_header("Authorization"),
            "Bearer secret",
        )
        self.assertIsNone(mocked.call_args_list[1].args[0].get_header("Authorization"))
        self.assertIn("Mozilla", mocked.call_args_list[1].args[0].get_header("User-agent"))

    def test_download_from_other_host_uses_browser_headers_without_token(self) -> None:
        image = png()
        response = MagicMock()
        response.read.return_value = image
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-2.6")
        with patch("drawing_api.urlopen", return_value=response) as mocked:
            result = OpenAIImageClient()._decode_item(
                {"url": "https://cdn.test/result.png"},
                config,
                config.endpoint,
            )

        self.assertEqual(result, image)
        request = mocked.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(request.get_header("Referer"), "https://relay.test/")
        self.assertIn("Mozilla", request.get_header("User-agent"))

    def test_store_routes_group_and_private_to_specific_profiles(self) -> None:
        with TemporaryDirectory() as directory:
            store = DrawingApiStore(Path(directory))
            store.load()
            first = DrawingApiConfig(profile_id="first", name="一组", base_url="https://a.test/v1", api_key="a", model="gpt-image-2")
            second = DrawingApiConfig(profile_id="second", name="二组", base_url="https://b.test/v1", api_key="b", model="gpt-image-2.6")
            store.save_profile(first)
            store.save_profile(second)
            store.save_routes(
                [
                    DrawingApiRoute(
                        name="二组群",
                        scope="group",
                        target_id="10001",
                        profile_id="second",
                    ),
                    DrawingApiRoute(
                        name="一组私聊",
                        scope="private",
                        target_id="20001",
                        profile_id="first",
                    ),
                ]
            )
            self.assertEqual(store.resolve("group", "10001").profile_id, "second")
            self.assertEqual(store.list_routes()[0].name, "二组群")
            self.assertEqual(store.resolve("private", "20001").profile_id, "first")
            self.assertIsNone(store.resolve("private", "20002"))
            reopened = DrawingApiStore(Path(directory))
            reopened.load()
            self.assertEqual(len(reopened.list_routes()), 2)
            reopened.delete_profile("second")
            self.assertIsNone(reopened.resolve("group", "10001"))
            self.assertEqual(reopened.resolve("private", "20001").profile_id, "first")

    def test_client_posts_multipart_images_and_decodes_b64(self) -> None:
        image = png()
        payload = json.dumps(
            {
                "data": [{"b64_json": base64.b64encode(image).decode()}],
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            }
        ).encode()
        response = MagicMock()
        response.read.return_value = payload
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-1")

        with patch("drawing_api.urlopen", return_value=response) as mocked:
            result = OpenAIImageClient().generate(config, "换装", [image, image])

        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://relay.test/v1/images/edits")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertIn(b'filename="image1.png"', request.data)
        self.assertIn(b'name="image[]"', request.data)
        self.assertIn(b'name="prompt"', request.data)
        self.assertIn(b'name="size"\r\n\r\nauto', request.data)
        self.assertIn(b'name="quality"\r\n\r\nauto', request.data)
        self.assertIn(b'name="output_format"\r\n\r\npng', request.data)
        self.assertEqual(result.image, image)
        self.assertEqual(result.response["usage"]["output_tokens"], 20)
        self.assertTrue(result.response["data"][0]["b64_json"].startswith("<omitted "))

    def test_reference_size_uses_nearest_supported_orientation(self) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps({"data": [{"b64_json": base64.b64encode(png()).decode()}]}).encode()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        config = DrawingApiConfig(
            base_url="https://relay.test/v1", api_key="secret", model="gpt-image-1", size="reference"
        )

        with patch("drawing_api.urlopen", return_value=response) as mocked:
            OpenAIImageClient().generate(config, "换装", [png(1600, 900)])
        self.assertIn(b'name="size"\r\n\r\n1536x1024', mocked.call_args.args[0].data)

        with patch("drawing_api.urlopen", return_value=response) as mocked:
            OpenAIImageClient().generate(config, "换装", [png(900, 1600)])
        self.assertIn(b'name="size"\r\n\r\n1024x1536', mocked.call_args.args[0].data)

    def test_text_generation_uses_generations_endpoint(self) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
        ).encode()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-2")
        with patch("drawing_api.urlopen", return_value=response) as mocked:
            result = OpenAIImageClient().generate_text(config, "星空")
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://relay.test/v1/images/generations")
        self.assertIn(b'"prompt": "\\u661f\\u7a7a"', request.data)
        self.assertEqual(result.image, png())

    def test_http_400_becomes_short_image_rejected_error(self) -> None:
        self.assertIn("内容", _bad_request_message("该图片违反内容政策"))
        self.assertIn("格式", _bad_request_message("unsupported image format"))
        self.assertIn("400", _bad_request_message("something unexpected"))
        error = HTTPError(
            "https://relay.test/v1/images/edits",
            400,
            "Bad Request",
            None,
            BytesIO(b"very long upstream detail"),
        )
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-1")
        with patch("drawing_api.urlopen", side_effect=error), self.assertRaises(DrawingApiError) as raised:
            OpenAIImageClient().generate(config, "换装", [png()])
        self.assertEqual(raised.exception.status_code, 400)
        self.assertNotIn("very long upstream detail", str(raised.exception))

    def test_http_451_unsafe_becomes_short_image_rejected_error(self) -> None:
        self.assertIn("451", _bad_request_message("unsafe image", 451))
        error = HTTPError(
            "https://relay.test/v1/images/edits",
            451,
            "Unavailable For Legal Reasons",
            None,
            BytesIO(
                b'{"error":{"message":"The generated images appear to be unsafe. Try modifying the prompts or the seeds."}}'
            ),
        )
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-2.6")
        with patch("drawing_api.urlopen", side_effect=error), self.assertRaises(DrawingApiError) as raised:
            OpenAIImageClient().generate(config, "换装", [png()])
        self.assertEqual(raised.exception.status_code, 451)
        self.assertIn("451", str(raised.exception))
        self.assertIn("审核", str(raised.exception))
        self.assertNotIn("appear to be unsafe", str(raised.exception))

    def test_gpt_image_26_does_not_send_removed_input_fidelity(self) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
        ).encode()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        config = DrawingApiConfig(
            base_url="https://relay.test/v1",
            api_key="secret",
            model="gpt-image-2.6-sunburst",
            quality="max",
            complexity="high",
        )
        with patch("drawing_api.urlopen", return_value=response) as mocked:
            OpenAIImageClient().generate(config, "换装", [png()])

        request = mocked.call_args.args[0]
        self.assertNotIn(b'name="input_fidelity"', request.data)
        self.assertIn(b'name="quality"\r\n\r\nmax', request.data)
        self.assertIn(b'name="complexity"\r\n\r\nhigh', request.data)
        self.assertIn(b'name="output_format"\r\n\r\npng', request.data)

    def test_http_429_retries_with_retry_after(self) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
        ).encode()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        error = HTTPError(
            "https://relay.test/v1/images/edits",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            BytesIO(b"rate limited"),
        )
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-2.6")
        with patch("drawing_api.urlopen", side_effect=[error, response]) as mocked, patch(
            "drawing_api.time.sleep"
        ) as sleep:
            result = OpenAIImageClient().generate(config, "换装", [png()])

        self.assertEqual(result.image, png())
        self.assertEqual(mocked.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_http_503_exhausts_retry_with_exponential_delay(self) -> None:
        errors = [
            HTTPError(
                "https://relay.test/v1/images/edits",
                503,
                "Service Unavailable",
                {},
                BytesIO(b"unavailable"),
            )
            for _ in range(3)
        ]
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-2.6")
        with patch("drawing_api.urlopen", side_effect=errors), patch("drawing_api.time.sleep") as sleep:
            with self.assertRaises(DrawingApiError) as raised:
                OpenAIImageClient().generate(config, "换装", [png()])

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(sleep.call_args_list, [call(1.0), call(2.0)])

    def test_http_error_keeps_sanitized_response_body(self) -> None:
        errors = [
            HTTPError(
                "https://relay.test/v1/images/edits",
                503,
                "Service Unavailable",
                {},
                BytesIO(
                    json.dumps({"error": {"message": "no account"}, "usage": {"output_tokens": 0}}).encode()
                ),
            )
            for _ in range(3)
        ]
        config = DrawingApiConfig(base_url="https://relay.test/v1", api_key="secret", model="gpt-image-2.6")
        with patch("drawing_api.urlopen", side_effect=errors), patch(
            "drawing_api.time.sleep"
        ):
            with self.assertRaises(DrawingApiError) as raised:
                OpenAIImageClient().generate(config, "换装", [png()])

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.response["error"]["message"], "no account")
        self.assertEqual(raised.exception.response["usage"]["output_tokens"], 0)

    def test_client_requires_configuration(self) -> None:
        with self.assertRaises(DrawingApiError):
            OpenAIImageClient().generate(DrawingApiConfig(), "prompt", [png()])


if __name__ == "__main__":
    unittest.main()
