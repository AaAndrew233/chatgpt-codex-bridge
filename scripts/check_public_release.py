#!/usr/bin/env python3
"""Fail when a release tree contains local state, personal paths, or likely credentials."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
IGNORED_DIRS = {".git", ".venv", "__pycache__", ".gstack"}
FORBIDDEN_NAMES = {"config.json", ".mcp.json", "assignment.sock", "assignment.token"}
FORBIDDEN_SUFFIXES = {".log", ".pid", ".sock", ".token", ".zip"}
TEXT_SUFFIXES = {
    "",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PATTERNS = {
    "OpenAI API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{70,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "personal absolute path": re.compile(
        r"/" + r"Users/" + r"(?!YOUR_USERNAME(?:/|\b))[^/\s]+/"
    ),
}


def iter_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in IGNORED_DIRS for part in path.parts)
    )


def main() -> int:
    failures: list[str] = []
    files = iter_files()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append(f"禁止公开的文件：{relative}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"无法按 UTF-8 审查的文件：{relative}")
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{relative} 包含疑似 {label}")

    if failures:
        print("公开发布检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"公开发布检查通过：已扫描 {len(files)} 个文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
