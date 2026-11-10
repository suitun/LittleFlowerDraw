"""绘图任务的本地持久化存储：排队顺序、状态流转与历史记录。"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

import json
import shutil
import sqlite3
import time


ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
INTERRUPTED_ERROR = "插件重启导致任务中断"
_ERROR_LIMIT = 4000
_ROW_COLUMNS = (
    "id",
    "status",
    "stage",
    "command_id",
    "kind",
    "requester_id",
    "group_id",
    "account_id",
    "scope",
    "source_message_id",
    "source_key",
    "role_names_json",
    "extra_prompt",
    "final_prompt",
    "reference_path",
    "api_model",
    "api_size",
    "quota_counted",
    "quota_window_start",
    "quota_refunded",
    "attempts",
    "error",
    "output_path",
    "created_at",
    "started_at",
    "finished_at",
    "requester_group_name",
    "requester_qq_name",
)
_SELECT_TASK_SQL = f"SELECT {', '.join(_ROW_COLUMNS)} FROM drawing_tasks"
_CREATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_sequence (
    name TEXT PRIMARY KEY,
    next_id INTEGER NOT NULL
);
INSERT OR IGNORE INTO task_sequence(name, next_id) VALUES ('drawing', 0);

CREATE TABLE IF NOT EXISTS drawing_tasks (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    command_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    requester_id TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_key TEXT NOT NULL UNIQUE,
    role_names_json TEXT NOT NULL,
    extra_prompt TEXT NOT NULL DEFAULT '',
    final_prompt TEXT NOT NULL,
    reference_path TEXT NOT NULL,
    api_model TEXT NOT NULL,
    api_size TEXT NOT NULL,
    quota_counted INTEGER NOT NULL DEFAULT 0,
    quota_window_start INTEGER NOT NULL DEFAULT 0,
    quota_refunded INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    output_path TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    started_at INTEGER NOT NULL DEFAULT 0,
    finished_at INTEGER NOT NULL DEFAULT 0,
    requester_group_name TEXT NOT NULL DEFAULT '',
    requester_qq_name TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_task_per_user
ON drawing_tasks(requester_id)
WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS drawing_task_queue_order
ON drawing_tasks(status, id);
"""
_INSERT_TASK_SQL = """
INSERT INTO drawing_tasks (
    id, status, stage, command_id, kind, requester_id, group_id, account_id, scope,
    source_message_id, source_key, role_names_json, extra_prompt, final_prompt,
    reference_path, api_model, api_size, created_at, requester_group_name, requester_qq_name
) VALUES (?, 'queued', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class TaskRecord:
    """一条绘图任务的完整快照。"""

    id: int
    status: str
    stage: str
    command_id: str
    kind: str
    requester_id: str
    group_id: str
    account_id: str
    scope: str
    source_message_id: str
    source_key: str
    role_names: List[str]
    extra_prompt: str
    final_prompt: str
    reference_path: str
    api_model: str
    api_size: str
    quota_counted: bool
    quota_window_start: int
    quota_refunded: bool
    attempts: int
    error: str
    output_path: str
    created_at: int
    started_at: int
    finished_at: int
    requester_group_name: str = ""
    requester_qq_name: str = ""


class TaskStoreError(RuntimeError):
    """绘图任务存储的统一异常基类。"""


class TaskNotFoundError(TaskStoreError):
    """指定的绘图任务不存在。"""


class DuplicateTaskError(TaskStoreError):
    """同一条来源消息已经提交过绘图任务。"""


class ActiveTaskError(TaskStoreError):
    """该用户已有排队或运行中的任务。"""


class QueueFullError(TaskStoreError):
    """排队中的任务数已达上限。"""


def _escape_like(value: str) -> str:
    """按 SQLite LIKE 的 ESCAPE 规则转义用户搜索词。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_record(row: Sequence[object]) -> TaskRecord:
    return TaskRecord(
        id=int(row[0]),
        status=str(row[1]),
        stage=str(row[2]),
        command_id=str(row[3]),
        kind=str(row[4]),
        requester_id=str(row[5]),
        group_id=str(row[6]),
        account_id=str(row[7]),
        scope=str(row[8]),
        source_message_id=str(row[9]),
        source_key=str(row[10]),
        role_names=[str(name) for name in json.loads(str(row[11]))],
        extra_prompt=str(row[12]),
        final_prompt=str(row[13]),
        reference_path=str(row[14]),
        api_model=str(row[15]),
        api_size=str(row[16]),
        quota_counted=bool(row[17]),
        quota_window_start=int(row[18]),
        quota_refunded=bool(row[19]),
        attempts=int(row[20]),
        error=str(row[21]),
        output_path=str(row[22]),
        created_at=int(row[23]),
        started_at=int(row[24]),
        finished_at=int(row[25]),
        requester_group_name=str(row[26]),
        requester_qq_name=str(row[27]),
    )


class DrawingTaskStore:
    """同步的绘图任务存储，调用方应放入 asyncio.to_thread 执行。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_dir / "drawing_tasks.sqlite3"
        self.tasks_dir = self.data_dir / "drawing_tasks"
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_CREATE_SCHEMA_SQL)
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(drawing_tasks)")}
            for column in ("requester_group_name", "requester_qq_name"):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE drawing_tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """每次操作独立连接，便于在线程池中调用并确保连接关闭。"""
        connection = sqlite3.connect(self.database_path, timeout=15)
        try:
            yield connection
        finally:
            connection.close()

    def _load(self, connection: sqlite3.Connection, task_id: int) -> Optional[TaskRecord]:
        row = connection.execute(f"{_SELECT_TASK_SQL} WHERE id = ?", (task_id,)).fetchone()
        return _row_to_record(row) if row is not None else None

    def _require(self, connection: sqlite3.Connection, task_id: int) -> TaskRecord:
        record = self._load(connection, task_id)
        if record is None:
            raise TaskNotFoundError(f"绘图任务不存在: {task_id}")
        return record

    def _allocate_id(self, connection: sqlite3.Connection) -> int:
        """在事务内领取下一个任务 ID；删除历史不会让 ID 回退。"""
        row = connection.execute("SELECT next_id FROM task_sequence WHERE name = ?", ("drawing",)).fetchone()
        if row is None:
            raise TaskStoreError("绘图任务序号记录缺失")
        next_id = int(row[0])
        connection.execute("UPDATE task_sequence SET next_id = ? WHERE name = ?", (next_id + 1, "drawing"))
        return next_id

    def _task_dir(self, task_id: int) -> Path:
        return self.tasks_dir / str(task_id)

    def _remove_files(self, record: TaskRecord) -> None:
        """删除任务的参考图、生成图和任务目录。"""
        for path in (record.reference_path, record.output_path):
            if path:
                Path(path).unlink(missing_ok=True)
        task_dir = self._task_dir(record.id)
        if task_dir.is_dir():
            shutil.rmtree(task_dir)

    def create_task(
        self,
        *,
        command_id: str,
        kind: str,
        requester_id: str,
        requester_group_name: str = "",
        requester_qq_name: str = "",
        group_id: str,
        account_id: str,
        scope: str,
        source_message_id: str,
        source_key: str,
        role_names: List[str],
        extra_prompt: str,
        final_prompt: str,
        reference_image: bytes,
        api_model: str,
        api_size: str,
        max_waiting_tasks: int = 0,
    ) -> TaskRecord:
        """查重、并发检查、配额容量、ID 分配、参考图写入与入库在同一事务内完成。"""
        if not command_id or not requester_id or not account_id:
            raise ValueError("指令、发起人和账号不能为空")
        if not source_key:
            raise ValueError("来源消息标识不能为空")
        if not isinstance(reference_image, bytes) or not reference_image:
            raise ValueError("绘图任务需要非空的参考图数据")

        now = int(time.time())
        role_names_json = json.dumps(list(role_names), ensure_ascii=False)
        task_dir: Optional[Path] = None
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    "SELECT id FROM drawing_tasks WHERE source_key = ?", (source_key,)
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateTaskError(f"该消息已提交过绘图任务: {source_key}")
                active = connection.execute(
                    "SELECT id FROM drawing_tasks WHERE requester_id = ? AND status IN ('queued', 'running')",
                    (requester_id,),
                ).fetchone()
                if active is not None:
                    raise ActiveTaskError(f"该用户已有进行中的绘图任务: {int(active[0])}")
                if max_waiting_tasks > 0:
                    queued = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM drawing_tasks WHERE status = 'queued'"
                        ).fetchone()[0]
                    )
                    if queued >= max_waiting_tasks:
                        raise QueueFullError(f"绘图排队已满: {queued}/{max_waiting_tasks}")

                task_id = self._allocate_id(connection)
                task_dir = self._task_dir(task_id)
                task_dir.mkdir(parents=True, exist_ok=True)
                reference_path = task_dir / "reference.png"
                reference_path.write_bytes(reference_image)
                connection.execute(
                    _INSERT_TASK_SQL,
                    (
                        task_id,
                        command_id,
                        kind,
                        requester_id,
                        group_id,
                        account_id,
                        scope,
                        source_message_id,
                        source_key,
                        role_names_json,
                        extra_prompt,
                        final_prompt,
                        str(reference_path),
                        api_model,
                        api_size,
                        now,
                        requester_group_name.strip(),
                        requester_qq_name.strip(),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                if task_dir is not None:
                    shutil.rmtree(task_dir, ignore_errors=True)
                raise

        return TaskRecord(
            id=task_id,
            status="queued",
            stage="",
            command_id=command_id,
            kind=kind,
            requester_id=requester_id,
            group_id=group_id,
            account_id=account_id,
            scope=scope,
            source_message_id=source_message_id,
            source_key=source_key,
            role_names=list(role_names),
            extra_prompt=extra_prompt,
            final_prompt=final_prompt,
            reference_path=str(reference_path),
            api_model=api_model,
            api_size=api_size,
            quota_counted=False,
            quota_window_start=0,
            quota_refunded=False,
            attempts=0,
            error="",
            output_path="",
            created_at=now,
            started_at=0,
            finished_at=0,
            requester_group_name=requester_group_name.strip(),
            requester_qq_name=requester_qq_name.strip(),
        )

    def set_quota(self, task_id: int, window_start: int) -> None:
        """记录该任务已计入配额及其时间窗口。"""
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "UPDATE drawing_tasks SET quota_counted = 1, quota_window_start = ? WHERE id = ?",
                (int(window_start), task_id),
            )
        if cursor.rowcount == 0:
            raise TaskNotFoundError(f"绘图任务不存在: {task_id}")

    def mark_refunded(self, task_id: int) -> None:
        """标记该任务的配额已经退还。"""
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "UPDATE drawing_tasks SET quota_refunded = 1 WHERE id = ?", (task_id,)
            )
        if cursor.rowcount == 0:
            raise TaskNotFoundError(f"绘图任务不存在: {task_id}")

    def claim_next(self) -> Optional[TaskRecord]:
        """按 ID 顺序领取最早排队的任务；没有排队任务时返回 None。"""
        now = int(time.time())
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    f"{_SELECT_TASK_SQL} WHERE status = 'queued' ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                task_id = int(row[0])
                connection.execute(
                    "UPDATE drawing_tasks SET status = 'running', stage = 'preparing', "
                    "started_at = ?, attempts = attempts + 1 WHERE id = ?",
                    (now, task_id),
                )
                record = self._require(connection, task_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return record

    def set_stage(self, task_id: int, stage: str) -> None:
        """更新任务当前阶段，供界面展示进度。"""
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "UPDATE drawing_tasks SET stage = ? WHERE id = ?", (stage, task_id)
            )
        if cursor.rowcount == 0:
            raise TaskNotFoundError(f"绘图任务不存在: {task_id}")

    def succeed(self, task_id: int, output_path: str) -> None:
        """把任务标记为成功并记录生成图路径。"""
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "UPDATE drawing_tasks SET status = 'succeeded', stage = '', output_path = ?, finished_at = ? "
                "WHERE id = ?",
                (output_path, int(time.time()), task_id),
            )
        if cursor.rowcount == 0:
            raise TaskNotFoundError(f"绘图任务不存在: {task_id}")

    def fail(self, task_id: int, error: str) -> TaskRecord:
        """把任务标记为失败并返回最新记录；错误信息截断到 4000 字符。"""
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE drawing_tasks SET status = 'failed', stage = '', error = ?, finished_at = ? "
                    "WHERE id = ?",
                    (error[:_ERROR_LIMIT], int(time.time()), task_id),
                )
                if cursor.rowcount == 0:
                    raise TaskNotFoundError(f"绘图任务不存在: {task_id}")
                record = self._require(connection, task_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return record

    def cancel(self, task_id: int) -> Optional[TaskRecord]:
        """取消排队中的任务；任务不存在或已在运行/结束时返回 None。"""
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE drawing_tasks SET status = 'cancelled', stage = '', finished_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (int(time.time()), task_id),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    return None
                record = self._require(connection, task_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return record

    def recover_running(self) -> List[TaskRecord]:
        """把上次进程遗留的 running 任务全部标记为失败。"""
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    f"{_SELECT_TASK_SQL} WHERE status = 'running' ORDER BY id"
                ).fetchall()
                task_ids = [int(row[0]) for row in rows]
                if not task_ids:
                    connection.rollback()
                    return []
                connection.execute(
                    "UPDATE drawing_tasks SET status = 'failed', stage = '', error = ?, finished_at = ? "
                    "WHERE status = 'running'",
                    (INTERRUPTED_ERROR, int(time.time())),
                )
                records = [self._require(connection, task_id) for task_id in task_ids]
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return records

    def get(self, task_id: int) -> Optional[TaskRecord]:
        """读取单个任务，不存在时返回 None。"""
        with self._connection() as connection:
            return self._load(connection, task_id)

    def list_current(self, limit: int = 100) -> List[TaskRecord]:
        """读取排队中和运行中的任务，按 ID 升序。"""
        with self._connection() as connection:
            rows = connection.execute(
                f"{_SELECT_TASK_SQL} WHERE status IN ('queued', 'running') ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_history(
        self, page: int = 1, page_size: int = 20, status: str = "", query: str = ""
    ) -> Tuple[List[TaskRecord], int]:
        """分页读取终态任务，可按状态和关键词筛选。"""
        if page < 1:
            raise ValueError("页码从 1 开始")
        if page_size < 1:
            raise ValueError("每页数量至少为 1")
        if status and status not in TERMINAL_STATUSES:
            return [], 0

        clauses = ["status IN ('succeeded', 'failed', 'cancelled')"]
        parameters: List[object] = []
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if query:
            pattern = f"%{_escape_like(query)}%"
            clauses.append(
                "(CAST(id AS TEXT) LIKE ? ESCAPE '\\' OR requester_id LIKE ? ESCAPE '\\' "
                "OR requester_group_name LIKE ? ESCAPE '\\' OR requester_qq_name LIKE ? ESCAPE '\\' "
                "OR role_names_json LIKE ? ESCAPE '\\' OR command_id LIKE ? ESCAPE '\\')"
            )
            parameters.extend([pattern] * 6)
        where = " AND ".join(clauses)
        with self._connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM drawing_tasks WHERE {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"{_SELECT_TASK_SQL} WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*parameters, page_size, (page - 1) * page_size],
            ).fetchall()
        return [_row_to_record(row) for row in rows], total

    def queue_position(self, task_id: int) -> int:
        """返回该任务前面还有多少排队任务；不是排队状态时返回 0。"""
        with self._connection() as connection:
            record = self._load(connection, task_id)
            if record is None or record.status != "queued":
                return 0
            row = connection.execute(
                "SELECT COUNT(*) FROM drawing_tasks WHERE status = 'queued' AND id < ?", (task_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def count_queued(self) -> int:
        """统计排队中的任务数量，不包含运行中的任务。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM drawing_tasks WHERE status = 'queued'"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def delete_tasks(self, task_ids: Sequence[int]) -> int:
        """删除终态任务及其文件，返回实际删除数量；排队和运行中的任务会跳过。"""
        records: List[TaskRecord] = []
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for task_id in dict.fromkeys(int(task_id) for task_id in task_ids):
                    record = self._load(connection, task_id)
                    if record is None or record.status not in TERMINAL_STATUSES:
                        continue
                    connection.execute("DELETE FROM drawing_tasks WHERE id = ?", (task_id,))
                    records.append(record)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        for record in records:
            self._remove_files(record)
        return len(records)

    def purge_history(self, days: int) -> int:
        """删除 finished_at 早于 now-days 的终态任务及其文件，返回删除数量。"""
        if days <= 0:
            return 0
        cutoff = int(time.time()) - days * 86400
        records: List[TaskRecord] = []
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    f"{_SELECT_TASK_SQL} WHERE status IN ('succeeded', 'failed', 'cancelled') "
                    "AND finished_at < ?",
                    (cutoff,),
                ).fetchall()
                records = [_row_to_record(row) for row in rows]
                if records:
                    connection.execute(
                        "DELETE FROM drawing_tasks WHERE status IN ('succeeded', 'failed', 'cancelled') "
                        "AND finished_at < ?",
                        (cutoff,),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        for record in records:
            self._remove_files(record)
        return len(records)

    def purge_outputs(self, days: int) -> int:
        """只清理过期任务的输出图，保留历史元数据和参考图。"""
        if days <= 0:
            return 0
        cutoff = int(time.time()) - days * 86400
        records: List[TaskRecord] = []
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    f"{_SELECT_TASK_SQL} WHERE status IN ('succeeded', 'failed', 'cancelled') "
                    "AND finished_at < ? AND output_path != ''",
                    (cutoff,),
                ).fetchall()
                records = [_row_to_record(row) for row in rows]
                if records:
                    connection.execute(
                        "UPDATE drawing_tasks SET output_path = '' WHERE status IN "
                        "('succeeded', 'failed', 'cancelled') AND finished_at < ? AND output_path != ''",
                        (cutoff,),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        for record in records:
            output = Path(record.output_path)
            if output.is_file():
                output.unlink(missing_ok=True)
        return len(records)
