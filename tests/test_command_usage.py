"""验证每人每指令次数、0 点重置与成功计数。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempdir import TemporaryDirectory
from unittest.mock import patch

import unittest

from command_usage import CommandUsageStore, format_reset_hint, local_midnight, reset_at_for


def local_timestamp(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 1, day, hour, minute).timestamp())


class CommandUsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "plugin-data"
        self.store = CommandUsageStore(self.data_dir)

    def test_daily_quota_resets_at_local_midnight(self) -> None:
        first_use = local_timestamp(10, 23)
        with patch("command_usage.time.time", return_value=first_use):
            self.assertTrue(self.store.check("user", "登记角色", 1, 1).allowed)
            self.store.record_success("user", "登记角色", 1, 1)

        late = local_timestamp(10, 23, 30)
        with patch("command_usage.time.time", return_value=late):
            status = self.store.check("user", "登记角色", 1, 1)
        self.assertFalse(status.allowed)
        self.assertEqual(status.used, 1)
        self.assertEqual(format_reset_hint(status.reset_at, now=late), "明天 0 点后")

        next_day = local_timestamp(11, 0, 1)
        with patch("command_usage.time.time", return_value=next_day):
            self.assertTrue(self.store.check("user", "登记角色", 1, 1).allowed)
            self.store.record_success("user", "登记角色", 1, 1)
            self.assertFalse(self.store.check("user", "登记角色", 1, 1).allowed)

    def test_two_day_interval_resets_after_the_second_midnight(self) -> None:
        first_use = local_timestamp(10, 23)
        with patch("command_usage.time.time", return_value=first_use):
            self.store.record_success("user", "查看角色", 1, 2)

        same_second_day = local_timestamp(11, 23)
        with patch("command_usage.time.time", return_value=same_second_day):
            self.assertFalse(self.store.check("user", "查看角色", 1, 2).allowed)

        after_reset = local_timestamp(12, 0, 1)
        with patch("command_usage.time.time", return_value=after_reset):
            self.assertTrue(self.store.check("user", "查看角色", 1, 2).allowed)

        self.assertEqual(reset_at_for(local_midnight(first_use), 2), local_timestamp(12, 0))

    def test_unlimited_commands_do_not_write_usage(self) -> None:
        with patch("command_usage.time.time", return_value=local_timestamp(10, 12)):
            self.assertTrue(self.store.check("user", "登记角色", 0, 1).allowed)
            self.store.record_success("user", "登记角色", 0, 1)

        with self.store._connection() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM command_usage").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_refund_once_restores_used_in_same_window(self) -> None:
        now = local_timestamp(10, 12)
        with patch("command_usage.time.time", return_value=now):
            window = self.store.record_success("user", "登记角色", 2, 1)
            self.assertEqual(self.store.check("user", "登记角色", 2, 1).used, 1)

            self.assertTrue(self.store.refund_once(42, "user", "登记角色", window))
            self.assertEqual(self.store.check("user", "登记角色", 2, 1).used, 0)
            self.assertFalse(self.store.refund_once(42, "user", "登记角色", window))
            self.assertEqual(self.store.check("user", "登记角色", 2, 1).used, 0)

    def test_refund_does_not_touch_a_new_window(self) -> None:
        first_day = local_timestamp(10, 23)
        with patch("command_usage.time.time", return_value=first_day):
            old_window = self.store.record_success("user", "登记角色", 2, 1)
        next_day = local_timestamp(11, 12)
        with patch("command_usage.time.time", return_value=next_day):
            self.store.record_success("user", "登记角色", 2, 1)
            self.store.refund_once(43, "user", "登记角色", old_window)
            self.assertEqual(self.store.check("user", "登记角色", 2, 1).used, 1)

    def test_usage_survives_reopening(self) -> None:
        now = local_timestamp(10, 12)
        with patch("command_usage.time.time", return_value=now):
            self.store.record_success("user", "登记角色", 2, 1)
            self.store.record_success("user", "登记角色", 2, 1)

            reopened = CommandUsageStore(self.data_dir)
            self.assertFalse(reopened.check("user", "登记角色", 2, 1).allowed)
            self.assertEqual(reopened.check("user", "登记角色", 3, 1).used, 2)

    def test_concurrent_successes_increment_atomically(self) -> None:
        now = local_timestamp(10, 12)

        def record(_: int) -> None:
            with patch("command_usage.time.time", return_value=now):
                self.store.record_success("user", "登记角色", 100, 1)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(record, range(20), timeout=15))

        with patch("command_usage.time.time", return_value=now):
            status = self.store.check("user", "登记角色", 100, 1)
        self.assertEqual(status.used, 20)
        self.assertTrue(status.allowed)

    def test_invalid_interval_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "重置间隔"):
            self.store.check("user", "登记角色", 1, 0)
        with self.assertRaisesRegex(ValueError, "重置间隔"):
            reset_at_for(local_timestamp(10, 0), 0)


if __name__ == "__main__":
    unittest.main()
