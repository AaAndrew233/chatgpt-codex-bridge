import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


EXPECTED_TOOLS = {
    "codex_status",
    "codex_list_projects",
    "codex_prepare_project_context",
    "codex_analyze",
    "codex_plan",
    "codex_prepare_apply",
    "codex_apply",
    "codex_job_status",
    "codex_job_result",
    "codex_cancel_job",
    "codex_list_sessions",
    "codex_read_session",
    "codex_prepare_session_access",
    "codex_create_desktop_session",
    "codex_continue_desktop_session",
    "codex_handoff_chat_context",
}


class ServerSmokeTests(unittest.TestCase):
    @staticmethod
    def _run_stdio_request(root: Path, config_path: Path, request: dict) -> tuple[list[dict], int, str]:
        environment = {
            **os.environ,
            "CODEX_BRIDGE_CONFIG": str(config_path),
            "PYTHONUNBUFFERED": "1",
        }
        process = subprocess.Popen(
            [sys.executable, str(root / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=root,
            env=environment,
            text=True,
            bufsize=1,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "1.0"},
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        lines = [process.stdout.readline()]
        process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            )
            + "\n"
            + json.dumps(request)
            + "\n"
        )
        process.stdin.flush()
        while True:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line)
            if json.loads(line).get("id") == request.get("id"):
                break
        process.stdin.close()
        process.wait(timeout=20)
        stderr = process.stderr.read() if process.stderr is not None else ""
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        responses = [json.loads(line) for line in lines if line.strip()]
        return responses, process.returncode, stderr

    def test_stdio_server_lists_all_public_tools(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temp_dir:
            allowed_root = Path(temp_dir) / "project"
            allowed_root.mkdir()
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "codex_command": sys.executable,
                        "model": None,
                        "codex_project_catalog": None,
                        "allowed_roots": [str(allowed_root)],
                    }
                ),
                encoding="utf-8",
            )
            responses, returncode, stderr = self._run_stdio_request(
                root,
                config_path,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
        tool_response = next(
            (response for response in responses if response.get("id") == 2),
            None,
        )
        self.assertEqual(
            returncode,
            0,
            stderr[:1000],
        )
        self.assertIsNotNone(
            tool_response,
            "\n".join(json.dumps(item) for item in responses) + stderr[:2000],
        )
        tools = {
            item.get("name")
            for item in tool_response.get("result", {}).get("tools", [])
        }
        self.assertEqual(tools, EXPECTED_TOOLS)

        with tempfile.TemporaryDirectory() as status_dir:
            status_root = Path(status_dir) / "project"
            status_root.mkdir()
            status_config = Path(status_dir) / "config.json"
            status_config.write_text(
                json.dumps(
                    {
                        "codex_command": sys.executable,
                        "model": None,
                        "codex_project_catalog": None,
                        "allowed_roots": [str(status_root)],
                    }
                ),
                encoding="utf-8",
            )
            status_responses, status_returncode, status_stderr = self._run_stdio_request(
                root,
                status_config,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "codex_status", "arguments": {}},
                },
            )
        status_response = next(
            (response for response in status_responses if response.get("id") == 3),
            None,
        )
        self.assertEqual(
            status_returncode,
            0,
            status_stderr[:1000],
        )
        self.assertIsNotNone(
            status_response,
            "\n".join(json.dumps(item) for item in status_responses) + status_stderr[:2000],
        )
        status_text = status_response["result"]["content"][0]["text"]
        status = json.loads(status_text)
        self.assertEqual(status["bridge"]["name"], "Codex with ChatGPT")
        self.assertEqual(status["bridge"]["version"], "0.3.1")
        self.assertEqual(
            status["maintenance"]["tool_schema_update"],
            "refresh_chatgpt_connector_then_start_a_new_conversation",
        )


if __name__ == "__main__":
    unittest.main()
