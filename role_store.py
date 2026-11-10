"""角色图片的本地持久化存储。"""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

import sqlite3


class RoleStore:
    """按命名空间和角色名保存有序图片，业务入口使用全局命名空间。"""

    def __init__(self, data_dir: Path) -> None:
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = data_dir / "roles.sqlite3"
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS roles (
                    scope_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    PRIMARY KEY (scope_id, role_name)
                );
                CREATE TABLE IF NOT EXISTS role_images (
                    scope_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    image_index INTEGER NOT NULL,
                    image_data BLOB NOT NULL CHECK (length(image_data) > 0),
                    PRIMARY KEY (scope_id, role_name, image_index),
                    FOREIGN KEY (scope_id, role_name)
                        REFERENCES roles(scope_id, role_name) ON DELETE CASCADE
                );
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """每次操作独立连接，便于在线程池中调用并确保连接关闭。"""
        connection = sqlite3.connect(self.database_path, timeout=15)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    def save(self, scope_id: str, role_name: str, owner_id: str, images: List[bytes]) -> bool:
        """写入或完整替换角色图片，返回是否覆盖；保留首次创建者。"""
        if not scope_id or not role_name or not owner_id:
            raise ValueError("图库命名空间、角色名和创建者不能为空")
        image_data = tuple(images)
        if not image_data or any(not isinstance(image, bytes) or not image for image in image_data):
            raise ValueError("角色至少需要一张非空的二进制图片")

        with self._connection() as connection, connection:
            # 在判断是否覆盖之前取得写锁，避免并发首次注册都返回“新建”。
            connection.execute("BEGIN IMMEDIATE")
            existed = connection.execute(
                "SELECT 1 FROM roles WHERE scope_id = ? AND role_name = ?", (scope_id, role_name)
            ).fetchone() is not None
            connection.execute(
                "INSERT INTO roles (scope_id, role_name, owner_id) VALUES (?, ?, ?) "
                "ON CONFLICT (scope_id, role_name) DO NOTHING",
                (scope_id, role_name, owner_id),
            )
            connection.execute(
                "DELETE FROM role_images WHERE scope_id = ? AND role_name = ?", (scope_id, role_name)
            )
            connection.executemany(
                "INSERT INTO role_images (scope_id, role_name, image_index, image_data) VALUES (?, ?, ?, ?)",
                [(scope_id, role_name, index, image) for index, image in enumerate(image_data)],
            )
        return existed

    def get(self, scope_id: str, role_name: str) -> Optional[List[bytes]]:
        """按保存顺序读取角色图片，不存在时返回 None。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT image_data FROM role_images WHERE scope_id = ? AND role_name = ? ORDER BY image_index",
                (scope_id, role_name),
            ).fetchall()
        return [bytes(row[0]) for row in rows] if rows else None

    def get_owner(self, scope_id: str, role_name: str) -> Optional[str]:
        """读取首次注册角色的创建者，不存在时返回 None。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT owner_id FROM roles WHERE scope_id = ? AND role_name = ?", (scope_id, role_name)
            ).fetchone()
        return str(row[0]) if row is not None else None
