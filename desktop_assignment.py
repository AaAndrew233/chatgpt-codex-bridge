"""通过受限 Unix socket 通知 Codex Desktop 更新会话项目归属。"""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout_seconds: float) -> None:
        super().__init__("localhost", timeout=timeout_seconds)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self._socket_path))
        self.sock = connection


class DesktopAssignmentClient:
    """向已打补丁的 Codex Desktop 主进程发送最小项目归属更新。"""

    def __init__(
        self,
        socket_path: Path | None = None,
        token_path: Path | None = None,
        timeout_seconds: float = 1.0,
        connection_factory: Callable[[Path, float], http.client.HTTPConnection]
        | None = None,
    ) -> None:
        state_dir = Path.home() / ".codex" / "codex-bridge"
        self._socket_path = socket_path or state_dir / "assignment.sock"
        self._token_path = token_path or state_dir / "assignment.token"
        self._timeout_seconds = timeout_seconds
        self._connection_factory = connection_factory or _UnixHTTPConnection

    def assign(self, thread_id: str, project_id: str) -> dict[str, Any]:
        if not thread_id.strip() or not project_id.strip():
            return {"state": "invalid-request", "notified": False}
        if not self._socket_path.exists():
            return {"state": "desktop-unavailable", "notified": False}

        try:
            token = self._read_token()
        except (OSError, ValueError):
            return {"state": "token-unavailable", "notified": False}

        body = json.dumps(
            {
                "threadId": thread_id,
                "assignment": {"projectKind": "local", "projectId": project_id},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = self._connection_factory(self._socket_path, self._timeout_seconds)
        try:
            connection.request(
                "POST",
                "/set-assignment",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "x-codex-bridge-token": token,
                },
            )
            response = connection.getresponse()
            response.read(4096)
            if response.status == 204:
                return {"state": "notified", "notified": True}
            return {
                "state": "desktop-rejected",
                "notified": False,
                "status_code": response.status,
            }
        except (OSError, TimeoutError, http.client.HTTPException):
            return {"state": "desktop-unavailable", "notified": False}
        finally:
            connection.close()

    def _read_token(self) -> str:
        token_stat = self._token_path.stat()
        if not stat.S_ISREG(token_stat.st_mode):
            raise ValueError("token 文件类型无效")
        if hasattr(os, "getuid") and token_stat.st_uid != os.getuid():
            raise ValueError("token 文件所有者无效")
        if token_stat.st_mode & 0o077:
            raise ValueError("token 文件权限过宽")
        token = self._token_path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("token 长度无效")
        return token
