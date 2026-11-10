"""验证角色库持久化、隔离及覆盖事务的完整性。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Tuple

import sqlite3
import unittest

from role_store import RoleStore


class RoleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "plugin-data"
        self.store = RoleStore(self.data_dir)

    def test_multiple_images_remain_ordered_after_reopening(self) -> None:
        images = [b"front\x00\xff", b"side\x01", b"back\x02"]

        self.assertFalse(self.store.save("group-a", "小花", "owner-a", images))
        reopened = RoleStore(self.data_dir)

        self.assertEqual(reopened.get("group-a", "小花"), images)
        self.assertEqual(reopened.get_owner("group-a", "小花"), "owner-a")
        self.assertIsNone(reopened.get("group-a", "不存在"))
        self.assertIsNone(reopened.get_owner("group-a", "不存在"))

    def test_overwrite_replaces_all_images_and_keeps_the_creator(self) -> None:
        self.store.save("group-a", "小花", "creator", [b"old-front", b"old-side", b"old-back"])

        self.assertTrue(self.store.save("group-a", "小花", "updater", [b"new-reference"]))

        self.assertEqual(self.store.get("group-a", "小花"), [b"new-reference"])
        self.assertEqual(self.store.get_owner("group-a", "小花"), "creator")

    def test_same_name_is_isolated_between_chat_scopes(self) -> None:
        self.store.save("group-a", "小花", "owner-a", [b"reference-a"])
        self.store.save("group-b", "小花", "owner-b", [b"reference-b"])
        self.store.save("group-a", "小花", "owner-a", [b"replacement-a"])

        self.assertEqual(self.store.get("group-a", "小花"), [b"replacement-a"])
        self.assertEqual(self.store.get("group-b", "小花"), [b"reference-b"])
        self.assertEqual(self.store.get_owner("group-b", "小花"), "owner-b")

    def test_role_names_are_literal_database_keys(self) -> None:
        role_name = "../小花/' OR 1=1 --"
        self.store.save("group-a", role_name, "owner", [b"reference"])

        self.assertEqual(self.store.get("group-a", role_name), [b"reference"])
        self.assertIsNone(self.store.get("group-a", "小花"))
        self.assertEqual(
            {path.name for path in self.data_dir.iterdir()},
            {"roles.sqlite3"},
        )

    def test_failed_write_rolls_back_new_and_existing_roles(self) -> None:
        old_images = [b"old-front", b"old-side"]
        self.store.save("group-a", "已有角色", "creator", old_images)
        with closing(sqlite3.connect(self.store.database_path)) as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER reject_test_image BEFORE INSERT ON role_images
                WHEN NEW.image_data = X'72656A656374'
                BEGIN
                    SELECT RAISE(ABORT, '模拟图片写入失败');
                END
                """
            )

        for name in ("已有角色", "新角色"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "模拟图片写入失败"):
                    self.store.save("group-a", name, "updater", [b"first-inserted", b"reject"])

        self.assertEqual(self.store.get("group-a", "已有角色"), old_images)
        self.assertEqual(self.store.get_owner("group-a", "已有角色"), "creator")
        self.assertIsNone(self.store.get("group-a", "新角色"))
        self.assertIsNone(self.store.get_owner("group-a", "新角色"))

    def test_concurrent_overwrites_commit_one_complete_image_set(self) -> None:
        def write_reference(number: int) -> Tuple[str, bool]:
            owner = str(number)
            images = [f"{number}-front".encode(), f"{number}-back".encode()]
            return owner, self.store.save("group-a", "小花", owner, images)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(write_reference, range(24), timeout=15))

        creators = [owner for owner, existed in results if not existed]
        self.assertEqual(len(creators), 1)
        images = self.store.get("group-a", "小花")
        self.assertIsNotNone(images)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].split(b"-")[0], images[1].split(b"-")[0])
        self.assertEqual(self.store.get_owner("group-a", "小花"), creators[0])


if __name__ == "__main__":
    unittest.main()
