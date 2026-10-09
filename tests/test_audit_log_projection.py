# 本文件验证权威 audit outbox 到本地日志的幂等投影和中断恢复。

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.storage.project_audit_log import project_pending_audit_log
from stock_review.storage.review_run_repository import ReviewRunRepository


class AuditLogProjectionTest(unittest.TestCase):
    def test_existing_log_line_is_marked_delivered_without_duplicate_append(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository = ReviewRunRepository(root / "review.sqlite")
            repository.initialize_schema()
            repository.append_audit_event(
                audit_event_id="AUDIT-1",
                event_type="test",
                payload={"command": "test", "status": "completed"},
                outbox_id="OUTBOX-1",
                outbox_payload={"command": "test", "status": "completed"},
            )
            log_path = root / "stock_review.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "outbox_id": "OUTBOX-1",
                        "audit_event_id": "AUDIT-1",
                        "event": {"command": "test", "status": "completed"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = project_pending_audit_log(repository, log_path)
            replay = project_pending_audit_log(repository, log_path)

            self.assertEqual(0, result["projected_count"])
            self.assertEqual(1, result["recovered_count"])
            self.assertEqual(0, replay["pending_count"])
            self.assertEqual(1, len(log_path.read_text(encoding="utf-8").splitlines()))

    def test_pending_events_are_written_as_json_lines_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository = ReviewRunRepository(root / "review.sqlite")
            repository.initialize_schema()
            repository.append_audit_event(
                audit_event_id="AUDIT-2",
                event_type="daily start",
                payload={"command": "daily start", "status": "completed"},
                outbox_id="OUTBOX-2",
                outbox_payload={"command": "daily start", "status": "completed"},
            )
            log_path = root / "stock_review.jsonl"

            first = project_pending_audit_log(repository, log_path)
            second = project_pending_audit_log(repository, log_path)

            self.assertEqual(1, first["projected_count"])
            self.assertEqual(0, second["pending_count"])
            line = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual("OUTBOX-2", line["outbox_id"])


if __name__ == "__main__":
    unittest.main()
