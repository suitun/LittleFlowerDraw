"""角色图片的本地持久化存储。"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

import sqlite3
import time


SCHEMA_VERSION = 2
_SORT_SQL = {
    "name": "role_name COLLATE NOCASE ASC",
    "updated": "updated_at DESC, role_name COLLATE NOCASE ASC",
}
_CREATE_ROLES_SQL = """
CREATE TABLE roles (
    scope_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    image_data BLOB NOT NULL CHECK (length(image_data) > 0),
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_id, role_name)
)
"""


@dataclass(frozen=True)
class RoleSummary:
    """角色列表与详情共用的元信息，不包含图片本体。"""

    name: str
    owner_id: str
    image_bytes: int
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class LibraryStats:
    """角色库的聚合统计。"""

    role_count: int
    image_bytes: int
    database_bytes: int


def _escape_like(value: str) -> str:
    """按 SQLite LIKE 的 ESCAPE 规则转义用户搜索词。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class RoleStore:
    """按命名空间和角色名保存单张图片，业务入口使用全局命名空间。"""

    def __init__(self, data_dir: Path) -> None:
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = data_dir / "roles.sqlite3"
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._migrate(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """每次操作独立连接，便于在线程池中调用并确保连接关闭。"""
        connection = sqlite3.connect(self.database_path, timeout=15)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    def _migrate(self, connection: sqlite3.Connection) -> None:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        columns = (
            {str(row[1]) for row in connection.execute("PRAGMA table_info(roles)")} if "roles" in tables else set()
        )
        if "roles" not in tables:
            with connection:
                connection.execute(_CREATE_ROLES_SQL)
        elif "image_data" not in columns:
            self._upgrade_single_image(connection, "role_images" in tables)
        with connection:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _upgrade_single_image(self, connection: sqlite3.Connection, has_image_table: bool) -> None:
        """把旧版多图表结构迁移为单图结构，多图角色保留第一张。"""
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with connection:
                connection.execute("DROP TABLE IF EXISTS roles_migrated")
                connection.execute(_CREATE_ROLES_SQL.replace("CREATE TABLE roles", "CREATE TABLE roles_migrated", 1))
                if has_image_table:
                    connection.execute(
                        """
                        INSERT INTO roles_migrated (scope_id, role_name, owner_id, image_data, created_at, updated_at)
                        SELECT roles.scope_id, roles.role_name, roles.owner_id, role_images.image_data, 0, 0
                        FROM roles
                        JOIN role_images
                          ON role_images.scope_id = roles.scope_id AND role_images.role_name = roles.role_name
                        WHERE role_images.image_index = (
                            SELECT MIN(pending.image_index)
                            FROM role_images AS pending
                            WHERE pending.scope_id = roles.scope_id AND pending.role_name = roles.role_name
                        )
                        """
                    )
                connection.execute("DROP TABLE IF EXISTS role_images")
                connection.execute("DROP TABLE roles")
                connection.execute("ALTER TABLE roles_migrated RENAME TO roles")
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def save(self, scope_id: str, role_name: str, owner_id: str, image: bytes) -> bool:
        """写入或替换角色的唯一图片，返回是否覆盖；保留首次创建者。"""
        if not scope_id or not role_name or not owner_id:
            raise ValueError("图库命名空间、角色名和创建者不能为空")
        if not isinstance(image, bytes) or not image:
            raise ValueError("角色需要一张非空的二进制图片")

        now = int(time.time())
        with self._connection() as connection, connection:
            # 在判断是否覆盖之前取得写锁，避免并发首次注册都返回“新建”。
            connection.execute("BEGIN IMMEDIATE")
            existed = connection.execute(
                "SELECT 1 FROM roles WHERE scope_id = ? AND role_name = ?", (scope_id, role_name)
            ).fetchone() is not None
            if existed:
                connection.execute(
                    "UPDATE roles SET image_data = ?, updated_at = ? WHERE scope_id = ? AND role_name = ?",
                    (image, now, scope_id, role_name),
                )
            else:
                connection.execute(
                    "INSERT INTO roles (scope_id, role_name, owner_id, image_data, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (scope_id, role_name, owner_id, image, now, now),
                )
        return existed

    def get(self, scope_id: str, role_name: str) -> Optional[bytes]:
        """读取角色的唯一图片，不存在时返回 None。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT image_data FROM roles WHERE scope_id = ? AND role_name = ?", (scope_id, role_name)
            ).fetchone()
        return bytes(row[0]) if row is not None else None

    def get_owner(self, scope_id: str, role_name: str) -> Optional[str]:
        """读取首次注册角色的创建者，不存在时返回 None。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT owner_id FROM roles WHERE scope_id = ? AND role_name = ?", (scope_id, role_name)
            ).fetchone()
        return str(row[0]) if row is not None else None

    def get_role(self, scope_id: str, role_name: str) -> Optional[RoleSummary]:
        """读取角色元信息，不存在时返回 None。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT role_name, owner_id, length(image_data), created_at, updated_at "
                "FROM roles WHERE scope_id = ? AND role_name = ?",
                (scope_id, role_name),
            ).fetchone()
        return RoleSummary(*row) if row is not None else None

    def list_names(self, scope_id: str) -> List[str]:
        """读取全部角色名，供双角色绘图拆分名称。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT role_name FROM roles WHERE scope_id = ? ORDER BY role_name", (scope_id,)
            ).fetchall()
        return [str(row[0]) for row in rows]

    def list_roles(
        self, scope_id: str, query: str = "", limit: int = 20, offset: int = 0, sort: str = "name"
    ) -> List[RoleSummary]:
        """按名称或最近更新分页读取角色元信息。"""
        if limit < 0 or offset < 0:
            raise ValueError("分页大小和偏移量不能为负数")
        order = _SORT_SQL.get(sort)
        if order is None:
            raise ValueError(f"不支持的排序方式: {sort}")
        clause = "scope_id = ?"
        parameters: List[object] = [scope_id]
        if query:
            clause += " AND role_name LIKE ? ESCAPE '\\'"
            parameters.append(f"%{_escape_like(query)}%")
        parameters.extend((limit, offset))
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT role_name, owner_id, length(image_data), created_at, updated_at "
                f"FROM roles WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        return [RoleSummary(*row) for row in rows]

    def count_roles(self, scope_id: str, query: str = "") -> int:
        """统计匹配搜索词的角色数量。"""
        clause = "scope_id = ?"
        parameters: List[object] = [scope_id]
        if query:
            clause += " AND role_name LIKE ? ESCAPE '\\'"
            parameters.append(f"%{_escape_like(query)}%")
        with self._connection() as connection:
            row = connection.execute(f"SELECT COUNT(*) FROM roles WHERE {clause}", parameters).fetchone()
        return int(row[0]) if row is not None else 0

    def stats(self, scope_id: str) -> LibraryStats:
        """读取角色数、图片总字节和数据库逻辑大小。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(image_data)), 0) FROM roles WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        role_count = int(row[0]) if row is not None else 0
        image_bytes = int(row[1]) if row is not None else 0
        return LibraryStats(role_count, image_bytes, page_count * page_size)

    def delete(self, scope_id: str, role_name: str) -> bool:
        """删除角色及其唯一图片，返回是否真的删除了记录。"""
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "DELETE FROM roles WHERE scope_id = ? AND role_name = ?", (scope_id, role_name)
            )
        return cursor.rowcount > 0

    def rename(self, scope_id: str, old_name: str, new_name: str) -> bool:
        """重命名角色；角色不存在返回 False，新名称已存在时抛出 ValueError。"""
        if not old_name or not new_name:
            raise ValueError("角色名不能为空")
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existed = connection.execute(
                "SELECT 1 FROM roles WHERE scope_id = ? AND role_name = ?", (scope_id, old_name)
            ).fetchone() is not None
            if not existed:
                return False
            if old_name == new_name:
                return True
            conflict = connection.execute(
                "SELECT 1 FROM roles WHERE scope_id = ? AND role_name = ?", (scope_id, new_name)
            ).fetchone() is not None
            if conflict:
                raise ValueError("已存在同名角色")
            connection.execute(
                "UPDATE roles SET role_name = ?, updated_at = ? WHERE scope_id = ? AND role_name = ?",
                (new_name, int(time.time()), scope_id, old_name),
            )
        return True
