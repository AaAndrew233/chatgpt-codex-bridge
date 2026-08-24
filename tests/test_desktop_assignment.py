from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from desktop_assignment import DesktopAssignmentClient


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def read(self, size: int) -> bytes:
        return b""


class _FakeConnection:
    def __init__(self, status: int) -> None:
        self.status = status
        self.request_args = None
        self.closed = False

    def request(self, method, path, body=None, headers=None) -> None:
        self.request_args = (method, path, body, headers)

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(self.status)

    def close(self) -> None:
        self.closed = True


class DesktopAssignmentClientTests(unittest.TestCase):
    def test_missing_socket_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = DesktopAssignmentClient(
                socket_path=root / "missing.sock",
                token_path=root / "assignment.token",
            )

            self.assertEqual(
                client.assign("thread-1", "project-1"),
                {"state": "desktop-unavailable", "notified": False},
            )

    def test_rejects_token_with_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            socket_path = root / "assignment.sock"
            token_path = root / "assignment.token"
            socket_path.touch()
            token_path.write_text("a" * 64, encoding="utf-8")
            token_path.chmod(0o644)
            client = DesktopAssignmentClient(socket_path, token_path)

            self.assertEqual(
                client.assign("thread-1", "project-1"),
                {"state": "token-unavailable", "notified": False},
            )

    def test_posts_assignment_and_accepts_no_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            socket_path = root / "assignment.sock"
            token_path = root / "assignment.token"
            socket_path.touch()
            token_path.write_text("b" * 64, encoding="utf-8")
            token_path.chmod(0o600)
            connection = _FakeConnection(204)
            client = DesktopAssignmentClient(
                socket_path,
                token_path,
                connection_factory=lambda _path, _timeout: connection,
            )

            result = client.assign("thread-1", "project-1")

            self.assertEqual(result, {"state": "notified", "notified": True})
            method, path, body, headers = connection.request_args
            self.assertEqual((method, path), ("POST", "/set-assignment"))
            self.assertEqual(
                json.loads(body),
                {
                    "threadId": "thread-1",
                    "assignment": {"projectKind": "local", "projectId": "project-1"},
                },
            )
            self.assertEqual(headers["x-codex-bridge-token"], "b" * 64)
            self.assertTrue(connection.closed)

    def test_reports_desktop_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            socket_path = root / "assignment.sock"
            token_path = root / "assignment.token"
            socket_path.touch()
            token_path.write_text("c" * 64, encoding="utf-8")
            token_path.chmod(0o600)
            connection = _FakeConnection(401)
            client = DesktopAssignmentClient(
                socket_path,
                token_path,
                connection_factory=lambda _path, _timeout: connection,
            )

            self.assertEqual(
                client.assign("thread-1", "project-1"),
                {
                    "state": "desktop-rejected",
                    "notified": False,
                    "status_code": 401,
                },
            )


if __name__ == "__main__":
    unittest.main()
