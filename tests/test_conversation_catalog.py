import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from conversation_catalog import ConversationCatalog, ConversationError
from bridge_core import ProjectRecord
from project_context import ProjectContextCollector


class ConversationCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex = self.root / ".codex"
        (self.codex / "sessions/2026/08/21").mkdir(parents=True)
        (self.codex / "archived_sessions").mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.session_id = "019f8422-9576-75f3-81b8-67f0a13578c2"
        self.state = self.codex / ".codex-global-state.json"
        self.state.write_text(json.dumps({
            "local-projects": {"p1": {"id": "p1", "name": "测试项目", "rootPaths": [str(self.project)]}},
            "sidebar-project-thread-orders": {"p1": {"threadIds": [self.session_id]}},
            "thread-project-assignments": {self.session_id: {"projectKind": "local", "projectId": "p1"}},
            "projectless-thread-ids": [],
        }), encoding="utf-8")
        self.file = self.codex / "sessions/2026/08/21" / f"rollout-2026-08-21T00-00-00-{self.session_id}.jsonl"
        self.file.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"id": self.session_id, "cwd": str(self.project), "timestamp": "2026-08-21T00:00:00Z"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "# Files mentioned by the user:\n\n## My request:\n读取项目"}}),
            json.dumps({"type": "response_item", "payload": {"item": {"type": "reasoning", "text": "secret"}}}),
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "结果 sk-abcdefghijklmnopqrstuvwxyz123456"}}),
        ]) + "\n", encoding="utf-8")
        self.state_db = self.codex / "state_5.sqlite"
        with closing(sqlite3.connect(self.state_db)) as connection:
            connection.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    cwd TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    preview TEXT NOT NULL DEFAULT ''
                );
            """)
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def catalog(self):
        return ConversationCatalog(self.state, lambda: (ProjectRecord("p1", "测试项目", (self.project,), "test"),))

    def test_list_and_read_are_project_scoped_and_redacted(self):
        catalog = self.catalog()
        listed = catalog.list_sessions(project_path=str(self.project))
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["sessions"][0]["title"], "读取项目")
        result = catalog.read_session(self.session_id, max_messages=5)
        self.assertEqual(result["project_name"], "测试项目")
        self.assertEqual(result["messages"][0]["text"], "读取项目")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", json.dumps(result))

    def test_session_text_redacts_extended_private_data(self):
        private_text = (
            "Cookie: session=private-value\n"
            "数据库 postgresql://user:password@example.test/db\n"
            "联系 owner@example.test"
        )
        self.file.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {
                "id": self.session_id,
                "cwd": str(self.project),
            }}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "user_message",
                "message": private_text,
            }}),
        ]) + "\n", encoding="utf-8")

        result = self.catalog().read_session(self.session_id)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private-value", serialized)
        self.assertNotIn("postgresql://", serialized)
        self.assertNotIn("owner@example.test", serialized)
        self.assertIn("已脱敏数据库连接串", serialized)
        self.assertIn("已脱敏邮箱", serialized)

    def test_unknown_or_invalid_session_is_rejected(self):
        catalog = self.catalog()
        with self.assertRaises(ConversationError):
            catalog.read_session("not-a-session")
        with self.assertRaises(ConversationError):
            catalog.read_session("019f8422-9576-75f3-81b8-67f0a13578c3")

    def test_sessions_outside_authorized_projects_are_hidden(self):
        unauthorized_project = self.root / "unauthorized"
        unauthorized_project.mkdir()
        unauthorized_session = "019f8422-9576-75f3-81b8-67f0a13578c3"
        unauthorized_file = self.file.with_name(
            f"rollout-2026-08-21T00-01-00-{unauthorized_session}.jsonl"
        )
        unauthorized_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": unauthorized_session,
                                "cwd": str(unauthorized_project),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "不应公开的会话",
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["sidebar-project-thread-orders"]["p1"]["threadIds"].append(
            unauthorized_session
        )
        state["thread-project-assignments"][unauthorized_session] = {
            "projectKind": "local",
            "projectId": "p1",
        }
        self.state.write_text(json.dumps(state), encoding="utf-8")

        catalog = self.catalog()
        listed = catalog.list_sessions()

        self.assertEqual(listed["count"], 1)
        self.assertNotIn(
            unauthorized_session,
            {item["session_id"] for item in listed["sessions"]},
        )
        with self.assertRaisesRegex(ConversationError, "已授权项目"):
            catalog.read_session(unauthorized_session)

    def test_state_db_threads_refresh_without_restart(self):
        second_session = "019f8422-9576-75f3-81b8-67f0a13578c3"
        second_file = self.file.with_name(
            f"rollout-2026-08-21T00-01-00-{second_session}.jsonl"
        )
        second_file.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {
                "id": second_session,
                "cwd": str(self.project),
                "timestamp": "2026-08-21T00:01:00Z",
            }}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "user_message",
                "message": "数据库新会话",
            }}),
        ]) + "\n", encoding="utf-8")
        catalog = self.catalog()
        self.assertEqual(catalog.list_sessions()["count"], 1)

        with closing(sqlite3.connect(self.state_db)) as connection:
            connection.execute(
                "INSERT INTO projects (id, name) VALUES (?, ?)",
                ("canonical-1", "测试项目"),
            )
            connection.execute(
                "INSERT INTO threads (id, project_id, cwd, archived, preview) VALUES (?, ?, ?, 0, ?)",
                (second_session, "canonical-1", str(self.project), "数据库新会话"),
            )
            connection.commit()

        listed = catalog.list_sessions(project_path=str(self.project))
        self.assertEqual(listed["count"], 2)
        self.assertEqual(listed["refresh_policy"], "live-per-call")
        self.assertIn(second_session, {item["session_id"] for item in listed["sessions"]})
        self.assertEqual(
            catalog.compatibility_snapshot(preview_limit=1)["refresh_policy"],
            "live-per-call",
        )

    def test_long_session_reads_latest_messages_with_lossless_backward_pagination(self):
        events = [
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": self.session_id,
                    "cwd": str(self.project),
                    "timestamp": "2026-08-21T00:00:00Z",
                },
            })
        ]
        events.extend(
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "user_message" if index % 2 == 0 else "agent_message",
                    "message": f"消息-{index:03d}",
                },
            })
            for index in range(250)
        )
        self.file.write_text("\n".join(events) + "\n", encoding="utf-8")

        catalog = self.catalog()
        newest = catalog.read_session(self.session_id, max_messages=100)
        middle = catalog.read_session(
            self.session_id,
            max_messages=100,
            cursor=newest["next_cursor"],
        )
        oldest = catalog.read_session(
            self.session_id,
            max_messages=100,
            cursor=middle["next_cursor"],
        )

        self.assertEqual(newest["messages"][0]["text"], "消息-150")
        self.assertEqual(newest["messages"][-1]["text"], "消息-249")
        self.assertEqual(middle["messages"][0]["text"], "消息-050")
        self.assertEqual(middle["messages"][-1]["text"], "消息-149")
        self.assertEqual(oldest["messages"][0]["text"], "消息-000")
        self.assertEqual(oldest["messages"][-1]["text"], "消息-049")
        self.assertIsNone(oldest["next_cursor"])
        self.assertFalse(oldest["has_more"])
        self.assertEqual(newest["scanned_message_count"], 250)

    def test_session_list_supports_cursor_pagination(self):
        session_ids = [self.session_id]
        state = json.loads(self.state.read_text(encoding="utf-8"))
        for index in range(1, 4):
            session_id = f"019f8422-9576-75f3-81b8-67f0a13578c{index + 2}"
            session_ids.append(session_id)
            path = self.file.with_name(
                f"rollout-2026-08-21T00-0{index}-00-{session_id}.jsonl"
            )
            path.write_text("\n".join([
                json.dumps({"type": "session_meta", "payload": {
                    "id": session_id,
                    "cwd": str(self.project),
                    "timestamp": f"2026-08-21T00:0{index}:00Z",
                }}),
                json.dumps({"type": "event_msg", "payload": {
                    "type": "user_message",
                    "message": f"会话-{index}",
                }}),
            ]) + "\n", encoding="utf-8")
        state["sidebar-project-thread-orders"]["p1"]["threadIds"] = session_ids
        for session_id in session_ids:
            state["thread-project-assignments"][session_id] = {
                "projectKind": "local",
                "projectId": "p1",
            }
        self.state.write_text(json.dumps(state), encoding="utf-8")

        catalog = self.catalog()
        first = catalog.list_sessions(limit=2)
        second = catalog.list_sessions(limit=2, cursor=first["next_cursor"])

        returned = [item["session_id"] for item in first["sessions"] + second["sessions"]]
        self.assertEqual(len(returned), 4)
        self.assertEqual(len(set(returned)), 4)
        self.assertEqual(first["total_count"], 4)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])

    def test_invalid_pagination_cursor_is_rejected(self):
        catalog = self.catalog()
        with self.assertRaisesRegex(ConversationError, "分页游标"):
            catalog.read_session(self.session_id, cursor="s:1")
        with self.assertRaisesRegex(ConversationError, "分页游标"):
            catalog.list_sessions(cursor="m:1")

    def test_single_message_can_expand_to_request_limit_with_page_budget(self):
        long_text = "长" * 120000
        self.file.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {
                "id": self.session_id,
                "cwd": str(self.project),
            }}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "user_message",
                "message": long_text,
            }}),
        ]) + "\n", encoding="utf-8")

        result = self.catalog().read_session(
            self.session_id,
            max_messages=1,
            max_message_chars=120000,
        )
        self.assertEqual(result["messages"][0]["text_chars"], 120000)
        self.assertFalse(result["messages"][0]["text_truncated"])
        with self.assertRaisesRegex(ConversationError, "单页消息预算"):
            self.catalog().read_session(
                self.session_id,
                max_messages=100,
                max_message_chars=120000,
            )

    def test_prepare_project_context_scans_redacts_and_caches(self):
        catalog = self.catalog()
        collector = ProjectContextCollector(catalog)

        first = collector.prepare(
            str(self.project),
            max_context_chars=10000,
            max_total_scan_bytes=1024 * 1024,
            max_session_scan_bytes=1024 * 1024,
        )

        self.assertTrue(first["scan_complete"])
        self.assertTrue(first["context_complete"])
        self.assertEqual(first["available_session_count"], 1)
        self.assertEqual(first["scanned_message_count"], 2)
        self.assertEqual(first["included_message_count"], 2)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", first["output"])
        self.assertIn("[已脱敏 API Key]", first["output"])
        self.assertFalse(first["cache_hit"])

        cached = collector.prepare(
            str(self.project),
            max_context_chars=10000,
            max_total_scan_bytes=1024 * 1024,
            max_session_scan_bytes=1024 * 1024,
        )
        self.assertTrue(cached["cache_hit"])

        with self.file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "缓存应失效"},
            }) + "\n")
        refreshed = collector.prepare(
            str(self.project),
            max_context_chars=10000,
            max_total_scan_bytes=1024 * 1024,
            max_session_scan_bytes=1024 * 1024,
        )
        self.assertFalse(refreshed["cache_hit"])
        self.assertEqual(refreshed["scanned_message_count"], 3)

    def test_prepare_project_context_marks_bounded_output(self):
        long_text = "上下文" * 8000
        self.file.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {
                "id": self.session_id,
                "cwd": str(self.project),
            }}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "user_message",
                "message": long_text,
            }}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "agent_message",
                "message": long_text,
            }}),
        ]) + "\n", encoding="utf-8")

        result = ProjectContextCollector(self.catalog()).prepare(
            str(self.project),
            max_context_chars=10000,
            max_total_scan_bytes=1024 * 1024,
            max_session_scan_bytes=1024 * 1024,
        )

        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["context_complete"])
        self.assertLessEqual(result["context_chars"], 10000)
        self.assertGreater(result["budget_truncated_message_count"], 0)

    def test_prepare_project_context_reports_source_scan_limit(self):
        self.file.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {
                "id": self.session_id,
                "cwd": str(self.project),
            }}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "user_message",
                "message": "超出扫描预算" * 1000,
            }}),
        ]) + "\n", encoding="utf-8")

        result = ProjectContextCollector(self.catalog()).prepare(
            str(self.project),
            max_context_chars=10000,
            max_total_scan_bytes=1024,
            max_session_scan_bytes=1024,
        )

        self.assertFalse(result["scan_complete"])
        self.assertEqual(result["source_truncated_session_count"], 1)

    def test_prepare_project_context_rejects_unregistered_project(self):
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(ConversationError, "已登记项目"):
            ProjectContextCollector(self.catalog()).prepare(str(outside))


if __name__ == "__main__":
    unittest.main()
