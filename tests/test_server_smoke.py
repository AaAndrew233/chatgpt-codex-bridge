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
            messages = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "1.0"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ]
            payload = "".join(json.dumps(item) + "\n" for item in messages).encode()
            environment = {
                **os.environ,
                "CODEX_BRIDGE_CONFIG": str(config_path),
            }

            completed = subprocess.run(
                [sys.executable, str(root / "server.py")],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=root,
                env=environment,
                timeout=20,
                check=False,
            )

        responses = [
            json.loads(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        tool_response = next(
            (response for response in responses if response.get("id") == 2),
            None,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace")[:1000],
        )
        self.assertIsNotNone(tool_response)
        tools = {
            item.get("name")
            for item in tool_response.get("result", {}).get("tools", [])
        }
        self.assertEqual(tools, EXPECTED_TOOLS)

        status_messages = [
            messages[0],
            messages[1],
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "codex_status", "arguments": {}},
            },
        ]
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
            status_completed = subprocess.run(
                [sys.executable, str(root / "server.py")],
                input=("".join(json.dumps(item) + "\n" for item in status_messages)).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=root,
                env={**os.environ, "CODEX_BRIDGE_CONFIG": str(status_config)},
                timeout=20,
                check=False,
            )
        status_responses = [
            json.loads(line)
            for line in status_completed.stdout.splitlines()
            if line.strip()
        ]
        status_response = next(
            (response for response in status_responses if response.get("id") == 3),
            None,
        )
        self.assertEqual(
            status_completed.returncode,
            0,
            status_completed.stderr.decode("utf-8", errors="replace")[:1000],
        )
        self.assertIsNotNone(status_response)
        status_text = status_response["result"]["content"][0]["text"]
        status = json.loads(status_text)
        self.assertEqual(status["bridge"]["name"], "Codex with ChatGPT")
        self.assertEqual(status["bridge"]["version"], "0.3.0")
        self.assertEqual(
            status["maintenance"]["tool_schema_update"],
            "refresh_chatgpt_connector_then_start_a_new_conversation",
        )


if __name__ == "__main__":
    unittest.main()
