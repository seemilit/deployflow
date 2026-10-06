"""Shared timestamp formatting for persisted application logs."""

from __future__ import annotations

from datetime import datetime


def timestamp_log_text(
    value: str, recorded_at: datetime | None = None, *, line_start: bool = True,
) -> tuple[str, bool]:
    """Prefix nonempty lines, keeping streamed fragments on the same line."""
    timestamp = (recorded_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for line in value.splitlines(keepends=True):
        if line_start and line != "\n":
            parts.append(f"[{timestamp}] ")
        parts.append(line)
        line_start = line.endswith("\n")
    return "".join(parts), line_start
