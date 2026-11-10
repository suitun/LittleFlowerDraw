import base64
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from PIL import Image

from drawing_api import (
    DrawingApiConfig,
    DrawingApiError,
    DrawingApiStore,
    OpenAIImageClient,
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

    def test_client_posts_multipart_images_and_decodes_b64(self) -> None:
        image = png()
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(image).decode()}]}).encode()
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
        self.assertEqual(result.image, image)

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

    def test_client_requires_configuration(self) -> None:
        with self.assertRaises(DrawingApiError):
            OpenAIImageClient().generate(DrawingApiConfig(), "prompt", [png()])


if __name__ == "__main__":
    unittest.main()
