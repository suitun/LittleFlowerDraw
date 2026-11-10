"""验证 WebUI 设置的校验、持久化与旧 config.toml 迁移。"""

from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

import json
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "little_flower_draw_settings_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
webui_settings = import_module(f"{PACKAGE}.webui_settings")
role_patterns = import_module(f"{PACKAGE}.role_patterns")


class WebUISettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"

    def test_default_policies_fill_all_registered_commands(self) -> None:
        settings = webui_settings.WebUISettings()

        self.assertEqual(
            [policy.command for policy in settings.commands.policies],
            ["登记角色", "查看角色", "角色列表", "帮助", "自由绘画", "单角色替换", "双角色替换", "多人替换"],
        )
        self.assertEqual(settings.access.admin_qq, "2685275264")
        self.assertFalse(settings.access.whitelist_enabled)
        self.assertEqual(settings.drawing.min_group_roles, 3)
        self.assertEqual(settings.drawing.max_group_roles, 8)

    def test_command_policies_fill_missing_and_reject_duplicates(self) -> None:
        config = webui_settings.CommandPolicyConfig(
            policies=[{"command": "登记角色", "permission": "仅管理员", "limit": 2, "reset_days": 2}]
        )
        self.assertEqual(len(config.policies), 8)
        register = next(policy for policy in config.policies if policy.command == "登记角色")
        self.assertEqual((register.permission, register.limit, register.reset_days), ("仅管理员", 2, 2))

        with self.assertRaisesRegex(ValueError, "重复"):
            webui_settings.CommandPolicyConfig(
                policies=[
                    {"command": "登记角色", "permission": "所有人", "limit": 0, "reset_days": 1},
                    {"command": "登记角色", "permission": "仅管理员", "limit": 0, "reset_days": 1},
                ]
            )

    def test_access_normalizes_and_validates_admin_and_whitelists(self) -> None:
        config = webui_settings.AccessConfig(
            admin_qq=" 2685275264 ",
            group_whitelist=["10001", " 10001 ", "10002"],
            private_whitelist=["20001"],
        )
        self.assertEqual(config.admin_qq, "2685275264")
        self.assertEqual(config.group_whitelist, ["10001", "10002"])
        for value in ("", "1234", "abcde", "2685275264x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                webui_settings.AccessConfig(admin_qq=value)
        with self.assertRaises(ValueError):
            webui_settings.AccessConfig(group_whitelist=["not-a-group"])

    def test_store_creates_defaults_and_round_trips_edits(self) -> None:
        store = webui_settings.WebUISettingsStore(self.data_dir)
        loaded = store.load()
        self.assertTrue(store.path.is_file())
        self.assertEqual(loaded.access.admin_qq, "2685275264")

        loaded.access.admin_qq = "2999999999"
        loaded.guard_replies.admin_only = ["只有管理员可以哦"]
        loaded.replies.registered = ["记住{角色名}了喵～", "好哒，记住{角色名}啦～"]
        loaded.replies.at_replies = ["registered", "view"]
        store.save(loaded)

        reopened = webui_settings.WebUISettingsStore(self.data_dir)
        current = reopened.load()
        self.assertEqual(current.access.admin_qq, "2999999999")
        self.assertEqual(current.guard_replies.admin_only, ["只有管理员可以哦"])
        self.assertEqual(current.replies.registered, ["记住{角色名}了喵～", "好哒，记住{角色名}啦～"])
        self.assertEqual(current.replies.at_replies, ["registered", "view"])
        self.assertEqual(store.current().access.admin_qq, "2999999999")

    def test_legacy_config_sections_are_migrated(self) -> None:
        config_path = Path(self.temporary.name) / "config.toml"
        config_path.write_text(
            """
[access]
admin_qq = "2685275264"
whitelist_enabled = true
group_whitelist = ["10001"]
private_whitelist = []

[[commands.policies]]
command = "登记角色"
permission = "仅管理员"
limit = 3
reset_days = 2

[[commands.policies]]
command = "查看角色"
permission = "所有人"
limit = 0
reset_days = 1

[guard_replies]
admin_only = "旧配置提示"

[keywords]
register_templates = ["记住 {角色名}"]

[replies]
registered = "记住{角色名}啦"

[library]
max_name_length = 20
max_image_mb = 4
""",
            encoding="utf-8",
        )

        legacy = webui_settings.load_legacy_settings(config_path)
        self.assertIsNotNone(legacy)
        store = webui_settings.WebUISettingsStore(self.data_dir)
        migrated = store.load(legacy)

        self.assertEqual(migrated.access.group_whitelist, ["10001"])
        self.assertTrue(migrated.access.whitelist_enabled)
        self.assertEqual(migrated.commands.policies[0].limit, 3)
        self.assertEqual(migrated.guard_replies.admin_only, ["旧配置提示"])
        self.assertEqual(migrated.keywords.register_templates, ["记住 {角色名}"])
        self.assertEqual(migrated.replies.registered, ["记住{角色名}啦"])
        self.assertEqual(migrated.library.max_name_length, 20)
        self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["access"]["admin_qq"], "2685275264")

    def test_existing_settings_file_merges_new_groups_from_legacy(self) -> None:
        store = webui_settings.WebUISettingsStore(self.data_dir)
        store.load()
        old_data = json.loads(store.path.read_text(encoding="utf-8"))
        for key in ("keywords", "replies", "library"):
            old_data.pop(key)
        store.path.write_text(json.dumps(old_data), encoding="utf-8")

        legacy = {
            "keywords": {"register_templates": ["记住 {角色名}"], "view_templates": ["看看{角色名}"]},
            "replies": {"registered": "记住啦"},
            "library": {"max_name_length": 30, "max_image_mb": 5},
        }
        migrated = webui_settings.WebUISettingsStore(self.data_dir).load(legacy)

        self.assertEqual(migrated.keywords.view_templates, ["看看{角色名}"])
        self.assertEqual(migrated.replies.registered, ["记住啦"])
        self.assertEqual(migrated.library.max_image_mb, 5)
        self.assertEqual(migrated.access.admin_qq, "2685275264")

    def test_guard_reply_placeholders_are_limited_and_rendered(self) -> None:
        text = role_patterns.render_guard_reply("「{指令}」用完啦，{重置时间}再来喵～", "登记角色", "明天 0 点后")
        self.assertEqual(text, "「登记角色」用完啦，明天 0 点后再来喵～")
        for value in ("{角色名}", "{指令!r}", "{重置时间:>10}", "{不存在}", "{"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                webui_settings.GuardReplyConfig(quota_exceeded=value)

    def test_invalid_store_file_is_explicit(self) -> None:
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "webui_settings.json").write_text("{", encoding="utf-8")
        store = webui_settings.WebUISettingsStore(self.data_dir)

        with self.assertRaises(json.JSONDecodeError):
            store.load()


if __name__ == "__main__":
    unittest.main()
