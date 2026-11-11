"""验证角色库单图持久化、迁移、检索及覆盖事务的完整性。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempdir import TemporaryDirectory
from typing import Tuple
from unittest.mock import patch

import sqlite3
import unittest

from role_store import RoleStore


class RoleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "plugin-data"
        self.store = RoleStore(self.data_dir)

    def test_single_image_survives_reopening(self) -> None:
        self.assertFalse(self.store.save("group-a", "小花", "owner-a", b"front\x00\xff"))
        reopened = RoleStore(self.data_dir)

        self.assertEqual(reopened.get("group-a", "小花"), b"front\x00\xff")
        self.assertEqual(reopened.get_owner("group-a", "小花"), "owner-a")
        self.assertIsNone(reopened.get("group-a", "不存在"))
        self.assertIsNone(reopened.get_owner("group-a", "不存在"))

    def test_overwrite_replaces_the_image_and_keeps_the_creator(self) -> None:
        self.store.save("group-a", "小花", "creator", b"old-reference")

        with patch("role_store.time.time", return_value=2_000_000_000):
            self.assertTrue(self.store.save("group-a", "小花", "updater", b"new-reference"))

        self.assertEqual(self.store.get("group-a", "小花"), b"new-reference")
        self.assertEqual(self.store.get_owner("group-a", "小花"), "creator")
        summary = self.store.get_role("group-a", "小花")
        assert summary is not None
        self.assertEqual(summary.updated_at, 2_000_000_000)
        self.assertLess(summary.created_at, summary.updated_at)

    def test_same_name_is_isolated_between_chat_scopes(self) -> None:
        self.store.save("group-a", "小花", "owner-a", b"reference-a")
        self.store.save("group-b", "小花", "owner-b", b"reference-b")
        self.store.save("group-a", "小花", "owner-a", b"replacement-a")

        self.assertEqual(self.store.get("group-a", "小花"), b"replacement-a")
        self.assertEqual(self.store.get("group-b", "小花"), b"reference-b")
        self.assertEqual(self.store.get_owner("group-b", "小花"), "owner-b")

    def test_role_names_are_literal_database_keys(self) -> None:
        role_name = "../小花/' OR 1=1 --"
        self.store.save("group-a", role_name, "owner", b"reference")

        self.assertEqual(self.store.get("group-a", role_name), b"reference")
        self.assertIsNone(self.store.get("group-a", "小花"))
        self.assertEqual(
            {path.name for path in self.data_dir.iterdir()},
            {"roles.sqlite3"},
        )

    def test_search_escapes_like_wildcards_and_paginates(self) -> None:
        self.store.save("group-a", "100%_花", "owner", b"literal")
        self.store.save("group-a", "1000花", "owner", b"plain")
        self.store.save("group-a", "小花", "owner", b"flower")

        self.assertEqual([item.name for item in self.store.list_roles("group-a", "100%_")], ["100%_花"])
        self.assertEqual(self.store.count_roles("group-a", "100%_"), 1)
        first_page = self.store.list_roles("group-a", limit=2, offset=0)
        second_page = self.store.list_roles("group-a", limit=2, offset=2)
        self.assertEqual(len(first_page), 2)
        self.assertEqual(len(second_page), 1)
        self.assertNotEqual({item.name for item in first_page}, {item.name for item in second_page})

    def test_sort_by_updated_uses_timestamps(self) -> None:
        with patch("role_store.time.time", side_effect=[100, 200, 300]):
            self.store.save("group-a", "先创建", "owner", b"one")
            self.store.save("group-a", "后创建", "owner", b"two")
            self.store.save("group-a", "先创建", "owner", b"three")

        self.assertEqual([item.name for item in self.store.list_roles("group-a", sort="updated")], ["先创建", "后创建"])

    def test_stats_report_roles_image_bytes_and_database_size(self) -> None:
        self.store.save("group-a", "小花", "owner", b"12345")
        self.store.save("group-a", "小草", "owner", b"123")

        stats = self.store.stats("group-a")

        self.assertEqual(stats.role_count, 2)
        self.assertEqual(stats.image_bytes, 8)
        self.assertGreater(stats.database_bytes, 0)

    def test_delete_and_rename_keep_metadata_consistent(self) -> None:
        self.store.save("group-a", "旧名字", "creator", b"reference")
        self.assertTrue(self.store.rename("group-a", "旧名字", "新名字"))

        self.assertIsNone(self.store.get_role("group-a", "旧名字"))
        self.assertEqual(self.store.get("group-a", "新名字"), b"reference")
        self.assertEqual(self.store.get_owner("group-a", "新名字"), "creator")
        self.assertFalse(self.store.rename("group-a", "不存在", "另一个名字"))

        self.store.save("group-a", "已有名字", "owner", b"other")
        with self.assertRaisesRegex(ValueError, "同名角色"):
            self.store.rename("group-a", "新名字", "已有名字")
        self.assertEqual(self.store.get("group-a", "新名字"), b"reference")

        self.assertTrue(self.store.delete("group-a", "新名字"))
        self.assertFalse(self.store.delete("group-a", "新名字"))
        self.assertIsNone(self.store.get("group-a", "新名字"))

    def test_old_multi_image_database_is_migrated_to_the_first_image(self) -> None:
        with TemporaryDirectory() as directory:
            old_data_dir = Path(directory) / "old-format"
            old_data_dir.mkdir()
            database_path = old_data_dir / "roles.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE roles (
                        scope_id TEXT NOT NULL,
                        role_name TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        PRIMARY KEY (scope_id, role_name)
                    );
                    CREATE TABLE role_images (
                        scope_id TEXT NOT NULL,
                        role_name TEXT NOT NULL,
                        image_index INTEGER NOT NULL,
                        image_data BLOB NOT NULL,
                        PRIMARY KEY (scope_id, role_name, image_index),
                        FOREIGN KEY (scope_id, role_name) REFERENCES roles(scope_id, role_name) ON DELETE CASCADE
                    );
                    INSERT INTO roles VALUES ('group-a', '小花', 'creator');
                    INSERT INTO role_images VALUES ('group-a', '小花', 0, X'6669727374');
                    INSERT INTO role_images VALUES ('group-a', '小花', 1, X'7365636F6E64');
                    """
                )

            migrated = RoleStore(old_data_dir)

            self.assertEqual(migrated.get("group-a", "小花"), b"first")
            self.assertEqual(migrated.get_owner("group-a", "小花"), "creator")
            with closing(sqlite3.connect(database_path)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertNotIn("role_images", tables)
            self.assertEqual(user_version, 2)

    def test_failed_write_rolls_back_new_and_existing_roles(self) -> None:
        self.store.save("group-a", "已有角色", "creator", b"old-reference")
        with closing(sqlite3.connect(self.store.database_path)) as connection, connection:
            for operation in ("INSERT", "UPDATE"):
                connection.execute(
                    f"""
                    CREATE TRIGGER reject_test_{operation.lower()} BEFORE {operation} ON roles
                    WHEN NEW.image_data = X'72656A656374'
                    BEGIN
                        SELECT RAISE(ABORT, '模拟图片写入失败');
                    END
                    """
                )

        for name in ("已有角色", "新角色"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "模拟图片写入失败"):
                    self.store.save("group-a", name, "updater", b"reject")

        self.assertEqual(self.store.get("group-a", "已有角色"), b"old-reference")
        self.assertEqual(self.store.get_owner("group-a", "已有角色"), "creator")
        self.assertIsNone(self.store.get("group-a", "新角色"))
        self.assertIsNone(self.store.get_owner("group-a", "新角色"))

    def test_concurrent_overwrites_commit_one_complete_image(self) -> None:
        def write_reference(number: int) -> Tuple[str, bool]:
            owner = str(number)
            image = f"{number}-image".encode()
            return owner, self.store.save("group-a", "小花", owner, image)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(write_reference, range(24), timeout=15))

        creators = [owner for owner, existed in results if not existed]
        self.assertEqual(len(creators), 1)
        image = self.store.get("group-a", "小花")
        self.assertIsNotNone(image)
        assert image is not None
        self.assertIn(image.split(b"-")[0].decode(), {str(number) for number in range(24)})
        self.assertEqual(self.store.get_owner("group-a", "小花"), creators[0])


if __name__ == "__main__":
    unittest.main()
