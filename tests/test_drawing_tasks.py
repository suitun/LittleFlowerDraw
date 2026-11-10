"""验证绘图任务的 ID 分配、排队容量、状态流转、文件清理与历史筛选。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional
from unittest.mock import patch

import unittest

from drawing_tasks import (
    ActiveTaskError,
    DrawingTaskStore,
    DuplicateTaskError,
    QueueFullError,
    TaskRecord,
)


def make_task(
    store: DrawingTaskStore,
    requester_id: str = "user-a",
    source_key: str = "message-1",
    role_names: Optional[List[str]] = None,
    command_id: str = "绘图",
    max_waiting_tasks: int = 0,
    reference_image: bytes = b"reference-bytes",
) -> TaskRecord:
    """用默认字段创建一个排队任务，测试只覆盖关注的参数。"""
    return store.create_task(
        command_id=command_id,
        kind="single",
        requester_id=requester_id,
        group_id="group-a",
        account_id="account-a",
        scope="group-a",
        source_message_id=source_key,
        source_key=source_key,
        role_names=["小花"] if role_names is None else role_names,
        extra_prompt="额外要求",
        final_prompt="画一只小花",
        reference_image=reference_image,
        api_model="gpt-image-1",
        api_size="1024x1024",
        max_waiting_tasks=max_waiting_tasks,
    )


class DrawingTaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "plugin-data"
        self.store = DrawingTaskStore(self.data_dir)

    def _create_and_finish(
        self, requester_id: str, source_key: str, role_names: List[str], command_id: str, outcome: str
    ) -> TaskRecord:
        record = make_task(
            self.store,
            requester_id=requester_id,
            source_key=source_key,
            role_names=role_names,
            command_id=command_id,
        )
        if outcome == "succeed":
            self.store.succeed(record.id, "")
        elif outcome == "fail":
            self.store.fail(record.id, "生成失败")
        else:
            self.store.cancel(record.id)
        return record

    def test_requester_names_persist_across_reopen(self) -> None:
        record = self.store.create_task(
            command_id="绘图",
            kind="replace",
            requester_id="user-name",
            requester_group_name="群里的名字",
            requester_qq_name="QQ 名字",
            group_id="group-a",
            account_id="account-a",
            scope="group-a",
            source_message_id="message-name",
            source_key="message-name",
            role_names=["小花"],
            extra_prompt="",
            final_prompt="画一只小花",
            reference_image=b"reference-bytes",
            api_model="gpt-image-1",
            api_size="1024x1024",
        )
        reopened = DrawingTaskStore(self.data_dir).get(record.id)
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened.requester_group_name, "群里的名字")
        self.assertEqual(reopened.requester_qq_name, "QQ 名字")

    def test_task_ids_start_at_zero_and_never_rewind(self) -> None:
        ids = [
            make_task(self.store, requester_id=f"user-{index}", source_key=f"message-{index}").id
            for index in range(3)
        ]
        self.assertEqual(ids, [0, 1, 2])

        for task_id in ids:
            record = self.store.claim_next()
            assert record is not None
            self.assertEqual(record.id, task_id)
            self.store.succeed(task_id, "")

        self.assertEqual(self.store.delete_tasks(ids), 3)
        self.assertEqual(make_task(self.store, requester_id="user-new", source_key="message-new").id, 3)

        reopened = DrawingTaskStore(self.data_dir)
        reopened_record = reopened.get(3)
        assert reopened_record is not None
        self.assertEqual(reopened_record.id, 3)
        with reopened._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_active_task_blocks_the_same_user(self) -> None:
        first = make_task(self.store, requester_id="user-a", source_key="message-a")

        with self.assertRaisesRegex(ActiveTaskError, "进行中"):
            make_task(self.store, requester_id="user-a", source_key="message-b")

        claimed = self.store.claim_next()
        assert claimed is not None
        self.assertEqual(claimed.id, first.id)
        # 运行中的任务同样阻止再次提交
        with self.assertRaises(ActiveTaskError):
            make_task(self.store, requester_id="user-a", source_key="message-b")

        self.store.succeed(first.id, "output-0.png")
        second = make_task(self.store, requester_id="user-a", source_key="message-b")
        self.assertEqual(second.id, first.id + 1)

        self.store.fail(second.id, "API 调用失败")
        third = make_task(self.store, requester_id="user-a", source_key="message-c")
        self.assertEqual(third.id, second.id + 1)

    def test_different_users_can_queue_at_the_same_time(self) -> None:
        first = make_task(self.store, requester_id="user-a", source_key="message-a")
        second = make_task(self.store, requester_id="user-b", source_key="message-b")

        self.assertEqual([record.id for record in self.store.list_current()], [first.id, second.id])
        self.assertEqual(self.store.count_queued(), 2)
        self.assertEqual(self.store.queue_position(first.id), 0)
        self.assertEqual(self.store.queue_position(second.id), 1)
        self.assertEqual(self.store.queue_position(404), 0)

    def test_queue_capacity_counts_queued_only(self) -> None:
        first = make_task(
            self.store, requester_id="user-a", source_key="message-a", max_waiting_tasks=1
        )

        with self.assertRaisesRegex(QueueFullError, "排队已满"):
            make_task(self.store, requester_id="user-b", source_key="message-b", max_waiting_tasks=1)

        # 回滚不消耗 ID，也不留下参考图目录
        self.assertFalse((self.data_dir / "drawing_tasks" / "1").exists())

        claimed = self.store.claim_next()
        assert claimed is not None
        self.assertEqual(claimed.id, first.id)

        # 运行中的任务不占排队容量
        second = make_task(
            self.store, requester_id="user-c", source_key="message-c", max_waiting_tasks=1
        )
        self.assertEqual(second.id, 1)
        self.assertEqual(self.store.count_queued(), 1)

    def test_duplicate_source_key_is_rejected(self) -> None:
        make_task(self.store, requester_id="user-a", source_key="same-message")

        with self.assertRaisesRegex(DuplicateTaskError, "已提交"):
            make_task(self.store, requester_id="user-a", source_key="same-message")

        # 查重先于同用户并发检查，换用户提交同一条消息仍判为重复
        with self.assertRaises(DuplicateTaskError):
            make_task(self.store, requester_id="user-b", source_key="same-message")

        self.assertEqual(make_task(self.store, requester_id="user-b", source_key="other-message").id, 1)

    def test_claim_next_is_fifo_and_advances_stage(self) -> None:
        first = make_task(self.store, requester_id="user-a", source_key="message-a")
        second = make_task(self.store, requester_id="user-b", source_key="message-b")

        claimed = self.store.claim_next()
        assert claimed is not None
        self.assertEqual(claimed.id, first.id)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.stage, "preparing")
        self.assertEqual(claimed.attempts, 1)
        self.assertGreater(claimed.started_at, 0)
        self.assertEqual(self.store.queue_position(claimed.id), 0)
        self.assertEqual(self.store.queue_position(second.id), 0)

        self.store.set_stage(claimed.id, "requesting")
        staged = self.store.get(claimed.id)
        assert staged is not None
        self.assertEqual(staged.stage, "requesting")

        self.store.succeed(claimed.id, "output-0.png")
        finished = self.store.get(claimed.id)
        assert finished is not None
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.stage, "")
        self.assertEqual(finished.output_path, "output-0.png")
        self.assertGreater(finished.finished_at, 0)

        next_claimed = self.store.claim_next()
        assert next_claimed is not None
        self.assertEqual(next_claimed.id, second.id)
        self.assertEqual(next_claimed.attempts, 1)
        self.assertIsNone(self.store.claim_next())

    def test_fail_cancel_and_quota_flags(self) -> None:
        failing = make_task(self.store, requester_id="user-a", source_key="message-a")
        self.store.set_quota(failing.id, 1_700_000_000)
        self.store.mark_refunded(failing.id)
        self.store.set_stage(failing.id, "requesting")

        failed = self.store.fail(failing.id, "x" * 5000)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.stage, "")
        self.assertEqual(len(failed.error), 4000)
        self.assertGreater(failed.finished_at, 0)
        self.assertTrue(failed.quota_counted)
        self.assertEqual(failed.quota_window_start, 1_700_000_000)
        self.assertTrue(failed.quota_refunded)

        cancelled = make_task(self.store, requester_id="user-b", source_key="message-b")
        cancelled_record = self.store.cancel(cancelled.id)
        assert cancelled_record is not None
        self.assertEqual(cancelled_record.status, "cancelled")
        self.assertEqual(cancelled_record.stage, "")
        self.assertGreater(cancelled_record.finished_at, 0)
        self.assertIsNone(self.store.cancel(cancelled.id))

        running = make_task(self.store, requester_id="user-c", source_key="message-c")
        claimed = self.store.claim_next()
        assert claimed is not None
        self.assertEqual(claimed.id, running.id)
        self.assertIsNone(self.store.cancel(running.id))
        running_record = self.store.get(running.id)
        assert running_record is not None
        self.assertEqual(running_record.status, "running")

    def test_recover_running_marks_tasks_failed(self) -> None:
        first = make_task(self.store, requester_id="user-a", source_key="message-a")
        self.store.set_quota(first.id, 1_700_000_000)
        second = make_task(self.store, requester_id="user-b", source_key="message-b")
        claimed_first = self.store.claim_next()
        claimed_second = self.store.claim_next()
        assert claimed_first is not None and claimed_second is not None
        self.assertEqual([claimed_first.id, claimed_second.id], [first.id, second.id])

        records = self.store.recover_running()
        self.assertEqual([record.id for record in records], [first.id, second.id])
        for record in records:
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.stage, "")
            self.assertEqual(record.error, "插件重启导致任务中断")
            self.assertGreater(record.finished_at, 0)
        self.assertTrue(records[0].quota_counted)
        self.assertEqual(records[0].quota_window_start, 1_700_000_000)
        self.assertEqual(self.store.recover_running(), [])

    def test_reference_image_and_delete_remove_files(self) -> None:
        record = make_task(self.store, reference_image=b"\x89PNG-reference")
        reference_path = self.data_dir / "drawing_tasks" / str(record.id) / "reference.png"
        self.assertEqual(Path(record.reference_path), reference_path)
        self.assertEqual(reference_path.read_bytes(), b"\x89PNG-reference")

        output_path = self.data_dir / "outputs" / "result.png"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"generated")

        # 排队和运行中的任务不能删除
        self.assertEqual(self.store.delete_tasks([record.id]), 0)
        self.assertIsNotNone(self.store.get(record.id))
        claimed = self.store.claim_next()
        assert claimed is not None
        self.assertEqual(claimed.id, record.id)
        self.assertEqual(self.store.delete_tasks([record.id]), 0)
        self.assertIsNotNone(self.store.get(record.id))

        self.store.succeed(record.id, str(output_path))
        self.assertEqual(self.store.delete_tasks([record.id, 404]), 1)
        self.assertIsNone(self.store.get(record.id))
        self.assertFalse(reference_path.exists())
        self.assertFalse((self.data_dir / "drawing_tasks" / str(record.id)).exists())
        self.assertFalse(output_path.exists())

    def test_delete_tasks_does_not_fail_when_a_file_is_already_locked(self) -> None:
        record = make_task(self.store, requester_id="user-locked", source_key="message-locked")
        self.store.succeed(record.id, "")
        with patch.object(self.store, "_remove_files", side_effect=OSError("文件被占用")):
            self.assertEqual(self.store.delete_tasks([record.id]), 1)
        self.assertIsNone(self.store.get(record.id))

    def test_history_pagination_and_filters(self) -> None:
        first = self._create_and_finish("user-a", "message-a", ["小花"], "绘图", "succeed")
        second = self._create_and_finish("user-b", "message-b", ["小花", "阿花"], "双人绘图", "fail")
        third = self._create_and_finish("user-c", "message-c", ["梅花"], "绘图", "cancel")
        fourth = self._create_and_finish("user-d", "message-d", ["兰花"], "绘图", "succeed")

        page_one, total = self.store.list_history(page=1, page_size=2)
        self.assertEqual(total, 4)
        self.assertEqual([record.id for record in page_one], [fourth.id, third.id])

        page_two, total = self.store.list_history(page=2, page_size=2)
        self.assertEqual(total, 4)
        self.assertEqual([record.id for record in page_two], [second.id, first.id])

        page_three, total = self.store.list_history(page=3, page_size=2)
        self.assertEqual(page_three, [])
        self.assertEqual(total, 4)

        failed_records, failed_total = self.store.list_history(status="failed")
        self.assertEqual([record.id for record in failed_records], [second.id])
        self.assertEqual(failed_total, 1)

        by_requester, requester_total = self.store.list_history(query="user-c")
        self.assertEqual([record.id for record in by_requester], [third.id])
        self.assertEqual(requester_total, 1)

        by_role, role_total = self.store.list_history(query="阿花")
        self.assertEqual([record.id for record in by_role], [second.id])
        self.assertEqual(role_total, 1)

        by_command, command_total = self.store.list_history(query="双人")
        self.assertEqual([record.id for record in by_command], [second.id])
        self.assertEqual(command_total, 1)

        by_id, id_total = self.store.list_history(query=str(third.id))
        self.assertEqual([record.id for record in by_id], [third.id])
        self.assertEqual(id_total, 1)

        by_exact_id, exact_total = self.store.list_history(query=f"#{third.id}")
        self.assertEqual([record.id for record in by_exact_id], [third.id])
        self.assertEqual(exact_total, 1)
        self.assertEqual(self.store.list_history(query="2026"), ([], 0))

        # 未结束的状态不属于历史
        self.assertEqual(self.store.list_history(status="running"), ([], 0))

    def test_purge_history_removes_old_terminal_tasks(self) -> None:
        with patch("drawing_tasks.time.time", return_value=1_000_000):
            old = make_task(self.store, requester_id="user-a", source_key="old-message")
            old_claim = self.store.claim_next()
            assert old_claim is not None
            self.assertEqual(old_claim.id, old.id)
            self.store.fail(old.id, "旧任务失败")

        recent = make_task(self.store, requester_id="user-b", source_key="recent-message")
        self.store.succeed(recent.id, "")

        self.assertEqual(self.store.purge_history(0), 0)
        self.assertEqual(self.store.purge_history(1), 1)
        self.assertIsNone(self.store.get(old.id))
        self.assertFalse(Path(old.reference_path).exists())
        self.assertIsNotNone(self.store.get(recent.id))


if __name__ == "__main__":
    unittest.main()
