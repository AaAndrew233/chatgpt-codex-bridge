"""Codex Bridge MCP 服务入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - 启动环境提示
    print(
        "缺少 mcp 依赖，请先运行 scripts/bootstrap.sh",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

from bridge_core import (
    BridgeConfig,
    BridgeError,
    CodexRunner,
    ConfirmationStore,
    JobStore,
    SessionAccessStore,
    compose_chat_handoff,
    unwrap_user_request,
    wrap_user_request,
)
from conversation_catalog import ConversationCatalog, ConversationError
from desktop_assignment import DesktopAssignmentClient
from desktop_sessions import DesktopSessionClient, DesktopSessionError
from project_context import ProjectContextCollector


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)

CONFIG_PATH = Path(
    os.environ.get(
        "CODEX_BRIDGE_CONFIG",
        str(Path(__file__).with_name("config.json")),
    )
).expanduser()


def _startup() -> tuple[BridgeConfig, CodexRunner, ConfirmationStore]:
    config = BridgeConfig.load(CONFIG_PATH)
    return config, CodexRunner(config), ConfirmationStore(config.confirmation_ttl_seconds)


CONFIG, RUNNER, CONFIRMATIONS = _startup()
SESSION_ACCESS = SessionAccessStore(
    CONFIG.confirmation_ttl_seconds,
    CONFIG.session_access_ttl_seconds,
)
DESKTOP_CLIENT = DesktopSessionClient(
    CONFIG.codex_command,
    model=CONFIG.model,
    timeout_seconds=CONFIG.apply_timeout_seconds,
)
DESKTOP_ASSIGNMENTS = DesktopAssignmentClient()


class _CompositeRunner:
    """让现有 JobStore 同时承载 CLI 任务和 Desktop 持久会话回合。"""

    def __init__(
        self,
        codex_runner: CodexRunner,
        desktop_client: DesktopSessionClient,
        desktop_assignments: DesktopAssignmentClient,
        project_context: ProjectContextCollector | None,
        config: BridgeConfig,
    ) -> None:
        self._codex_runner = codex_runner
        self._desktop_client = desktop_client
        self._desktop_assignments = desktop_assignments
        self._project_context = project_context
        self._config = config

    async def run(self, project: Path, request: str, mode: str) -> dict[str, Any]:
        user_request = unwrap_user_request(request)
        if user_request is not None:
            return await self._codex_runner.run(project, user_request, mode)

        context_marker = "__codex_project_context__"
        if request.startswith(context_marker):
            if self._project_context is None:
                raise BridgeError("未配置 Codex 会话索引。")
            try:
                payload = json.loads(request[len(context_marker):])
            except (TypeError, ValueError) as exc:
                raise BridgeError("项目上下文任务标记无效。") from exc
            include_archived = payload.get("include_archived", False)
            max_context_chars = payload.get(
                "max_context_chars", self._config.project_context_max_chars
            )
            if not isinstance(include_archived, bool):
                raise BridgeError("include_archived 必须是布尔值。")
            try:
                return await asyncio.to_thread(
                    self._project_context.prepare,
                    str(project),
                    include_archived=include_archived,
                    max_context_chars=max_context_chars,
                    max_sessions=self._config.project_context_max_sessions,
                    max_total_scan_bytes=self._config.project_context_max_total_scan_bytes,
                    max_session_scan_bytes=self._config.project_context_max_session_scan_bytes,
                )
            except ConversationError as exc:
                raise BridgeError(str(exc)) from exc

        marker = "__codex_desktop_turn__"
        if request.startswith(marker):
            try:
                payload = json.loads(request[len(marker):])
            except (TypeError, ValueError) as exc:
                raise BridgeError("Desktop 会话任务标记无效。") from exc
            session_id = payload.get("session_id")
            prompt = payload.get("request")
            project_id = payload.get("project_id")
            if not isinstance(session_id, str) or not isinstance(prompt, str):
                raise BridgeError("Desktop 会话任务缺少 session_id 或 request。")
            if project_id is not None and not isinstance(project_id, str):
                raise BridgeError("Desktop 会话任务的 project_id 无效。")
            try:
                result = await self._desktop_client.run_turn(
                    session_id, project, prompt, mode
                )
            finally:
                # thread/start 返回时会话可能尚未进入 Desktop 当前进程的任务缓存。
                # 回合结束或失败后再次通知，确保刷新发生在持久状态稳定之后。
                sidebar_sync = (
                    self._desktop_assignments.assign(session_id, project_id)
                    if project_id is not None
                    else {"notified": False, "state": "not-applicable-projectless"}
                )
            result["sidebar_sync"] = sidebar_sync
            return result
        return await self._codex_runner.run(project, request, mode)


CONVERSATIONS = (
    ConversationCatalog(
        CONFIG.codex_project_catalog,
        CONFIG.authorized_projects,
        SESSION_ACCESS.allows,
    )
    if CONFIG.codex_project_catalog is not None
    else None
)
PROJECT_CONTEXT = (
    ProjectContextCollector(CONVERSATIONS)
    if CONVERSATIONS is not None
    else None
)
JOBS = JobStore(
    _CompositeRunner(
        RUNNER,
        DESKTOP_CLIENT,
        DESKTOP_ASSIGNMENTS,
        PROJECT_CONTEXT,
        CONFIG,
    ),
    max_request_chars=CONFIG.max_request_chars,
    max_result_output_chars=CONFIG.max_output_chars,
)
MCP = FastMCP("Codex 本地桥接")
TOOL_NAMES = [
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
]


def _project_payloads(projects: tuple[Any, ...]) -> list[dict[str, Any]]:
    """将已授权项目转换为稳定、可供连接器读取的摘要。"""
    return [
        {
            "project_id": project.project_id,
            "name": project.name,
            "root_paths": [str(root) for root in project.roots],
            "is_git_repository": any(
                (root / ".git").exists() for root in project.roots
            ),
            "source": project.source,
        }
        for project in projects
    ]


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_status(
    session_id: str | None = None,
    include_session_snapshot: bool = False,
) -> dict[str, Any]:
    """检查本机 Codex CLI；会话快照仅在调用方显式请求时返回。"""
    try:
        if not isinstance(include_session_snapshot, bool):
            raise BridgeError("include_session_snapshot 必须是布尔值。")
        command = CONFIG.safe_command()
        projects = CONFIG.authorized_projects()
        session_payload: dict[str, Any] = {
            "enabled": CONVERSATIONS is not None,
            "visibility": "codex-sidebar-visible-only",
            "tools": [
                "codex_list_sessions",
                "codex_read_session",
                "codex_prepare_session_access",
                "codex_prepare_project_context",
                "codex_create_desktop_session",
                "codex_continue_desktop_session",
                "codex_handoff_chat_context",
            ],
        }
        if CONVERSATIONS is not None:
            try:
                if session_id:
                    session_payload["requested_session"] = CONVERSATIONS.read_session(session_id)
                elif include_session_snapshot:
                    session_payload["legacy_snapshot"] = CONVERSATIONS.compatibility_snapshot()
            except ConversationError as exc:
                session_payload["error"] = str(exc)
        return {
            "ok": True,
            "codex_command": command,
            "manual_allowed_roots": [str(root) for root in CONFIG.allowed_roots],
            "codex_project_catalog": (
                str(CONFIG.codex_project_catalog)
                if CONFIG.codex_project_catalog is not None
                else None
            ),
            "authorized_project_count": len(projects),
            "projects": _project_payloads(projects),
            "analysis_timeout_seconds": CONFIG.analysis_timeout_seconds,
            "apply_timeout_seconds": CONFIG.apply_timeout_seconds,
            "model": CONFIG.model,
            "max_handoff_context_chars": CONFIG.max_handoff_context_chars,
            "max_request_chars": CONFIG.max_request_chars,
            "session_access_ttl_seconds": CONFIG.session_access_ttl_seconds,
            "project_context": {
                "max_context_chars": CONFIG.project_context_max_chars,
                "max_sessions": CONFIG.project_context_max_sessions,
                "max_total_scan_bytes": CONFIG.project_context_max_total_scan_bytes,
                "max_session_scan_bytes": CONFIG.project_context_max_session_scan_bytes,
                "execution_mode": "background_job",
            },
            "execution_mode": "background_jobs",
            "active_jobs": JOBS.active_count,
            "jobs": JOBS.list_snapshots(10),
            "conversation_access": session_payload,
            "available_tools": TOOL_NAMES,
        }
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_list_projects() -> dict[str, Any]:
    """动态列出 Codex Desktop 登记的本地项目及手工授权项目。"""
    projects = CONFIG.authorized_projects()
    return {"ok": True, "count": len(projects), "projects": _project_payloads(projects)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
async def codex_prepare_project_context(
    project_path: str,
    include_archived: bool = False,
    max_context_chars: int | None = None,
) -> dict[str, Any]:
    """自动收集项目全部可见 Codex 会话；调用者应自行查询任务并取完结果，无需用户处理分页。"""
    try:
        project = CONFIG.resolve_project(project_path)
        requested_max = (
            CONFIG.project_context_max_chars
            if max_context_chars is None
            else max_context_chars
        )
        if (
            not isinstance(requested_max, int)
            or isinstance(requested_max, bool)
            or not 10000 <= requested_max <= CONFIG.project_context_max_chars
        ):
            raise BridgeError(
                "max_context_chars 必须是 10000 到项目上下文配置上限之间的整数。"
            )
        if not isinstance(include_archived, bool):
            raise BridgeError("include_archived 必须是布尔值。")
        marker = "__codex_project_context__" + json.dumps(
            {
                "include_archived": include_archived,
                "max_context_chars": requested_max,
            },
            ensure_ascii=False,
        )
        result = JOBS.submit(
            project,
            marker,
            "read-only",
            request_size_chars=0,
        )
        result.update(
            {
                "operation": "prepare-project-context",
                "include_archived": include_archived,
                "max_context_chars": requested_max,
                "next_step": (
                    "自动调用 codex_job_status 等待任务完成，再调用 codex_job_result；"
                    "若 output_has_more=true，应自动传入 next_output_offset 直到取完，"
                    "不要要求用户管理分页参数。"
                ),
            }
        )
        return result
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_analyze(project_path: str, request: str) -> dict[str, Any]:
    """提交只读 Codex 分析并立即返回任务 ID，不修改文件。"""
    try:
        legacy_prefix = "读取 Codex 会话："
        if CONVERSATIONS is not None and isinstance(request, str):
            requested = request.strip()
            session_match = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", requested)
            if requested.startswith(legacy_prefix) or (session_match and "读取" in requested):
                session_id = session_match.group(0) if session_match else ""
                if not session_id:
                    raise BridgeError("旧版连接器读取会话时，请提供会话 UUID。")
                return CONVERSATIONS.read_session(session_id)
        project = CONFIG.resolve_project(project_path)
        return JOBS.submit(
            project,
            wrap_user_request(request),
            "read-only",
            request_size_chars=len(request),
        )
    except (BridgeError, ConversationError, DesktopSessionError) as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_plan(project_path: str, request: str) -> dict[str, Any]:
    """提交只读实现计划并立即返回任务 ID，不修改文件。"""
    plan_request = (
        "请只输出实施计划、影响范围、风险、测试策略和待确认项，不要修改任何文件。\n"
        + request
    )
    return await codex_analyze(project_path, plan_request)


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
async def codex_prepare_apply(project_path: str, request: str) -> dict[str, Any]:
    """为一次待确认的文件修改生成短时一次性确认令牌，不执行修改。"""
    try:
        project = CONFIG.resolve_project(project_path)
        if not isinstance(request, str) or not request.strip():
            raise BridgeError("request 必须是非空字符串。")
        if len(request) > CONFIG.max_request_chars:
            raise BridgeError(
                f"request 过长，最多允许 {CONFIG.max_request_chars} 个字符。"
            )
        token = CONFIRMATIONS.issue(project, request)
        return {
            "ok": True,
            "project": str(project),
            "confirmation_token": token,
            "expires_in_seconds": CONFIG.confirmation_ttl_seconds,
            "next_step": "先向用户展示计划并取得明确确认，再调用 codex_apply。",
        }
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
async def codex_apply(
    project_path: str,
    request: str,
    confirmation_token: str,
) -> dict[str, Any]:
    """验证一次性确认令牌后提交写入任务，并立即返回任务 ID。"""
    try:
        project = CONFIG.resolve_project(project_path)
        reservation = JOBS.reserve(project, "workspace-write")
        try:
            if not CONFIRMATIONS.consume(confirmation_token, project, request):
                raise BridgeError("确认令牌无效、已过期、已使用或与项目/请求不匹配。")
            return JOBS.start_reserved(
                reservation["job_id"],
                wrap_user_request(request),
                request_size_chars=len(request),
            )
        except BaseException:
            JOBS.discard_reserved(reservation["job_id"])
            raise
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_job_status(job_id: str) -> dict[str, Any]:
    """查询后台 Codex 任务进度；任务完成后再调用 codex_job_result。"""
    try:
        return JOBS.status(job_id)
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_job_result(job_id: str, output_offset: int = 0) -> dict[str, Any]:
    """分页获取已完成后台任务的结果；传入 next_output_offset 继续读取。"""
    try:
        return JOBS.result(job_id, output_offset=output_offset)
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_cancel_job(job_id: str) -> dict[str, Any]:
    """取消排队中或正在运行的后台 Codex 任务，不删除项目文件。"""
    try:
        return JOBS.cancel(job_id)
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}


def _project_binding(project: Path) -> tuple[str, str, str]:
    for record in CONFIG.authorized_projects():
        for root in record.roots:
            if project == root or root in project.parents:
                return record.project_id, str(root), record.name
    raise BridgeError("项目不在 Codex 已登记项目内。")


def _desktop_marker(session_id: str, project_id: str | None, request: str) -> str:
    import json

    return "__codex_desktop_turn__" + json.dumps(
        {"session_id": session_id, "project_id": project_id, "request": request},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _session_workspace(session_id: str) -> tuple[dict[str, Any], Path]:
    if CONVERSATIONS is None:
        raise BridgeError("未配置 Codex 会话索引。")
    descriptor = CONVERSATIONS.session_descriptor(session_id)
    if descriptor.get("access_scope") != "session":
        raise BridgeError("该会话已有项目授权，无需申请会话级授权。")
    workspace_value = descriptor.get("workspace_path")
    if not isinstance(workspace_value, str) or not workspace_value:
        raise BridgeError("该会话没有可安全授权的工作目录。")
    if descriptor.get("access_state") == "ineligible":
        raise BridgeError("该会话的工作目录不符合会话级授权安全策略。")
    return descriptor, Path(workspace_value).resolve()


def _require_session_access(
    session_id: str,
    mode: str,
    session_access_token: str | None,
) -> tuple[dict[str, Any], Path]:
    descriptor, workspace = _session_workspace(session_id)
    if session_access_token is not None:
        if not SESSION_ACCESS.activate(
            session_access_token,
            session_id,
            workspace,
            mode,
        ):
            raise BridgeError(
                "会话授权令牌无效、已过期、已使用，或与会话/目录/模式不匹配。"
            )
    if not SESSION_ACCESS.allows(session_id, workspace, mode):
        raise BridgeError(
            "该“最近”会话尚未获得对应模式的临时授权；请先调用 "
            "codex_prepare_session_access，并在用户确认后携带令牌重试。"
        )
    return descriptor, workspace


async def _create_desktop_session(
    project_path: str,
    request: str,
    mode: str,
    confirmation_token: str | None,
) -> dict[str, Any]:
    if mode not in {"read-only", "workspace-write"}:
        raise BridgeError("mode 只能是 read-only 或 workspace-write。")
    project = CONFIG.resolve_project(project_path)
    if not request.strip():
        raise BridgeError("request 必须是非空字符串。")
    if len(request) > CONFIG.max_request_chars:
        raise BridgeError(
            f"request 过长，最多允许 {CONFIG.max_request_chars} 个字符。"
        )
    project_id, project_root, project_name = _project_binding(project)
    reservation = JOBS.reserve(project, mode)
    try:
        if mode == "workspace-write":
            if not confirmation_token or not CONFIRMATIONS.consume(confirmation_token, project, request):
                raise BridgeError("写入 Desktop 会话必须先取得有效的一次性确认令牌。")
        thread = await DESKTOP_CLIENT.create_thread(
            project,
            project_id,
            mode,
            project_root=Path(project_root),
            project_name=project_name,
        )
        submitted = JOBS.start_reserved(
            reservation["job_id"],
            _desktop_marker(thread["session_id"], project_id, request),
            request_size_chars=len(request),
        )
    except BaseException:
        JOBS.discard_reserved(reservation["job_id"])
        raise
    sidebar_sync = DESKTOP_ASSIGNMENTS.assign(thread["thread_id"], project_id)
    if not sidebar_sync["notified"]:
        logging.getLogger(__name__).warning(
            "Codex Desktop 侧边栏项目归属通知未完成 state=%s",
            sidebar_sync["state"],
        )
    return {
        "ok": True,
        "desktop_session": True,
        "thread_id": thread["thread_id"],
        "session_id": thread["session_id"],
        "project": project_root,
        "mode": mode,
        "model": CONFIG.model,
        "job_id": submitted["job_id"],
        "status": submitted["status"],
        "sidebar_sync": sidebar_sync,
        "next_step": "任务完成后，Codex Desktop 会话可继续使用；调用 codex_job_status 或 codex_status 查看结果。",
    }


async def _handoff_chat_context(
    project_path: str,
    request: str,
    chat_context: str,
    mode: str,
    confirmation_token: str | None,
) -> dict[str, Any]:
    """把 ChatGPT 主动传入的上下文作为 Desktop 会话首轮参考资料。"""
    handoff_request = compose_chat_handoff(
        request,
        chat_context,
        CONFIG.max_handoff_context_chars,
        CONFIG.max_request_chars,
    )
    result = await _create_desktop_session(
        project_path,
        handoff_request,
        mode,
        confirmation_token,
    )
    result.update(
        {
            "context_chars": len(chat_context.strip()),
            "context_policy": "explicit-input-only; untrusted-reference; secrets-rejected",
        }
    )
    return result


async def _continue_desktop_session(
    session_id: str,
    request: str,
    mode: str,
    project_path: str | None,
    confirmation_token: str | None,
    session_access_token: str | None,
) -> dict[str, Any]:
    """校验会话归属后提交一个继续同一 Desktop 线程的后台回合。"""
    if CONVERSATIONS is None:
        raise BridgeError("未配置 Codex 会话索引。")
    if not isinstance(session_id, str) or not session_id.strip():
        raise BridgeError("session_id 必须是非空字符串。")
    if not isinstance(request, str) or not request.strip():
        raise BridgeError("request 必须是非空字符串。")
    if len(request) > CONFIG.max_request_chars:
        raise BridgeError(
            f"request 过长，最多允许 {CONFIG.max_request_chars} 个字符。"
        )
    if mode not in {"read-only", "workspace-write"}:
        raise BridgeError("mode 只能是 read-only 或 workspace-write。")

    descriptor = CONVERSATIONS.session_descriptor(session_id)
    access_scope = descriptor.get("access_scope")
    if access_scope == "project":
        project_root = descriptor.get("project_root")
        if not isinstance(project_root, str) or not project_root:
            raise BridgeError("该会话缺少有效的项目授权目录。")
        selected_path = project_path or project_root
        project = CONFIG.resolve_project(selected_path)
        project_id, bound_root, _ = _project_binding(project)
        if (
            descriptor.get("project_id") != project_id
            or Path(bound_root).resolve() != Path(project_root).resolve()
        ):
            raise BridgeError("project_path 与该会话原有项目不匹配。")
    elif access_scope == "session":
        descriptor, project = _require_session_access(
            session_id,
            mode,
            session_access_token,
        )
        if project_path is not None and Path(project_path).expanduser().resolve() != project:
            raise BridgeError("project_path 与会话级授权绑定的工作目录不匹配。")
        project_id = None
    else:
        raise BridgeError("该会话不符合项目授权或会话级授权策略。")
    reservation = JOBS.reserve(project, mode)
    try:
        if mode == "workspace-write":
            if not confirmation_token or not CONFIRMATIONS.consume(confirmation_token, project, request):
                raise BridgeError("写入 Desktop 会话必须先取得有效的一次性确认令牌。")
        submitted = JOBS.start_reserved(
            reservation["job_id"],
            _desktop_marker(session_id, project_id, request),
            request_size_chars=len(request),
        )
    except BaseException:
        JOBS.discard_reserved(reservation["job_id"])
        raise
    return {
        "ok": True,
        "desktop_session": True,
        "session_id": session_id,
        "project": str(project),
        "mode": mode,
        "model": CONFIG.model,
        "job_id": submitted["job_id"],
        "status": submitted["status"],
        "next_step": "调用 codex_job_status 查询回合进度，完成后调用 codex_job_result 获取结果。",
    }


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
async def codex_create_desktop_session(
    project_path: str,
    request: str,
    mode: str = "read-only",
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """创建持久 Codex Desktop 会话并执行首轮请求；写入模式需要确认令牌。"""
    try:
        return await _create_desktop_session(project_path, request, mode, confirmation_token)
    except (BridgeError, DesktopSessionError) as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
async def codex_handoff_chat_context(
    project_path: str,
    request: str,
    chat_context: str,
    mode: str = "read-only",
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """将 ChatGPT 主动整理传入的当前会话上下文交给新的 Codex Desktop 会话。

    该工具不读取 URL、conversation ID、Cookie 或 ChatGPT 网页私有接口；调用方应传入
    与当前任务相关的摘要或原文片段。上下文会被当作不可信参考资料，不会覆盖本地安全约束。
    """
    try:
        return await _handoff_chat_context(
            project_path,
            request,
            chat_context,
            mode,
            confirmation_token,
        )
    except (BridgeError, DesktopSessionError) as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
async def codex_continue_desktop_session(
    session_id: str,
    request: str,
    mode: str = "read-only",
    project_path: str | None = None,
    confirmation_token: str | None = None,
    session_access_token: str | None = None,
) -> dict[str, Any]:
    """继续已有 Codex Desktop 会话；项目路径默认从会话索引解析。"""
    try:
        return await _continue_desktop_session(
            session_id,
            request,
            mode,
            project_path,
            confirmation_token,
            session_access_token,
        )
    except (BridgeError, ConversationError, DesktopSessionError) as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
async def codex_prepare_session_access(
    session_id: str,
    access_mode: str = "read-only",
    request: str | None = None,
) -> dict[str, Any]:
    """为侧边栏“最近”的单个无项目会话准备短时授权；必须先向用户展示并确认。"""
    try:
        if access_mode not in SessionAccessStore.VALID_MODES:
            raise BridgeError("access_mode 只能是 read-only 或 workspace-write。")
        descriptor, workspace = _session_workspace(session_id)
        if access_mode == "workspace-write":
            if not isinstance(request, str) or not request.strip():
                raise BridgeError("workspace-write 会话授权必须提供准确的 request。")
            if len(request) > CONFIG.max_request_chars:
                raise BridgeError(
                    f"request 过长，最多允许 {CONFIG.max_request_chars} 个字符。"
                )
        token = SESSION_ACCESS.issue(session_id, workspace, access_mode)
        result: dict[str, Any] = {
            "ok": True,
            "session_id": session_id,
            "title": descriptor.get("title"),
            "workspace_path": str(workspace),
            "access_mode": access_mode,
            "session_access_token": token,
            "approval_expires_in_seconds": CONFIG.confirmation_ttl_seconds,
            "grant_expires_in_seconds": CONFIG.session_access_ttl_seconds,
            "next_step": (
                "向用户展示会话标题、工作目录、访问模式和准确任务；取得明确确认后，"
                "把 session_access_token 原样传给 codex_read_session 或 "
                "codex_continue_desktop_session。"
            ),
        }
        if access_mode == "workspace-write":
            result["confirmation_token"] = CONFIRMATIONS.issue(workspace, request)
            result["next_step"] = (
                "向用户展示会话标题、工作目录和准确写入任务；取得明确确认后，"
                "调用 codex_continue_desktop_session，并同时原样传入 "
                "session_access_token、confirmation_token 和相同 request。"
            )
        return result
    except (BridgeError, ConversationError) as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_list_sessions(
    project_path: str | None = None,
    limit: int = 200,
    include_archived: bool = False,
    include_unassigned: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    """列出 Codex 侧边栏会话；include_unassigned 可显示待授权“最近”会话的元数据。"""
    if CONVERSATIONS is None:
        return {"ok": False, "error": "未配置 Codex 会话索引。"}
    try:
        return CONVERSATIONS.list_sessions(
            project_path=project_path,
            limit=limit,
            include_archived=include_archived,
            include_unassigned=include_unassigned,
            cursor=cursor,
        )
    except (BridgeError, ConversationError) as exc:
        return {"ok": False, "error": str(exc)}


@MCP.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def codex_read_session(
    session_id: str,
    max_messages: int = 100,
    max_message_chars: int = 12000,
    max_scan_bytes: int = 64 * 1024 * 1024,
    cursor: str | None = None,
    session_access_token: str | None = None,
) -> dict[str, Any]:
    """读取指定 Codex 会话的用户消息和可见 Codex 回复。"""
    if CONVERSATIONS is None:
        return {"ok": False, "error": "未配置 Codex 会话索引。"}
    try:
        descriptor = CONVERSATIONS.session_descriptor(session_id)
        if descriptor.get("access_scope") == "session":
            _require_session_access(
                session_id,
                "read-only",
                session_access_token,
            )
        return CONVERSATIONS.read_session(
            session_id,
            max_messages=max_messages,
            max_message_chars=max_message_chars,
            max_scan_bytes=max_scan_bytes,
            cursor=cursor,
        )
    except (BridgeError, ConversationError) as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    MCP.run()
