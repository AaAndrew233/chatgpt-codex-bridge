import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ServerSessionAccessTests(unittest.TestCase):
    def test_projectless_session_authorization_through_mcp(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / ".codex"
            sessions = codex_home / "sessions/2026/08/24"
            sessions.mkdir(parents=True)
            workspace = Path(temp_dir) / "recent-workspace"
            workspace.mkdir()
            session_id = "019f8422-9576-75f3-81b8-67f0a13578c9"
            state_path = codex_home / ".codex-global-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "local-projects": {},
                        "sidebar-project-thread-orders": {},
                        "thread-project-assignments": {},
                        "projectless-thread-ids": [session_id],
                    }
                ),
                encoding="utf-8",
            )
            session_path = sessions / f"rollout-2026-08-24T00-00-00-{session_id}.jsonl"
            session_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": session_id,
                                    "cwd": str(workspace),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "user_message",
                                    "message": "无项目会话测试",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "codex_command": sys.executable,
                        "codex_project_catalog": str(state_path),
                        "allowed_roots": [],
                    }
                ),
                encoding="utf-8",
            )

            process = subprocess.Popen(
                [sys.executable, str(root / "server.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=root,
                env={**os.environ, "CODEX_BRIDGE_CONFIG": str(config_path)},
                text=True,
                bufsize=1,
            )
            try:
                self._request(
                    process,
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "1.0"},
                    },
                )
                self._notify(process, "notifications/initialized", {})

                listing = self._tool_call(
                    process,
                    2,
                    "codex_list_sessions",
                    {"include_unassigned": True},
                )
                item = next(
                    item
                    for item in listing["sessions"]
                    if item["session_id"] == session_id
                )
                self.assertEqual(item["access_state"], "authorization_required")
                self.assertIsNone(item["project_path"])

                denied = self._tool_call(
                    process,
                    3,
                    "codex_read_session",
                    {"session_id": session_id},
                )
                self.assertFalse(denied["ok"])

                prepared = self._tool_call(
                    process,
                    4,
                    "codex_prepare_session_access",
                    {"session_id": session_id, "access_mode": "read-only"},
                )
                self.assertEqual(prepared["workspace_path"], str(workspace.resolve()))

                result = self._tool_call(
                    process,
                    5,
                    "codex_read_session",
                    {
                        "session_id": session_id,
                        "session_access_token": prepared["session_access_token"],
                    },
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["access_scope"], "session")
                self.assertEqual(result["messages"][0]["text"], "无项目会话测试")
            finally:
                if process.stdin:
                    process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

    @staticmethod
    def _notify(
        process: subprocess.Popen[str],
        method: str,
        params: dict,
    ) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        process.stdin.flush()

    @classmethod
    def _request(
        cls,
        process: subprocess.Popen[str],
        request_id: int,
        method: str,
        params: dict,
    ) -> dict:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            + "\n"
        )
        process.stdin.flush()
        while True:
            line = process.stdout.readline()
            if not line:
                stderr = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"MCP server exited before response: {stderr[:1000]}")
            response = json.loads(line)
            if response.get("id") == request_id:
                return response

    @classmethod
    def _tool_call(
        cls,
        process: subprocess.Popen[str],
        request_id: int,
        name: str,
        arguments: dict,
    ) -> dict:
        response = cls._request(
            process,
            request_id,
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        result = response.get("result", {})
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content", []):
            if item.get("type") == "text":
                return json.loads(item["text"])
        raise AssertionError(f"Tool {name} returned no JSON payload: {response}")


if __name__ == "__main__":
    unittest.main()
