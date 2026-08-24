"""Codex Desktop 会话的受限只读目录与消息读取。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bridge_core import is_safe_workspace_root


MAX_STATE_BYTES = 64 * 1024 * 1024
SUMMARY_SCAN_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
DEFAULT_SCAN_BYTES = 64 * 1024 * 1024
MAX_SCAN_BYTES = 256 * 1024 * 1024
MAX_LIST_LIMIT = 200
MAX_MESSAGES = 100
MAX_MESSAGE_CHARS = 120000
MAX_PAGE_CHARS = 2_000_000
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[已脱敏 API Key]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I), "Bearer [已脱敏]"),
    (re.compile(r"(?i)\b(api[_ -]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"), r"\1=[已脱敏]"),
    (re.compile(r"(?i)\bcookie\s*[:=]\s*[^\n]+"), "Cookie: [已脱敏]"),
    (
        re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s]+"),
        "[已脱敏数据库连接串]",
    ),
    (
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        "[已脱敏邮箱]",
    ),
)


class ConversationError(Exception):
    """会话目录的可安全返回错误。"""


class ConversationCatalog:
    """只读读取 Codex 当前侧边栏可见的会话。"""

    def __init__(
        self,
        state_path: Path,
        project_provider: Callable[[], tuple[Any, ...]],
        session_access_checker: Callable[[str, Path, str], bool] | None = None,
    ) -> None:
        self.state_path = state_path
        self._project_provider = project_provider
        self._session_access_checker = session_access_checker

    def list_sessions(
        self,
        *,
        project_path: str | None = None,
        limit: int = MAX_LIST_LIMIT,
        include_archived: bool = False,
        include_unassigned: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= MAX_LIST_LIMIT:
            raise ConversationError(f"limit 必须是 1 到 {MAX_LIST_LIMIT} 的整数。")
        if not isinstance(include_unassigned, bool):
            raise ConversationError("include_unassigned 必须是布尔值。")
        offset = self._decode_cursor(cursor, "s")
        records = [
            record
            for record, _ in self._session_records(
                project_path=project_path,
                include_archived=include_archived,
                include_unassigned=include_unassigned,
            )
        ]
        page = records[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "ok": True,
            "count": len(page),
            "total_count": len(records),
            "sessions": page,
            "cursor": cursor,
            "next_cursor": (
                self._encode_cursor("s", next_offset)
                if next_offset < len(records)
                else None
            ),
            "has_more": next_offset < len(records),
            "next_step": (
                "继续调用 codex_list_sessions，并原样传入 next_cursor。"
                if next_offset < len(records)
                else "会话列表已读取完毕。"
            ),
            "include_archived": include_archived,
            "include_unassigned": include_unassigned,
            "visibility": "codex-sidebar-visible-only",
            "refresh_policy": "live-per-call",
        }

    def read_session(
        self,
        session_id: str,
        *,
        max_messages: int = 40,
        max_message_chars: int = 6000,
        max_scan_bytes: int = DEFAULT_SCAN_BYTES,
        include_archived: bool = True,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id.strip()):
            raise ConversationError("session_id 必须是 Codex 会话 UUID。")
        if not 1 <= max_messages <= MAX_MESSAGES:
            raise ConversationError(f"max_messages 必须是 1 到 {MAX_MESSAGES} 的整数。")
        if not 100 <= max_message_chars <= MAX_MESSAGE_CHARS:
            raise ConversationError(f"max_message_chars 必须是 100 到 {MAX_MESSAGE_CHARS} 的整数。")
        if max_messages * max_message_chars > MAX_PAGE_CHARS:
            raise ConversationError(
                f"单页消息预算不能超过 {MAX_PAGE_CHARS} 字符，请降低 max_messages 或 max_message_chars。"
            )
        if not 1024 <= max_scan_bytes <= MAX_SCAN_BYTES:
            raise ConversationError(f"max_scan_bytes 必须是 1024 到 {MAX_SCAN_BYTES} 的整数。")
        before_message_index = self._decode_cursor(cursor, "m")
        visible = self._visible_threads()
        context = visible.get(session_id.strip())
        if context is None:
            raise ConversationError("会话不存在、不可见或不属于当前 Codex 侧边栏。")
        path = self._find_file(session_id.strip(), include_archived)
        if path is None:
            raise ConversationError("会话文件不存在或已被 Codex 归档清理。")
        parsed = self._parse_file(
            path,
            collect_messages=True,
            max_messages=max_messages,
            max_message_chars=max_message_chars,
            max_scan_bytes=max_scan_bytes,
            before_message_index=before_message_index,
        )
        access = self._resolve_access(session_id.strip(), context, parsed)
        if access["access_state"] != "authorized":
            if access.get("access_scope") == "session":
                raise ConversationError(
                    "该会话位于侧边栏“最近”且没有项目授权；请先调用 "
                    "codex_prepare_session_access，并在用户确认后携带令牌重试。"
                )
            raise ConversationError("会话不属于当前已授权项目。")
        return {
            "ok": True,
            "session_id": session_id.strip(),
            "title": parsed.get("title") or "未命名对话",
            "project_name": access.get("project_name"),
            "project_path": access.get("project_root") or access.get("workspace_path"),
            "workspace_path": access.get("workspace_path"),
            "access_scope": access.get("access_scope"),
            "updated_at": parsed.get("updated_at"),
            "archived": "archived_sessions" in path.parts,
            "messages": parsed.get("messages", []),
            "message_count": len(parsed.get("messages", [])),
            "scanned_message_count": parsed.get("scanned_message_count", 0),
            "cursor": cursor,
            "next_cursor": parsed.get("next_cursor"),
            "has_more": parsed.get("has_more", False),
            "next_step": (
                "继续调用 codex_read_session，并原样传入 next_cursor 读取更早消息。"
                if parsed.get("has_more", False)
                else "该会话在当前扫描范围内已读取完毕。"
            ),
            "truncated": parsed.get("truncated", False),
            "read_policy": "仅返回用户消息和 Codex 可见回复，已过滤系统/开发者/推理/工具内容并脱敏。",
        }

    def session_descriptor(
        self,
        session_id: str,
        *,
        include_archived: bool = True,
    ) -> dict[str, Any]:
        """返回单会话授权所需的可信元数据，不返回消息正文。"""
        normalized = self._validate_session_id(session_id)
        visible = self._visible_threads()
        context = visible.get(normalized)
        if context is None:
            raise ConversationError("会话不存在、不可见或不属于当前 Codex 侧边栏。")
        path = self._find_file(normalized, include_archived)
        if path is None:
            raise ConversationError("会话文件不存在或已被 Codex 归档清理。")
        parsed = self._parse_file(
            path,
            collect_messages=False,
            max_scan_bytes=SUMMARY_SCAN_BYTES,
        )
        access = self._resolve_access(normalized, context, parsed)
        return {
            "session_id": normalized,
            "title": parsed.get("title") or "未命名对话",
            "updated_at": parsed.get("updated_at") or path.stat().st_mtime,
            "archived": "archived_sessions" in path.parts,
            **access,
        }

    def compatibility_snapshot(self, limit: int = MAX_LIST_LIMIT, preview_limit: int = 12) -> dict[str, Any]:
        """给旧版连接器提供内嵌会话摘要，不依赖新工具发现。"""
        listing = self.list_sessions(limit=limit, include_archived=False)
        previews: list[dict[str, Any]] = []
        for item in listing["sessions"][:preview_limit]:
            try:
                detail = self.read_session(
                    item["session_id"],
                    max_messages=6,
                    max_message_chars=1200,
                    max_scan_bytes=4 * 1024 * 1024,
                    include_archived=False,
                )
            except ConversationError:
                continue
            previews.append(
                {
                    "session_id": detail["session_id"],
                    "title": detail["title"],
                    "project_name": detail["project_name"],
                    "project_path": detail["project_path"],
                    "updated_at": detail["updated_at"],
                    "messages": detail["messages"],
                    "truncated": detail["truncated"],
                }
            )
        return {
            "sessions": listing["sessions"],
            "count": listing["count"],
            "previews": previews,
            "limit": limit,
            "preview_limit": preview_limit,
            "visibility": "codex-sidebar-visible-only",
            "refresh_policy": listing.get("refresh_policy", "live-per-call"),
            "next_step": "需要读取指定会话时，调用 codex_status(session_id=...)；旧版连接器也可调用 codex_analyze，request 使用“读取 Codex 会话：<session_id>”。",
        }

    def _session_records(
        self,
        *,
        project_path: str | None,
        include_archived: bool,
        include_unassigned: bool = False,
        session_files: list[Path] | None = None,
    ) -> list[tuple[dict[str, Any], Path]]:
        visible = self._visible_threads()
        allowed = self._allowed_projects()
        path_filter = self._validate_project_filter(project_path, allowed)
        latest_paths: dict[str, Path] = {}
        paths = (
            session_files
            if session_files is not None
            else self._session_files(include_archived)
        )
        for path in paths:
            session_id = self._session_id(path.name)
            if session_id is None:
                continue
            current = latest_paths.get(session_id)
            if current is None or path.stat().st_mtime > current.stat().st_mtime:
                latest_paths[session_id] = path

        records: list[tuple[dict[str, Any], Path]] = []
        for session_id, path in latest_paths.items():
            context = visible.get(session_id)
            if context is None:
                continue
            stat = path.stat()
            summary = self._parse_file(
                path,
                collect_messages=False,
                max_scan_bytes=SUMMARY_SCAN_BYTES,
            )
            try:
                access = self._resolve_access(session_id, context, summary, allowed)
            except ConversationError:
                # 单个历史会话的状态索引不一致时将其隔离，不能拖垮整个列表。
                continue
            if access["access_state"] != "authorized" and not include_unassigned:
                continue
            if access["access_state"] == "ineligible":
                continue
            if project_path and not self._path_matches(
                access.get("project_root") or access.get("workspace_path"),
                path_filter,
            ):
                continue
            records.append(
                (
                    {
                        "session_id": session_id,
                        "title": summary.get("title") or "未命名对话",
                        "project_name": access.get("project_name"),
                        "project_path": (
                            access.get("project_root") or access.get("workspace_path")
                            if access["access_state"] == "authorized"
                            else None
                        ),
                        "access_scope": access.get("access_scope"),
                        "access_state": access.get("access_state"),
                        "updated_at": summary.get("updated_at") or stat.st_mtime,
                        "archived": "archived_sessions" in path.parts,
                        "file_size_bytes": stat.st_size,
                    },
                    path,
                )
            )
        records.sort(
            key=lambda item: self._sort_time(item[0]["updated_at"]),
            reverse=True,
        )
        return records

    def _resolve_access(
        self,
        session_id: str,
        context: dict[str, str | None],
        parsed: dict[str, Any],
        allowed: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        allowed = self._allowed_projects() if allowed is None else allowed
        workspace = self._validated_workspace(context, parsed)
        project = self._session_project(
            {**context, "cwd": str(workspace) if workspace else None},
            allowed,
        )
        if project is not None:
            return {
                "project_id": project.get("project_id"),
                "project_name": project.get("name"),
                "project_root": project.get("path"),
                "workspace_path": str(workspace or Path(project["path"])),
                "access_scope": "project",
                "access_state": "authorized",
            }
        if context.get("project_id") is not None or workspace is None:
            return {
                "project_id": context.get("project_id"),
                "project_name": context.get("project_name"),
                "project_root": None,
                "workspace_path": str(workspace) if workspace else None,
                "access_scope": "none",
                "access_state": "ineligible",
            }
        if not is_safe_workspace_root(workspace):
            return {
                "project_id": None,
                "project_name": "最近",
                "project_root": None,
                "workspace_path": str(workspace),
                "access_scope": "session",
                "access_state": "ineligible",
            }
        authorized = bool(
            self._session_access_checker
            and self._session_access_checker(session_id, workspace, "read-only")
        )
        return {
            "project_id": None,
            "project_name": "最近",
            "project_root": None,
            "workspace_path": str(workspace),
            "access_scope": "session",
            "access_state": "authorized" if authorized else "authorization_required",
        }

    @staticmethod
    def _validated_workspace(
        context: dict[str, str | None],
        parsed: dict[str, Any],
    ) -> Path | None:
        raw_cwd = parsed.get("cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd.strip():
            return None
        try:
            workspace = Path(raw_cwd).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ConversationError("会话工作目录无法安全解析。") from exc
        indexed_cwd = context.get("cwd")
        if isinstance(indexed_cwd, str) and indexed_cwd.strip():
            try:
                indexed = Path(indexed_cwd).expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                raise ConversationError("Codex 状态索引中的工作目录无法安全解析。") from exc
            if indexed != workspace:
                raise ConversationError("会话工作目录与 Codex 状态索引不一致。")
        return workspace

    @staticmethod
    def _validate_session_id(session_id: str) -> str:
        if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id.strip()):
            raise ConversationError("session_id 必须是 Codex 会话 UUID。")
        return session_id.strip()

    def _allowed_projects(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for project in self._project_provider():
            for root in project.roots:
                resolved = root.expanduser().resolve()
                result[str(resolved)] = {"name": project.name, "path": str(resolved), "project_id": project.project_id}
        return result

    def _validate_project_filter(self, project_path: str | None, allowed: dict[str, dict[str, str]]) -> Path | None:
        if project_path is None:
            return None
        if not isinstance(project_path, str) or not project_path.strip():
            raise ConversationError("project_path 必须是非空目录路径。")
        candidate = Path(project_path).expanduser().resolve()
        for root in (Path(value["path"]) for value in allowed.values()):
            if candidate == root or root in candidate.parents:
                return candidate
        raise ConversationError("project_path 不在 Codex 已登记项目内。")

    @staticmethod
    def _path_matches(value: str | None, expected: Path | None) -> bool:
        return bool(value and expected and (Path(value) == expected or Path(value) in expected.parents or expected in Path(value).parents))

    def _session_project(
        self,
        context: dict[str, str | None],
        allowed: dict[str, dict[str, str]],
    ) -> dict[str, str] | None:
        project_id = context.get("project_id")
        cwd = context.get("cwd")
        for value in allowed.values():
            if value.get("project_id") == project_id:
                if not cwd:
                    return value
                candidate = Path(cwd).expanduser().resolve()
                root = Path(value["path"])
                if candidate == root or root in candidate.parents:
                    return value
        if cwd:
            candidate = Path(cwd).expanduser().resolve()
            for root, value in allowed.items():
                root_path = Path(root)
                if candidate == root_path or root_path in candidate.parents:
                    return value
        return None

    def _visible_threads(self) -> dict[str, dict[str, str | None]]:
        try:
            if self.state_path.stat().st_size > MAX_STATE_BYTES:
                raise ConversationError("Codex 当前状态文件超过安全读取限制。")
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except ConversationError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ConversationError(f"Codex 当前会话索引无法读取：{type(exc).__name__}") from exc
        if not isinstance(state, dict):
            raise ConversationError("Codex 当前会话索引格式不兼容。")
        local_projects = state.get("local-projects", {})
        names: dict[str, str] = {}
        if isinstance(local_projects, dict):
            for key, value in local_projects.items():
                if isinstance(value, dict):
                    names[str(value.get("id") or key)] = str(value.get("name") or key)
        result: dict[str, dict[str, str | None]] = {}
        orders = state.get("sidebar-project-thread-orders", {})
        assignments = state.get("thread-project-assignments", {})
        if isinstance(orders, dict):
            for project_id, order in orders.items():
                thread_ids = order.get("threadIds", []) if isinstance(order, dict) else []
                for thread_id in thread_ids if isinstance(thread_ids, list) else []:
                    if not isinstance(thread_id, str) or thread_id.startswith("client-new-thread:"):
                        continue
                    assigned = project_id
                    assignment = assignments.get(thread_id) if isinstance(assignments, dict) else None
                    if isinstance(assignment, dict):
                        assigned = assignment.get("projectId") or assignment.get("project_id") or assigned
                    result[thread_id] = {"project_id": str(assigned), "project_name": names.get(str(assigned)), "cwd": None}
        for thread_id in state.get("projectless-thread-ids", []) if isinstance(state.get("projectless-thread-ids", []), list) else []:
            if isinstance(thread_id, str) and not thread_id.startswith("client-new-thread:"):
                result[thread_id] = {"project_id": None, "project_name": "最近", "cwd": None}
        self._merge_state_db_threads(result)
        return result

    def _merge_state_db_threads(self, result: dict[str, dict[str, str | None]]) -> None:
        """合并新版 app-server 状态库中的可见线程，始终使用只读连接。"""
        state_db = self._state_db_path()
        if state_db is None:
            return
        try:
            connection = sqlite3.connect(f"{state_db.as_uri()}?mode=ro", uri=True, timeout=1)
            try:
                rows = connection.execute(
                    """
                    SELECT threads.id, threads.project_id, threads.cwd, projects.name
                    FROM threads
                    LEFT JOIN projects ON projects.id = threads.project_id
                    WHERE threads.archived = 0 AND threads.preview <> ''
                    """
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            LOGGER.warning("Codex 状态库只读扫描失败：%s", type(exc).__name__)
            return

        for thread_id, project_id, cwd, project_name in rows:
            if not isinstance(thread_id, str) or thread_id.startswith("client-new-thread:"):
                continue
            current = result.get(thread_id)
            if current is None:
                result[thread_id] = {
                    "project_id": project_id if isinstance(project_id, str) else None,
                    "project_name": project_name if isinstance(project_name, str) else None,
                    "cwd": cwd if isinstance(cwd, str) else None,
                }
                continue
            if current.get("project_id") is None and isinstance(project_id, str):
                current["project_id"] = project_id
            if current.get("project_name") is None and isinstance(project_name, str):
                current["project_name"] = project_name
            if current.get("cwd") is None and isinstance(cwd, str):
                current["cwd"] = cwd

    def _state_db_path(self) -> Path | None:
        candidates: list[tuple[int, Path]] = []
        for path in self.state_path.parent.glob("state_*.sqlite"):
            match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
            if match and path.is_file():
                candidates.append((int(match.group(1)), path))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    def _session_files(self, include_archived: bool) -> list[Path]:
        roots = [self.state_path.parent / "sessions"]
        if include_archived:
            roots.append(self.state_path.parent / "archived_sessions")
        files: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("rollout-*.jsonl"):
                if path.is_symlink() or not path.is_file() or self._session_id(path.name) is None:
                    continue
                files.append(path)
        return files

    def _find_file(self, session_id: str, include_archived: bool) -> Path | None:
        candidates = [path for path in self._session_files(include_archived) if self._session_id(path.name) == session_id]
        return max(candidates, key=lambda path: path.stat().st_mtime, default=None)

    @staticmethod
    def _session_id(name: str) -> str | None:
        if not name.startswith("rollout-") or not name.endswith(".jsonl"):
            return None
        match = UUID_RE.search(name[:-6])
        return match.group(1) if match else None

    def _parse_file(
        self,
        path: Path,
        *,
        collect_messages: bool,
        max_messages: int = 40,
        max_message_chars: int = 6000,
        max_scan_bytes: int = DEFAULT_SCAN_BYTES,
        before_message_index: int | None = None,
        message_consumer: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        messages: deque[tuple[int, dict[str, str]]] = deque(maxlen=max_messages)
        first_user = ""
        latest_timestamp: str | float | None = None
        cwd: str | None = None
        scanned = 0
        truncated = False
        oversized_record_count = 0
        visible_message_count = 0
        last_visible: tuple[str | None, str] | None = None
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    scanned += len(raw)
                    if scanned > max_scan_bytes:
                        truncated = True
                        break
                    if len(raw) > MAX_RECORD_BYTES:
                        oversized_record_count += 1
                        continue
                    try:
                        event = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    timestamp = event.get("timestamp") or event.get("updated_at") or event.get("created_at")
                    if isinstance(timestamp, (str, int, float)):
                        latest_timestamp = timestamp
                    record_type = event.get("type")
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    if record_type == "session_meta":
                        if isinstance(payload.get("cwd"), str):
                            cwd = payload["cwd"]
                        latest_timestamp = latest_timestamp or payload.get("timestamp")
                    role, text, phase = self._event_message(record_type, payload, event)
                    if not text:
                        continue
                    text = self._visible_text(text)
                    if not text:
                        continue
                    if role == "user" and not first_user:
                        first_user = text
                    if role not in {"user", "assistant"}:
                        continue
                    current_visible = (role, text)
                    if current_visible == last_visible:
                        continue
                    last_visible = current_visible
                    message_index = visible_message_count
                    visible_message_count += 1
                    if (collect_messages or message_consumer is not None) and (
                        before_message_index is None
                        or message_index < before_message_index
                    ):
                        safe_text = self._redact(text[:max_message_chars])
                        message = {
                            "role": role,
                            "text": safe_text,
                            "text_chars": len(text),
                            "text_truncated": len(text) > max_message_chars,
                            **({"phase": phase} if phase else {}),
                        }
                        if collect_messages:
                            messages.append((message_index, message))
                        if message_consumer is not None:
                            message_consumer(message)
        except OSError as exc:
            raise ConversationError(f"会话文件无法读取：{type(exc).__name__}") from exc
        if latest_timestamp is None:
            latest_timestamp = path.stat().st_mtime
        if before_message_index is not None and before_message_index > visible_message_count:
            raise ConversationError("消息分页游标已失效，请从最新一页重新读取。")
        page = list(messages)
        page_start = page[0][0] if page else 0
        result: dict[str, Any] = {
            "title": self._title(self._redact(first_user)),
            "updated_at": latest_timestamp,
            "cwd": cwd,
            "truncated": truncated,
            "scanned_bytes": min(scanned, max_scan_bytes),
            "oversized_record_count": oversized_record_count,
            "scanned_message_count": visible_message_count,
            "next_cursor": (
                self._encode_cursor("m", page_start)
                if collect_messages and page_start > 0
                else None
            ),
            "has_more": bool(collect_messages and page_start > 0),
        }
        if collect_messages:
            result["messages"] = [message for _, message in page]
        return result

    @staticmethod
    def _encode_cursor(kind: str, value: int) -> str:
        return f"{kind}:{value}"

    @staticmethod
    def _decode_cursor(cursor: str | None, kind: str) -> int | None:
        if cursor is None:
            return 0 if kind == "s" else None
        if not isinstance(cursor, str) or not re.fullmatch(rf"{re.escape(kind)}:\d+", cursor):
            raise ConversationError("分页游标无效，请使用上一次返回的 next_cursor。")
        return int(cursor.split(":", 1)[1])

    @staticmethod
    def _event_message(record_type: Any, payload: dict[str, Any], event: dict[str, Any]) -> tuple[str | None, str, str | None]:
        if record_type == "event_msg":
            event_type = payload.get("type")
            if event_type == "user_message":
                return "user", ConversationCatalog._text(payload.get("message")), None
            if event_type == "agent_message":
                return "assistant", ConversationCatalog._text(payload.get("message") or payload.get("text")), str(payload.get("phase") or "") or None
        if record_type == "response_item":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
            if item.get("type") == "message":
                role = item.get("role")
                return ("user" if role == "user" else "assistant" if role == "assistant" else None), ConversationCatalog._text(item.get("content") or item.get("text")), None
        return None, "", None

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "\n".join(ConversationCatalog._text(item) for item in value if ConversationCatalog._text(item)).strip()
        if isinstance(value, dict):
            return ConversationCatalog._text(value.get("text") or value.get("message") or value.get("content"))
        return ""

    @staticmethod
    def _visible_text(text: str) -> str:
        text = text.strip()
        if text.startswith("# Files mentioned by the user:"):
            return ConversationCatalog._attachment_request(text)
        if not text or any(marker in text for marker in ("<environment_context>", "<permissions instructions>", "<skills_instructions>", "# AGENTS.md instructions", "<app-context>")):
            return ""
        return text

    @staticmethod
    def _attachment_request(text: str) -> str:
        marker = "## My request:"
        return text.split(marker, 1)[1].strip() if marker in text else "包含附件的对话"

    @staticmethod
    def _redact(text: str) -> str:
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    @staticmethod
    def _title(text: str) -> str:
        line = text.splitlines()[0].strip() if text else ""
        return (line[:48] + "…") if len(line) > 48 else line or "未命名对话"

    @staticmethod
    def _sort_time(value: str | float) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0
LOGGER = logging.getLogger("codex-bridge.conversations")
