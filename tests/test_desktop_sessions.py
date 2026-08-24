import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from desktop_sessions import DesktopSessionClient, DesktopSessionError


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = None


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False
        self.wait_closed_calls = 0

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1


class _FakeDesktopSessionClient(DesktopSessionClient):
    def __init__(self) -> None:
        super().__init__("codex", model="gpt-5.6-sol")
        self.processes: list[_FakeProcess] = []
        self.initialized = 0
        self.stopped = 0
        self.methods: list[str] = []
        self.params: list[tuple[str, dict]] = []
        self.projects: list[dict] = []
        self.invalid_metadata_update = False
        self.fail_turn = False

    async def _start_process(self) -> _FakeProcess:
        process = _FakeProcess()
        self.processes.append(process)
        return process

    async def _initialize(self, process: _FakeProcess) -> None:
        self.initialized += 1

    async def _rpc(
        self,
        process: _FakeProcess,
        method: str,
        params: dict,
        request_id: int,
        *,
        wait_for_completion: bool = False,
    ) -> dict:
        self.methods.append(method)
        self.params.append((method, params))
        if method == "project/list":
            return {"data": self.projects, "nextCursor": None}
        if method == "project/import":
            project = {
                "id": "canonical-project-1",
                "name": params["name"],
                "roots": params["roots"],
            }
            self.projects.append(project)
            return {"project": project}
        if method == "thread/start":
            return {
                "thread": {
                    "id": "thread-new",
                    "sessionId": "thread-new",
                    "ephemeral": False,
                }
            }
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/unarchive":
            return {}
        if method == "thread/metadata/update":
            return {
                "thread": {
                    "id": params["threadId"],
                    "projectId": (
                        "wrong-project"
                        if self.invalid_metadata_update
                        else params["projectId"]
                    ),
                }
            }
        if method == "thread/name/set":
            return {}
        if method == "turn/start":
            if self.fail_turn:
                raise DesktopSessionError("模拟首轮失败")
            return {"output": "完成"}
        raise AssertionError(f"unexpected method: {method}")

    async def _stop_process(self, process: _FakeProcess) -> None:
        self.stopped += 1
        process.returncode = 0


class DesktopSessionClientTests(unittest.IsolatedAsyncioTestCase):
    def test_app_server_always_uses_standalone_stdio_transport(self) -> None:
        client = DesktopSessionClient("codex")
        self.assertEqual(
            client._app_server_command(),
            ["codex", "app-server", "--listen", "stdio://"],
        )

    async def test_app_server_uses_bounded_stream_limit_above_asyncio_default(self) -> None:
        process = _FakeProcess()
        with patch(
            "desktop_sessions.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as create_process:
            result = await DesktopSessionClient("codex")._start_process()

        self.assertIs(result, process)
        self.assertEqual(
            create_process.await_args.kwargs["limit"],
            DesktopSessionClient.STREAM_LIMIT_BYTES,
        )
        self.assertGreater(DesktopSessionClient.STREAM_LIMIT_BYTES, 64 * 1024)

    async def test_stop_process_closes_stdio_before_waiting(self) -> None:
        class Process:
            def __init__(self) -> None:
                self.returncode = None
                self.stdin = _FakeStdin()

            async def wait(self) -> None:
                self.returncode = 0

        process = Process()
        await DesktopSessionClient("codex")._stop_process(process)
        self.assertTrue(process.stdin.closed)
        self.assertEqual(process.stdin.wait_closed_calls, 1)
        self.assertEqual(process.returncode, 0)

    def test_streamed_output_is_concatenated_without_extra_newlines(self) -> None:
        self.assertEqual(
            DesktopSessionClient._combine_output(["BR", "IDGE", "_PROJECT", "_OK"]),
            "BRIDGE_PROJECT_OK",
        )

    async def test_retryable_app_server_error_does_not_abort_turn(self) -> None:
        class RetryClient(DesktopSessionClient):
            def __init__(self) -> None:
                super().__init__("codex", timeout_seconds=5)
                self.messages = [
                    {
                        "method": "error",
                        "params": {
                            "error": {"message": "Reconnecting... 1/5"},
                            "willRetry": True,
                        },
                    },
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"delta": "2"},
                    },
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn-1", "status": "completed"}},
                    },
                ]

            async def _send(self, process, message) -> None:
                return None

            async def _read_until_id(self, process, request_id: int) -> dict:
                return {"turn": {"id": "turn-1"}}

            async def _read_message(self, process, timeout: float) -> dict:
                return self.messages.pop(0)

        result = await RetryClient()._rpc(
            _FakeProcess(),
            "turn/start",
            {"threadId": "thread-1", "input": []},
            1,
            wait_for_completion=True,
        )

        self.assertEqual(result, {"output": "2"})

    async def test_terminal_app_server_error_reports_sanitized_http_status(self) -> None:
        class ErrorClient(DesktopSessionClient):
            async def _send(self, process, message) -> None:
                return None

            async def _read_until_id(self, process, request_id: int) -> dict:
                return {"turn": {"id": "turn-1"}}

            async def _read_message(self, process, timeout: float) -> dict:
                return {
                    "method": "error",
                    "params": {
                        "error": "unexpected status 503 Service Unavailable at private URL"
                    },
                }

        with self.assertRaisesRegex(
            Exception,
            r"HTTP 503.*自动重试未恢复",
        ):
            await ErrorClient("codex")._rpc(
                _FakeProcess(),
                "turn/start",
                {"threadId": "thread-1", "input": []},
                1,
                wait_for_completion=True,
            )

    async def test_new_thread_runs_first_turn_on_same_process(self) -> None:
        client = _FakeDesktopSessionClient()

        thread = await client.create_thread(Path("/tmp/project"), "project-1", "read-only")
        result = await client.run_turn(
            thread["session_id"], Path("/tmp/project"), "检查项目", "read-only"
        )

        self.assertEqual(result["output"], "完成")
        self.assertEqual(
            client.methods,
            [
                "project/list",
                "project/import",
                "thread/start",
                "turn/start",
                "thread/name/set",
                "thread/unarchive",
                "thread/metadata/update",
            ],
        )
        thread_params = next(
            params for method, params in client.params if method == "thread/start"
        )
        self.assertEqual(thread_params["projectId"], "canonical-project-1")
        self.assertEqual(thread_params["model"], "gpt-5.6-sol")
        turn_params = next(
            params for method, params in client.params if method == "turn/start"
        )
        self.assertEqual(turn_params["model"], "gpt-5.6-sol")
        self.assertEqual(turn_params["cwd"], str(Path("/tmp/project").resolve()))
        self.assertEqual(turn_params["approvalPolicy"], "never")
        self.assertEqual(
            turn_params["sandboxPolicy"],
            {"type": "readOnly", "networkAccess": False},
        )
        metadata_params = next(
            params
            for method, params in client.params
            if method == "thread/metadata/update"
        )
        self.assertEqual(metadata_params["projectId"], "canonical-project-1")
        name_params = next(
            params for method, params in client.params if method == "thread/name/set"
        )
        self.assertEqual(name_params["name"], "检查项目")
        self.assertEqual(len(client.processes), 2)
        self.assertEqual(client.initialized, 2)
        self.assertEqual(client.stopped, 2)

    async def test_existing_canonical_project_is_reused(self) -> None:
        client = _FakeDesktopSessionClient()
        client.projects.append(
            {
                "id": "canonical-existing",
                "name": "项目",
                "roots": [{"path": "/tmp/project"}],
            }
        )

        thread = await client.create_thread(
            Path("/tmp/project"),
            "legacy-project",
            "read-only",
            project_root=Path("/tmp/project"),
            project_name="项目",
        )

        self.assertEqual(thread["project_id"], "canonical-existing")
        self.assertEqual(
            client.methods,
            ["project/list", "thread/start"],
        )
        thread_params = next(
            params for method, params in client.params if method == "thread/start"
        )
        self.assertEqual(thread_params["projectId"], "canonical-existing")
        self.assertEqual(thread_params["model"], "gpt-5.6-sol")

    async def test_new_thread_rejects_unconfirmed_project_binding(self) -> None:
        client = _FakeDesktopSessionClient()
        client.invalid_metadata_update = True
        thread = await client.create_thread(
            Path("/tmp/project"), "project-1", "read-only"
        )

        with self.assertRaisesRegex(Exception, "未确认新会话的项目归属"):
            await client.run_turn(
                thread["session_id"],
                Path("/tmp/project"),
                "检查项目",
                "read-only",
            )

        self.assertEqual(client.stopped, 2)

    async def test_failed_first_turn_still_updates_project_binding(self) -> None:
        client = _FakeDesktopSessionClient()
        client.fail_turn = True
        thread = await client.create_thread(
            Path("/tmp/project"), "project-1", "read-only"
        )

        with self.assertRaisesRegex(DesktopSessionError, "模拟首轮失败"):
            await client.run_turn(
                thread["session_id"],
                Path("/tmp/project"),
                "检查项目",
                "read-only",
            )

        self.assertIn("thread/metadata/update", client.methods)
        self.assertIn("thread/unarchive", client.methods)
        self.assertEqual(client.stopped, 2)

    def test_handoff_thread_name_uses_current_task(self) -> None:
        request = (
            "以下是 ChatGPT 主动交接的当前会话参考资料。\n\n"
            "【ChatGPT 会话上下文】\n背景内容\n\n"
            "【当前任务】\n检查项目并给出方案\n补充要求"
        )

        self.assertEqual(
            DesktopSessionClient._thread_name(request),
            "检查项目并给出方案",
        )

    async def test_existing_thread_can_be_assigned_to_project(self) -> None:
        client = _FakeDesktopSessionClient()

        result = await client.assign_thread_project(
            "thread-existing",
            Path("/tmp/project"),
            "项目",
            "legacy-project",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            client.methods,
            ["project/list", "project/import", "thread/metadata/update"],
        )
        update_params = next(
            params
            for method, params in client.params
            if method == "thread/metadata/update"
        )
        self.assertEqual(update_params["projectId"], "canonical-project-1")

    async def test_existing_thread_rejects_unconfirmed_project_binding(self) -> None:
        client = _FakeDesktopSessionClient()
        client.invalid_metadata_update = True

        with self.assertRaisesRegex(Exception, "未确认会话项目归属"):
            await client.assign_thread_project(
                "thread-existing",
                Path("/tmp/project"),
                "项目",
                "legacy-project",
            )

    async def test_existing_thread_uses_resume_on_new_process(self) -> None:
        client = _FakeDesktopSessionClient()

        result = await client.run_turn(
            "thread-existing", Path("/tmp/project"), "继续", "read-only"
        )

        self.assertEqual(result["output"], "完成")
        self.assertEqual(client.methods, ["thread/resume", "turn/start"])
        turn_params = next(
            params for method, params in client.params if method == "turn/start"
        )
        self.assertEqual(turn_params["model"], "gpt-5.6-sol")
        self.assertEqual(turn_params["cwd"], str(Path("/tmp/project").resolve()))
        self.assertEqual(
            turn_params["sandboxPolicy"],
            {"type": "readOnly", "networkAccess": False},
        )
        self.assertEqual(len(client.processes), 1)
        self.assertEqual(client.initialized, 1)
        self.assertEqual(client.stopped, 1)

    async def test_existing_thread_enforces_workspace_write_policy(self) -> None:
        client = _FakeDesktopSessionClient()

        await client.run_turn(
            "thread-existing", Path("/tmp/project"), "修改", "workspace-write"
        )

        turn_params = next(
            params for method, params in client.params if method == "turn/start"
        )
        self.assertEqual(turn_params["approvalPolicy"], "never")
        self.assertEqual(
            turn_params["sandboxPolicy"],
            {
                "type": "workspaceWrite",
                "writableRoots": [str(Path("/tmp/project").resolve())],
                "networkAccess": False,
                "excludeSlashTmp": True,
                "excludeTmpdirEnvVar": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
