"""验证白名单、管理员权限和次数守卫的组合。"""

from datetime import datetime
from importlib import import_module
from pathlib import Path
from tempdir import TemporaryDirectory
from types import ModuleType
from unittest.mock import patch

import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "little_flower_draw_guard_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
command_guard = import_module(f"{PACKAGE}.command_guard")
command_usage = import_module(f"{PACKAGE}.command_usage")
webui_settings = import_module(f"{PACKAGE}.webui_settings")


def local_timestamp(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 1, day, hour, minute).timestamp())


class CommandGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        data_dir = Path(self.temporary.name) / "data"
        self.usage_store = command_usage.CommandUsageStore(data_dir)
        self.settings_store = webui_settings.WebUISettingsStore(data_dir)
        self.settings_store.load()
        self.guard = command_guard.CommandGuard(self.usage_store, self.settings_store)
        self.admin = "2685275264"

    def settings(self, **access: object) -> object:
        values = {
            "admin_qq": self.admin,
            "whitelist_enabled": True,
            "group_whitelist": ["10001"],
            "private_whitelist": ["20001"],
        }
        values.update(access)
        settings = webui_settings.WebUISettings(
            access=values,
            commands={
                "policies": [
                    {"command": "登记角色", "permission": "所有人", "limit": 1, "reset_days": 1},
                    {"command": "查看角色", "permission": "仅管理员", "limit": 0, "reset_days": 1},
                ]
            },
        )
        self.settings_store.save(settings)
        return settings

    def test_admin_bypasses_whitelist_and_admin_only_rules(self) -> None:
        self.settings(group_whitelist=[], private_whitelist=[])

        for command_id in ("登记角色", "查看角色"):
            with self.subTest(command_id=command_id):
                self.assertTrue(self.guard.check_access(command_id, self.admin, "99999").allowed)
                self.assertTrue(self.guard.check_access(command_id, self.admin, "").allowed)

    def test_group_and_private_whitelists_are_separate(self) -> None:
        self.settings()

        self.assertTrue(self.guard.check_access("登记角色", "30001", "10001").allowed)
        denied_group = self.guard.check_access("登记角色", "30001", "10002")
        self.assertFalse(denied_group.allowed)
        self.assertIn("白名单", denied_group.reply)

        self.assertTrue(self.guard.check_access("登记角色", "20001", "").allowed)
        denied_private = self.guard.check_access("登记角色", "20002", "")
        self.assertFalse(denied_private.allowed)
        self.assertIn("白名单", denied_private.reply)

    def test_admin_only_command_rejects_everyone_else(self) -> None:
        self.settings()

        decision = self.guard.check_access("查看角色", "20001", "")

        self.assertFalse(decision.allowed)
        self.assertIn("管理员", decision.reply)
        self.assertNotIn("{指令}", decision.reply)

    def test_quota_counts_only_recorded_success_and_resets_at_midnight(self) -> None:
        self.settings()
        first_use = local_timestamp(10, 23)
        with patch("command_usage.time.time", return_value=first_use):
            self.assertIsNone(self.guard.check_quota("登记角色", "20001"))
            self.guard.record_success("登记角色", "20001")

        late = local_timestamp(10, 23, 30)
        with patch("command_usage.time.time", return_value=late):
            reply = self.guard.check_quota("登记角色", "20001")
        self.assertIsNotNone(reply)
        self.assertIn("明天 0 点后", reply)
        self.assertNotIn("{重置时间}", reply)

        next_day = local_timestamp(11, 0, 1)
        with patch("command_usage.time.time", return_value=next_day):
            self.assertIsNone(self.guard.check_quota("登记角色", "20001"))

    def test_four_drawing_commands_share_one_quota_pool(self) -> None:
        settings = webui_settings.WebUISettings()
        for policy in settings.commands.policies:
            if policy.command in {"自由绘画", "单角色替换", "双角色替换", "多人替换"}:
                policy.limit = 1
                policy.reset_days = 1
        self.settings_store.save(settings)

        for command_id in ("单角色替换", "双角色替换", "多人替换", "自由绘画"):
            self.assertIsNone(self.guard.check_quota(command_id, "20001"))
            self.guard.record_success(command_id, "20001")
        denied = self.guard.check_quota("单角色替换", "20001")
        self.assertIsNotNone(denied)
        self.assertIn("画画", denied)
        self.assertEqual(self.guard.remaining_quota("自由绘画", "20001"), 0)

    def test_drawing_zero_limits_contribute_zero_not_unlimited(self) -> None:
        settings = webui_settings.WebUISettings()
        limits = {"自由绘画": 3, "单角色替换": 10, "双角色替换": 0, "多人替换": 0}
        for policy in settings.commands.policies:
            if policy.command in limits:
                policy.limit = limits[policy.command]
        self.settings_store.save(settings)

        self.assertEqual(self.guard.quota_limit(settings, "自由绘画", "20001"), 13)
        self.assertEqual(self.guard.remaining_quota("双角色替换", "20001"), 13)
        self.assertIsNone(self.guard.check_quota("单角色替换", "20001"))
        self.guard.record_success("单角色替换", "20001")
        self.assertEqual(self.guard.remaining_quota("多人替换", "20001"), 12)

    def test_user_extra_quota_is_added_to_command_limit(self) -> None:
        settings = self.settings()
        settings.user_quotas = [
            webui_settings.UserQuota(user_id="20001", command="登记角色", extra_count=2)
        ]
        self.settings_store.save(settings)

        for _ in range(3):
            self.assertIsNone(self.guard.check_quota("登记角色", "20001"))
            self.guard.record_success("登记角色", "20001")
        self.assertIsNotNone(self.guard.check_quota("登记角色", "20001"))
        self.assertIsNone(self.guard.check_quota("登记角色", "20002"))

    def test_admin_never_consumes_quota(self) -> None:
        self.settings()

        self.assertIsNone(self.guard.check_quota("登记角色", self.admin))
        self.guard.record_success("登记角色", self.admin)
        with self.usage_store._connection() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM command_usage").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_unlimited_command_never_denies(self) -> None:
        self.settings()
        settings = self.settings_store.current()
        settings.commands.policies[0].limit = 0
        self.settings_store.save(settings)

        for _ in range(3):
            self.assertIsNone(self.guard.check_quota("登记角色", "20001"))
            self.guard.record_success("登记角色", "20001")


if __name__ == "__main__":
    unittest.main()
