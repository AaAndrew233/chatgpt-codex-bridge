"""通过 Codex app-server 创建和继续可由 Desktop 读取的持久会话。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from bridge_core import BridgeError, SENSITIVE_ENV_KEYS


LOGGER = logging.getLogger("codex-bridge.desktop")


class DesktopSessionError(BridgeError):
    """Desktop 会话协议错误。"""


class DesktopSessionClient:
    """每次操作使用一个短生命周期 app-server 连接，持久历史由 Codex 保存。"""

    STREAM_LIMIT_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        codex_command: str,
        *,
        model: str | None = None,
        timeout_seconds: int = 1200,
    ) -> None:
        self.codex_command = codex_command
        self.model = model
        self.timeout_seconds = timeout_seconds
        # thread/start 创建的线程要在同一 app-server 进程里完成首轮 turn，
        # 否则首轮 rollout 尚未落盘时，另一个进程无法 thread/resume。
        self._pending_processes: dict[str, asyncio.subprocess.Process] = {}
        self._pending_project_ids: dict[str, str] = {}

    async def create_thread(
        self,
        project: Path,
        project_id: str,
        mode: str,
        *,
        project_root: Path | None = None,
        project_name: str | None = None,
    ) -> dict[str, Any]:
        process = await self._start_process()
        try:
            await self._initialize(process)
            registered_root = (project_root or project).expanduser().resolve()
            canonical_project_id = await self._ensure_project(
                process,
                registered_root,
                project_name or registered_root.name,
                project_id,
            )
            thread_params: dict[str, Any] = {
                "cwd": str(project),
                "ephemeral": False,
                "approvalPolicy": "never",
                "sandbox": mode,
                "serviceName": "codex_bridge",
                "projectId": canonical_project_id,
            }
            if self.model is not None:
                thread_params["model"] = self.model
            result = await self._rpc(
                process,
                "thread/start",
                thread_params,
                200,
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise DesktopSessionError("Codex app-server 未返回有效的 Desktop 会话 ID。")
            session_id = thread.get("sessionId") or thread["id"]
            if not isinstance(session_id, str):
                raise DesktopSessionError("Codex app-server 未返回有效的 Desktop 会话 ID。")
            self._pending_processes[session_id] = process
            self._pending_project_ids[session_id] = canonical_project_id
            return {
                "thread_id": thread["id"],
                "session_id": session_id,
                "project_id": canonical_project_id,
                "source_project_id": project_id,
                "project": str(project),
                "model": self.model,
                "ephemeral": bool(thread.get("ephemeral", False)),
            }
        except Exception:
            await self._stop_process(process)
            raise

    async def assign_thread_project(
        self,
        session_id: str,
        project_root: Path,
        project_name: str,
        source_project_id: str,
    ) -> dict[str, Any]:
        """通过官方元数据接口为已有持久线程补充项目归属。"""
        if not isinstance(session_id, str) or not session_id.strip():
            raise DesktopSessionError("session_id 必须是非空字符串。")
        process = await self._start_process()
        try:
            await self._initialize(process)
            canonical_project_id = await self._ensure_project(
                process,
                project_root.expanduser().resolve(),
                project_name,
                source_project_id,
            )
            result = await self._rpc(
                process,
                "thread/metadata/update",
                {"threadId": session_id.strip(), "projectId": canonical_project_id},
                200,
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            if (
                not isinstance(thread, dict)
                or thread.get("id") != session_id.strip()
                or thread.get("projectId") != canonical_project_id
            ):
                raise DesktopSessionError("Codex app-server 未确认会话项目归属。")
            return {
                "ok": True,
                "session_id": session_id.strip(),
                "project_id": canonical_project_id,
                "project": str(project_root),
            }
        finally:
            await self._stop_process(process)

    async def _ensure_project(
        self,
        process: asyncio.subprocess.Process,
        project_root: Path,
        project_name: str,
        source_project_id: str,
    ) -> str:
        """按根目录幂等查找或导入 app-server canonical 项目。"""
        matches: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(20):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await self._rpc(process, "project/list", params, 10 + page)
            projects = result.get("data") if isinstance(result, dict) else None
            if not isinstance(projects, list):
                raise DesktopSessionError("Codex app-server 项目列表格式无效。")
            for candidate in projects:
                if not isinstance(candidate, dict):
                    continue
                roots = candidate.get("roots")
                if not isinstance(roots, list):
                    continue
                if any(
                    self._same_path(root.get("path"), project_root)
                    for root in roots
                    if isinstance(root, dict)
                ):
                    matches.append(candidate)
            cursor = result.get("nextCursor") if isinstance(result.get("nextCursor"), str) else None
            if not cursor:
                break
        else:
            raise DesktopSessionError("Codex app-server 项目列表分页超过安全上限。")

        project_ids = {item.get("id") for item in matches if isinstance(item.get("id"), str)}
        if len(project_ids) > 1:
            raise DesktopSessionError("同一项目目录对应多个 Codex 项目，已停止自动绑定。")
        if project_ids:
            return next(iter(project_ids))

        imported = await self._rpc(
            process,
            "project/import",
            {
                "idempotencyKey": self._project_idempotency_key(project_root),
                "name": project_name.strip() or project_root.name,
                "roots": [{"path": str(project_root)}],
                "metadata": {"codexBridgeSourceProjectId": source_project_id},
            },
            100,
        )
        project = imported.get("project") if isinstance(imported, dict) else None
        canonical_project_id = project.get("id") if isinstance(project, dict) else None
        if not isinstance(canonical_project_id, str) or not canonical_project_id:
            raise DesktopSessionError("Codex app-server 未返回有效的 canonical 项目 ID。")
        return canonical_project_id

    async def run_turn(
        self,
        session_id: str,
        project: Path,
        request: str,
        mode: str,
    ) -> dict[str, Any]:
        if not request.strip():
            raise DesktopSessionError("request 必须是非空字符串。")
        prompt = self._prompt(project, request, mode)
        process = self._pending_processes.pop(session_id, None)
        pending_project_id = self._pending_project_ids.pop(session_id, None)
        uses_pending_thread = process is not None and process.returncode is None
        if not uses_pending_thread:
            process = await self._start_process()
        turn_error: BaseException | None = None
        try:
            if not uses_pending_thread:
                await self._initialize(process)
                resumed = await self._rpc(process, "thread/resume", {"threadId": session_id}, 1)
                thread = resumed.get("thread") if isinstance(resumed, dict) else None
                if not isinstance(thread, dict):
                    raise DesktopSessionError("Codex app-server 无法恢复指定 Desktop 会话。")
            turn_params: dict[str, Any] = {
                "threadId": session_id,
                "input": [{"type": "text", "text": prompt}],
            }
            if self.model is not None:
                turn_params["model"] = self.model
            turn_result = await self._rpc(
                process,
                "turn/start",
                turn_params,
                201 if uses_pending_thread else 2,
                wait_for_completion=True,
            )
            if uses_pending_thread:
                if not isinstance(pending_project_id, str) or not pending_project_id:
                    raise DesktopSessionError("新会话缺少待确认的项目归属。")
                try:
                    await self._rpc(
                        process,
                        "thread/name/set",
                        {"threadId": session_id, "name": self._thread_name(request)},
                        202,
                    )
                except DesktopSessionError as exc:
                    LOGGER.warning("Desktop 会话标题设置失败：%s", exc)
            output = turn_result.get("output", "") if isinstance(turn_result, dict) else ""
        except BaseException as exc:
            turn_error = exc
            raise
        finally:
            await self._stop_process(process)
            # 首轮结束后 standalone app-server 可能会把线程归档。先恢复为活动线程，
            # 再补写项目归属，否则侧边栏排序虽正确，活动会话列表仍不会把它置顶。
            if uses_pending_thread:
                try:
                    await self._finalize_new_thread(session_id, pending_project_id)
                except Exception:
                    if turn_error is None:
                        raise
                    LOGGER.exception("首轮失败后恢复 Desktop 会话状态失败。")
        return {
            "ok": True,
            "session_id": session_id,
            "thread_id": session_id,
            "project": str(project),
            "mode": mode,
            "model": self.model,
            "output": output,
        }

    async def _finalize_new_thread(
        self,
        session_id: str,
        canonical_project_id: str,
    ) -> None:
        """在 rollout 持久化后恢复活动状态并确认项目归属。"""
        process = await self._start_process()
        try:
            await self._initialize(process)
            try:
                await self._rpc(
                    process,
                    "thread/unarchive",
                    {"threadId": session_id},
                    1,
                )
            except DesktopSessionError as exc:
                # 新版 Codex 可能已经保持活动状态；此时 unarchive 会报告没有
                # archived rollout，不应让一个可用会话创建失败。
                if "no archived rollout found" not in str(exc).lower():
                    raise
                LOGGER.info("Desktop 会话已处于活动状态，无需取消归档。")
            result = await self._rpc(
                process,
                "thread/metadata/update",
                {"threadId": session_id, "projectId": canonical_project_id},
                2,
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            if (
                not isinstance(thread, dict)
                or thread.get("id") != session_id
                or thread.get("projectId") != canonical_project_id
            ):
                raise DesktopSessionError("Codex app-server 未确认新会话的项目归属。")
        finally:
            await self._stop_process(process)

    async def _start_process(self) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *self._app_server_command(),
                cwd=str(Path.home()),
                env=self._environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.STREAM_LIMIT_BYTES,
            )
        except OSError as exc:
            raise DesktopSessionError(f"无法启动 Codex app-server：{type(exc).__name__}") from exc

    def _app_server_command(self) -> list[str]:
        """启动独立 stdio app-server，避免误把 Desktop IPC 当作 app-server 协议。"""
        return [self.codex_command, "app-server", "--listen", "stdio://"]

    async def _initialize(self, process: asyncio.subprocess.Process) -> None:
        await self._send(
            process,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "codex_bridge",
                        "title": "Codex Bridge",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        await self._read_until_id(process, 0)
        await self._send(process, {"method": "initialized", "params": {}})

    async def _rpc(
        self,
        process: asyncio.subprocess.Process,
        method: str,
        params: dict[str, Any],
        request_id: int,
        *,
        wait_for_completion: bool = False,
    ) -> dict[str, Any]:
        await self._send(process, {"method": method, "id": request_id, "params": params})
        response = await self._read_until_id(process, request_id)
        if not wait_for_completion:
            return response

        turn = response.get("turn") if isinstance(response, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        output_parts: list[str] = []
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while True:
            message = await self._read_message(process, max(1, deadline - asyncio.get_running_loop().time()))
            if message.get("method") == "item/agentMessage/delta":
                params_value = message.get("params")
                if isinstance(params_value, dict) and isinstance(params_value.get("delta"), str):
                    output_parts.append(params_value["delta"])
            elif message.get("method") == "item/completed":
                item = (message.get("params") or {}).get("item") if isinstance(message.get("params"), dict) else None
                if isinstance(item, dict) and item.get("type") in {"agentMessage", "agent_message"} and isinstance(item.get("text"), str):
                    if not output_parts:
                        output_parts.append(item["text"])
            elif message.get("method") == "turn/completed":
                completed = message.get("params") or {}
                completed_turn = completed.get("turn") if isinstance(completed, dict) else None
                if turn_id is None or not isinstance(completed_turn, dict) or completed_turn.get("id") == turn_id:
                    status = completed_turn.get("status") if isinstance(completed_turn, dict) else "completed"
                    if status not in {None, "completed", "succeeded"}:
                        raise DesktopSessionError(f"Desktop 会话回合未完成：{status}")
                    return {"output": self._combine_output(output_parts)}
            elif message.get("method") == "error":
                params_value = message.get("params")
                if isinstance(params_value, dict) and params_value.get("willRetry") is True:
                    LOGGER.info("Codex app-server 遇到临时错误，等待其自动重试。")
                    continue
                raise DesktopSessionError(self._app_server_error_message(message))

    @staticmethod
    def _app_server_error_message(message: dict[str, Any]) -> str:
        params_value = message.get("params")
        error_value = params_value.get("error") if isinstance(params_value, dict) else None
        status_code: int | None = None
        raw_message: str | None = None
        if isinstance(error_value, dict):
            raw_message = error_value.get("message")
            error_info = error_value.get("codexErrorInfo")
            disconnected = (
                error_info.get("responseStreamDisconnected")
                if isinstance(error_info, dict)
                else None
            )
            if isinstance(disconnected, dict) and isinstance(
                disconnected.get("httpStatusCode"), int
            ):
                status_code = disconnected["httpStatusCode"]
        elif isinstance(error_value, str):
            raw_message = error_value
        if status_code is None and isinstance(raw_message, str):
            match = re.search(r"\bstatus\s+(\d{3})\b", raw_message, re.IGNORECASE)
            if match is not None:
                status_code = int(match.group(1))
        if status_code is not None:
            return f"Codex 上游模型服务请求失败（HTTP {status_code}），自动重试未恢复。"
        return "Codex app-server 返回错误。"

    async def _read_until_id(self, process: asyncio.subprocess.Process, request_id: int) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            message = await self._read_message(process, max(1, deadline - asyncio.get_running_loop().time()))
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict):
                error = message["error"]
                raise DesktopSessionError(f"Codex app-server 请求失败：{error.get('message', '未知错误')}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise DesktopSessionError("Codex app-server 返回格式无效。")
            return result

    async def _read_message(self, process: asyncio.subprocess.Process, timeout: float) -> dict[str, Any]:
        if process.stdout is None:
            raise DesktopSessionError("Codex app-server 输出管道不可用。")
        try:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            except ValueError as exc:
                raise DesktopSessionError(
                    "Codex app-server 单条消息超过 4 MiB 安全上限。"
                ) from exc
        except asyncio.TimeoutError as exc:
            raise DesktopSessionError("Codex Desktop 会话响应超时。") from exc
        if not line:
            detail = ""
            if process.stderr is not None:
                try:
                    detail = (await asyncio.wait_for(process.stderr.read(4096), timeout=0.2)).decode("utf-8", errors="replace").strip()
                except Exception:
                    detail = ""
            raise DesktopSessionError(f"Codex app-server 已退出。{detail[:500]}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DesktopSessionError("Codex app-server 返回了无效 JSON。") from exc
        if not isinstance(message, dict):
            raise DesktopSessionError("Codex app-server 返回了无效消息。")
        return message

    async def _send(self, process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise DesktopSessionError("Codex app-server 输入管道不可用。")
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            # 先用 stdio EOF 让 app-server 清理连接级资源（包括线程写锁）。
            # 直接 SIGTERM 可能在锁释放前结束进程，导致下一次 thread/resume
            # 收到 "thread already has an active writer"。
            if process.stdin is not None:
                process.stdin.close()
                wait_closed = getattr(process.stdin, "wait_closed", None)
                if wait_closed is not None:
                    try:
                        await asyncio.wait_for(wait_closed(), timeout=1)
                    except (asyncio.TimeoutError, BrokenPipeError, ConnectionError):
                        pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    @staticmethod
    def _prompt(project: Path, request: str, mode: str) -> str:
        restriction = (
            "只读分析，不修改文件，不访问凭据。"
            if mode == "read-only"
            else "只允许修改当前项目目录内文件，不删除项目，不读取密钥。"
        )
        return (
            "你正在通过 Codex Bridge 的 Desktop 持久会话工作。\n"
            f"工作目录：{project}\n安全约束：{restriction}\n"
            "不要输出 Token、Cookie、密码、环境变量或完整连接串。\n\n"
            f"用户请求：\n{request.strip()}"
        )

    @staticmethod
    def _same_path(value: Any, expected: Path) -> bool:
        if not isinstance(value, str) or not value:
            return False
        try:
            return Path(value).expanduser().resolve() == expected
        except OSError:
            return False

    @staticmethod
    def _project_idempotency_key(project_root: Path) -> str:
        digest = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()
        return f"codex-bridge-project-v1:{digest}"

    @staticmethod
    def _thread_name(request: str) -> str:
        task_marker = "【当前任务】"
        title_source = request.split(task_marker, 1)[1] if task_marker in request else request
        first_line = next(
            (line.strip() for line in title_source.splitlines() if line.strip()),
            "Codex 任务",
        )
        return first_line[:80]

    @staticmethod
    def _combine_output(parts: list[str]) -> str:
        return "".join(parts).strip()

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"PATH", "HOME", "CODEX_HOME", "TMPDIR", "NO_COLOR", "TERM", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        allowed |= SENSITIVE_ENV_KEYS | {"CODEX_API_KEY"}
        return {key: value for key, value in os.environ.items() if key in allowed}
