"""Local terminal path hints and on-demand remote verification.

Hints are only for menu presentation; operations must verify against the server.
The cache is owned by the UI thread. Verification does not mutate it.
"""

from __future__ import annotations

import posixpath
import re
import stat
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import PurePosixPath


PATH_TYPE_CACHE_TTL = 300.0
KNOWN_FILE_EXTENSIONS = frozenset("""
.jar .war .class .java .kt .yml .yaml .json .xml .properties .conf .config
.ini .env .log .out .txt .md .csv .sh .bash .zsh .bat .cmd .ps1 .py
.html .htm .js .ts .css .vue .sql .zip .rar .7z .tar .gz .tgz .bz2 .xz
.pdf .doc .docx .xls .xlsx .ppt .pptx .jpg .jpeg .png .gif .webp .bmp
.pem .key .crt .pid
""".split())
KNOWN_FILE_NAMES = frozenset({
    "dockerfile", "makefile", "jenkinsfile", "license", "readme", "gradlew", "mvnw",
})


class RemotePathType(Enum):
    FILE = "file"
    DIRECTORY = "directory"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RemotePathContext:
    raw_text: str
    normalized_path: str
    resolved_path: str | None
    guessed_type: RemotePathType
    verified_type: RemotePathType | None = None
    working_directory: str | None = None

    @property
    def is_absolute(self) -> bool:
        return self.normalized_path.startswith("/")

    @property
    def path_type(self) -> RemotePathType:
        return self.verified_type or self.guessed_type


@dataclass(frozen=True)
class RemotePathCacheEntry:
    path_type: RemotePathType
    timestamp: float


def normalize_selected_path(text: str) -> str:
    value = text.strip()
    # Preserve punctuation inside quotes: it may be part of the actual name.
    for _ in range(3):
        if len(value) >= 2 and (value[0], value[-1]) in {
            ("'", "'"), ('"', '"'), ("`", "`"), ("(", ")"), ("[", "]"),
        }:
            return value[1:-1]
        cleaned = value.rstrip(",;:")
        if cleaned == value:
            break
        value = cleaned
    return value


def guess_remote_path_type(path: str) -> RemotePathType:
    if not path:
        return RemotePathType.UNKNOWN
    if path.endswith("/") or path in {".", "..", "~"}:
        return RemotePathType.DIRECTORY
    name = PurePosixPath(path).name.lower()
    if name in KNOWN_FILE_NAMES or PurePosixPath(name).suffix in KNOWN_FILE_EXTENSIONS:
        return RemotePathType.FILE
    # Bare hidden directories are common; only known dotfiles get a file hint.
    if name in {".env", ".gitignore", ".bashrc", ".bash_profile", ".profile", ".zshrc"}:
        return RemotePathType.FILE
    return RemotePathType.UNKNOWN


def command_changes_directory(command: str) -> bool:
    return bool(re.search(
        r"(?:^|[;&|\n])\s*(?:(?:command|builtin|exec)\s+)?"
        r"(?:cd|pushd|popd|su|ssh)\b|(?:^|[;&|\n])\s*sudo\s+(?:-[^\s]*[is]|--login|--shell)\b",
        command,
    ))


class RemotePathResolver:
    def __init__(self) -> None:
        self._cache: dict[str, RemotePathCacheEntry] = {}
        self.generation = 0

    def guess(self, text: str, cwd: str | None = None) -> RemotePathContext | None:
        path = normalize_selected_path(text)
        if not path or len(path) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in path):
            return None
        # A command/URL is not a path. Quoted filenames can contain shell syntax.
        quoted = len(text.strip()) >= 2 and text.strip()[0] in "\"'`"
        if "://" in path or (not quoted and any(c in path for c in "|;&<>")):
            return None
        kind = guess_remote_path_type(path)
        if not quoted and not path.startswith(("/", "./", "../", "~", ".")) and " " in path and kind is RemotePathType.UNKNOWN:
            return None
        resolved = self.resolve_absolute_path(path, cwd)
        cached = self.get_cached_type(resolved) if resolved is not None else None
        return RemotePathContext(text, path, resolved, kind, cached, cwd)

    @staticmethod
    def resolve_absolute_path(path: str, cwd: str | None) -> str | None:
        if path.startswith("/"):
            return posixpath.normpath(path)
        if path.startswith("~") or cwd is None:
            return None
        return posixpath.normpath(posixpath.join(cwd, path))

    def get_cached_type(self, path: str) -> RemotePathType | None:
        entry = self._cache.get(path)
        if entry is not None:
            if time.monotonic() - entry.timestamp < PATH_TYPE_CACHE_TTL:
                return entry.path_type
            self._cache.pop(path, None)
        return None

    def remember(self, path: str, path_type: RemotePathType) -> None:
        self._cache.pop(path, None)
        self._cache[path] = RemotePathCacheEntry(path_type, time.monotonic())
        while len(self._cache) > 200:
            self._cache.pop(next(iter(self._cache)))

    def invalidate_directory(self, directory: str) -> None:
        directory = posixpath.normpath(directory)
        prefix = directory.rstrip("/") + "/"
        for path in list(self._cache):
            if path == directory or path.startswith(prefix):
                self._cache.pop(path, None)
        self.generation += 1

    def clear(self) -> None:
        self._cache.clear()
        self.generation += 1

    @staticmethod
    def verify(context: RemotePathContext, sftp: object, cwd: str | None = None) -> RemotePathContext:
        path = context.resolved_path
        if path is None:
            value = context.normalized_path
            if value == "~" or value.startswith("~/"):
                path = posixpath.join(sftp.normalize("."), value[2:])
            else:
                path = RemotePathResolver.resolve_absolute_path(value, cwd)
        if path is None:
            raise ValueError("暂未读到终端当前目录，请选中完整路径")
        path = posixpath.normpath(path)
        mode = sftp.stat(path).st_mode
        if stat.S_ISDIR(mode):
            kind = RemotePathType.DIRECTORY
        elif stat.S_ISREG(mode):
            kind = RemotePathType.FILE
        else:
            raise ValueError("该路径不是普通文件或文件夹")
        return replace(
            context, resolved_path=path, verified_type=kind,
            working_directory=cwd or context.working_directory,
        )
