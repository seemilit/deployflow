"""Disposable per-tab directory metadata, persisted without UI-thread file I/O."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

try:
    import msvcrt
except ImportError:
    msvcrt = None


_INSTANCE_ID = uuid.uuid4().hex
_RUN_NAME = re.compile(r"[0-9]+-[0-9a-f]{32}")
_CACHE_NAME = re.compile(r"[0-9a-f]{32}\.jsonl")
_RUN_LOCK = threading.Lock()


@dataclass
class _RunOwner:
    directory: Path
    handle: BinaryIO
    references: int = 0


_RUN_OWNERS: dict[Path, _RunOwner] = {}


def _lock_file(handle: BinaryIO, *, blocking: bool) -> None:
    handle.seek(0)
    if msvcrt is not None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if msvcrt is not None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _cleanup_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".cleanup.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        _lock_file(handle, blocking=True)
        try:
            yield
        finally:
            _unlock_file(handle)


def _remove_cache_directory(directory: Path) -> None:
    paths = list(directory.iterdir())
    if any(
        path.is_symlink() or not path.is_file()
        or (path.name != ".owner.lock" and not _CACHE_NAME.fullmatch(path.name))
        for path in paths
    ):
        return
    for path in paths:
        if _CACHE_NAME.fullmatch(path.name):
            path.unlink(missing_ok=True)
    (directory / ".owner.lock").unlink(missing_ok=True)
    directory.rmdir()


def _cleanup_stale_directories(root: Path) -> None:
    resolved_root = root.resolve()
    for directory in root.iterdir():
        if not _RUN_NAME.fullmatch(directory.name) or directory.is_symlink():
            continue
        if not directory.is_dir() or directory.resolve().parent != resolved_root:
            continue
        if (directory / ".owner.lock").is_symlink():
            continue
        try:
            # The OS releases this lock on a crash; live instances keep it held.
            with (directory / ".owner.lock").open("r+b") as handle:
                try:
                    _lock_file(handle, blocking=False)
                except OSError:
                    continue
                _unlock_file(handle)
            _remove_cache_directory(directory)
        except OSError:
            continue


def start_terminal_cache_cleanup(data_directory: Path) -> None:
    def cleanup() -> None:
        root = data_directory / ".cache" / "terminal"
        try:
            with _cleanup_lock(root):
                _cleanup_stale_directories(root)
        except OSError:
            pass

    threading.Thread(target=cleanup, name="terminal-cache-cleanup", daemon=True).start()


def _acquire_run(root: Path) -> _RunOwner:
    with _RUN_LOCK:
        owner = _RUN_OWNERS.get(root)
        if owner is None:
            with _cleanup_lock(root):
                _cleanup_stale_directories(root)
                directory = root / f"{os.getpid()}-{_INSTANCE_ID}"
                directory.mkdir(exist_ok=True)
                handle = (directory / ".owner.lock").open("w+b")
                try:
                    handle.write(b"\0")
                    handle.flush()
                    _lock_file(handle, blocking=False)
                except BaseException:
                    handle.close()
                    raise
                owner = _RunOwner(directory, handle)
                _RUN_OWNERS[root] = owner
        owner.references += 1
        return owner


def _release_run(root: Path, owner: _RunOwner) -> None:
    with _RUN_LOCK:
        owner.references -= 1
        if owner.references:
            return
        try:
            with _cleanup_lock(root):
                _unlock_file(owner.handle)
                owner.handle.close()
                _remove_cache_directory(owner.directory)
        finally:
            owner.handle.close()
            _RUN_OWNERS.pop(root, None)


class TerminalDirectoryCache:
    def __init__(self, data_directory: Path) -> None:
        self.tab_id = uuid.uuid4().hex
        self._root = data_directory / ".cache" / "terminal"
        self._records: queue.SimpleQueue[tuple[int, str] | None] = queue.SimpleQueue()
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"terminal-directory-cache-{self.tab_id[:8]}", daemon=True,
        )
        self._thread.start()

    def record(self, context_id: int, directory: str) -> None:
        if not self._closed.is_set():
            self._records.put((context_id, directory))

    def clear(self) -> None:
        if not self._closed.is_set():
            self._records.put((-1, ""))

    def close(self) -> None:
        self._closed.set()
        self._records.put(None)

    def _run(self) -> None:
        owner = None
        path = None
        try:
            owner = _acquire_run(self._root)
            path = owner.directory / f"{self.tab_id}.jsonl"
            with path.open("x", encoding="utf-8") as handle:
                while not self._closed.is_set():
                    try:
                        record = self._records.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    lines: list[str] = []
                    for index in range(256):
                        if record is None or self._closed.is_set():
                            break
                        context_id, directory = record
                        if context_id < 0:
                            lines.clear()
                            handle.seek(0)
                            handle.truncate()
                        else:
                            lines.append(json.dumps({
                                "tab_id": self.tab_id,
                                "context_id": context_id,
                                "directory": directory,
                            }, ensure_ascii=False))
                        if index == 255:
                            break
                        try:
                            record = self._records.get_nowait()
                        except queue.Empty:
                            break
                    if lines and not self._closed.is_set():
                        handle.write("\n".join(lines) + "\n")
                        handle.flush()
        except OSError:
            # Disk cache failure never interrupts terminal rendering or input.
            pass
        finally:
            self._closed.set()
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if owner is not None:
                try:
                    _release_run(self._root, owner)
                except OSError:
                    pass
