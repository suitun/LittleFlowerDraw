"""验证 WebUI 角色列表、详情和缩略图的数据组装与图片预算。"""

from importlib import import_module
from io import BytesIO
from pathlib import Path
from tempdir import TemporaryDirectory
from types import ModuleType

import base64
import sys
import unittest

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "little_flower_draw_webui_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
role_store = import_module(f"{PACKAGE}.role_store")
role_webui = import_module(f"{PACKAGE}.role_webui")
RoleStore = role_store.RoleStore
MAX_THUMBNAIL_BYTES = role_webui.MAX_THUMBNAIL_BYTES
MAX_PREVIEW_BYTES = role_webui.MAX_PREVIEW_BYTES
ImageRenderError = role_webui.ImageRenderError
data_url = role_webui.data_url
detail_payload = role_webui.detail_payload
list_payload = role_webui.list_payload
render_preview = role_webui.render_preview
render_thumbnail = role_webui.render_thumbnail
thumbnail_payload = role_webui.thumbnail_payload


def png_bytes(size: tuple[int, int] = (320, 240), color: str = "red") -> bytes:
    buffer = BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def decode_data_url(value: str) -> bytes:
    assert value.startswith("data:image/webp;base64,")
    return base64.b64decode(value.split(",", 1)[1], validate=True)


class RoleWebUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = RoleStore(self.root / "data")
        self.cache_dir = self.root / "cache"

    def test_record_thumbnail_and_preview_stay_within_browser_budget(self) -> None:
        image = png_bytes((900, 700))

        thumbnail = render_thumbnail(image)
        preview = render_preview(image)

        with Image.open(BytesIO(thumbnail)) as decoded:
            self.assertEqual(decoded.format, "WEBP")
            self.assertLessEqual(max(decoded.size), 160)
        with Image.open(BytesIO(preview)) as decoded:
            self.assertEqual(decoded.format, "WEBP")
            self.assertLessEqual(max(decoded.size), 1280)
        self.assertLessEqual(len(thumbnail), MAX_THUMBNAIL_BYTES)
        self.assertLessEqual(len(preview), MAX_PREVIEW_BYTES)

    def test_invalid_image_cannot_be_rendered(self) -> None:
        with self.assertRaises(ImageRenderError):
            render_thumbnail(b"this is not an image")

    def test_list_payload_contains_stats_and_search_results(self) -> None:
        self.store.save("global", "角色一", "owner", png_bytes())
        self.store.save("global", "角色二", "owner", png_bytes(color="blue"))

        payload = list_payload(self.store, "global", "角色一", 1, 20, "name")

        self.assertEqual(payload["total"], 1)
        self.assertEqual([item["name"] for item in payload["items"]], ["角色一"])
        self.assertEqual(payload["stats"]["roles"], 2)
        self.assertGreater(payload["stats"]["database_bytes"], 0)
        self.assertEqual(payload["pages"], 1)

    def test_detail_and_thumbnail_use_cached_webp_data_urls(self) -> None:
        image = png_bytes(color="green")
        self.store.save("global", "角色", "owner", image)

        thumbnail = thumbnail_payload(self.store, "global", "角色", self.cache_dir)
        detail = detail_payload(self.store, "global", "角色", self.cache_dir)

        self.assertTrue(thumbnail["image"].startswith("data:image/webp;base64,"))
        self.assertTrue(detail["preview"].startswith("data:image/webp;base64,"))
        self.assertNotEqual(thumbnail["image"], detail["preview"])
        self.assertEqual(decode_data_url(thumbnail["image"])[:4], b"RIFF")
        self.assertEqual(detail["owner"], "owner")
        cache_files = list(self.cache_dir.glob("*.webp"))
        self.assertEqual(len(cache_files), 2)

    def test_broken_stored_image_is_reported_in_detail_and_thumbnail(self) -> None:
        self.store.save("global", "坏图", "owner", b"broken")

        detail = detail_payload(self.store, "global", "坏图", self.cache_dir)
        thumbnail = thumbnail_payload(self.store, "global", "坏图", self.cache_dir)

        self.assertIsNone(detail["preview"])
        self.assertEqual(detail["preview_error"], "角色图片无法解码")
        self.assertIsNone(thumbnail["image"])
        self.assertEqual(thumbnail["error"], "角色图片无法解码")

    def test_missing_role_is_an_explicit_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "角色不存在"):
            detail_payload(self.store, "global", "不存在", self.cache_dir)
        with self.assertRaisesRegex(ValueError, "角色不存在"):
            thumbnail_payload(self.store, "global", "不存在", self.cache_dir)

    def test_data_url_prefix_matches_webp(self) -> None:
        encoded = data_url(b"RIFF")
        self.assertEqual(encoded, "data:image/webp;base64,UklGRg==")


if __name__ == "__main__":
    unittest.main()
