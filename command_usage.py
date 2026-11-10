"""按每人每指令统计次数，并在本地时间 0 点重置。"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import sqlite3
import time


@dataclass(frozen=True)
class QuotaStatus:
    """一次配额检查的结果。"""

    allowed: bool
    used: int
    limit: int
    reset_at: int


def local_midnight(timestamp: float) -> int:
    moment = datetime.fromtimestamp(timestamp).astimezone()
    return int(moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def reset_at_for(window_start: int, reset_days: int) -> int:
    if reset_days < 1:
        raise ValueError("重置间隔至少为 1 天")
    start = datetime.fromtimestamp(window_start).astimezone()
    reset = (start + timedelta(days=reset_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(reset.timestamp())


def format_reset_hint(reset_at: int, now: Optional[float] = None) -> str:
    current = datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    if reset_at <= int(current.timestamp()):
        return "马上"
    reset_date = datetime.fromtimestamp(reset_at).astimezone().date()
    days = (reset_date - current.date()).days
    if days <= 0:
        return "今天 0 点后"
    if days == 1:
        return "明天 0 点后"
    return f"{days} 天后 0 点后"


class CommandUsageStore:
    """持久化每个用户在每个指令上的当日使用次数。"""

    def __init__(self, data_dir: Path) -> None:
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = data_dir / "command_usage.sqlite3"
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS command_usage (
                    user_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    used INTEGER NOT NULL CHECK (used >= 0),
                    PRIMARY KEY (user_id, command_id)
                );
                CREATE TABLE IF NOT EXISTS command_usage_refunds (
                    task_id INTEGER PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    refunded_at INTEGER NOT NULL
                );
                """
            )
            connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        try:
            yield connection
        finally:
            connection.close()

    def check(
        self,
        user_id: str,
        command_id: str,
        limit: int,
        reset_days: int,
        now: Optional[float] = None,
    ) -> QuotaStatus:
        """只检查是否还有额度，不写入；limit <= 0 表示不限次数。"""
        if not user_id or not command_id:
            raise ValueError("用户和指令不能为空")
        if reset_days < 1:
            raise ValueError("重置间隔至少为 1 天")
        current = time.time() if now is None else now
        if limit <= 0:
            return QuotaStatus(True, 0, 0, 0)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT window_start, used FROM command_usage WHERE user_id = ? AND command_id = ?",
                (user_id, command_id),
            ).fetchone()
        if row is None:
            return QuotaStatus(True, 0, limit, reset_at_for(local_midnight(current), reset_days))
        window_start = int(row[0])
        used = int(row[1])
        reset_at = reset_at_for(window_start, reset_days)
        if current >= reset_at:
            return QuotaStatus(True, 0, limit, reset_at_for(local_midnight(current), reset_days))
        return QuotaStatus(used < limit, used, limit, reset_at)

    def record_success(
        self,
        user_id: str,
        command_id: str,
        limit: int,
        reset_days: int,
        now: Optional[float] = None,
    ) -> int:
        """成功执行后额外记一次；返回计数窗口起点，失败请求不应调用。"""
        if limit <= 0:
            return 0
        if not user_id or not command_id:
            raise ValueError("用户和指令不能为空")
        current = time.time() if now is None else now
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT window_start, used FROM command_usage WHERE user_id = ? AND command_id = ?",
                (user_id, command_id),
            ).fetchone()
            if row is not None:
                window_start = int(row[0])
                if current < reset_at_for(window_start, reset_days):
                    used = int(row[1]) + 1
                else:
                    window_start = local_midnight(current)
                    used = 1
            else:
                window_start = local_midnight(current)
                used = 1
            connection.execute(
                "INSERT INTO command_usage (user_id, command_id, window_start, used) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (user_id, command_id) DO UPDATE SET window_start = excluded.window_start, used = excluded.used",
                (user_id, command_id, window_start, used),
            )
        return window_start

    def refund_once(
        self,
        task_id: int,
        user_id: str,
        command_id: str,
        window_start: int,
        now: Optional[float] = None,
    ) -> bool:
        """按任务 ID 幂等退还一次；窗口已经重置时不扣新窗口的次数。"""
        if task_id < 0 or not user_id or not command_id:
            raise ValueError("任务、用户和指令不能为空")
        current = int(time.time() if now is None else now)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO command_usage_refunds (task_id, user_id, command_id, refunded_at) "
                    "VALUES (?, ?, ?, ?)",
                    (task_id, user_id, command_id, current),
                )
            except sqlite3.IntegrityError:
                return False
            if window_start > 0:
                row = connection.execute(
                    "SELECT window_start, used FROM command_usage WHERE user_id = ? AND command_id = ?",
                    (user_id, command_id),
                ).fetchone()
                if row is not None and int(row[0]) == window_start and int(row[1]) > 0:
                    connection.execute(
                        "UPDATE command_usage SET used = used - 1 WHERE user_id = ? AND command_id = ?",
                        (user_id, command_id),
                    )
        return True
