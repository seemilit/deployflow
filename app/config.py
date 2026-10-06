"""Deployment configuration parsing and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from password_protection import PasswordProtectionError, is_protected, unprotect_text


class ConfigurationError(ValueError):
    """Raised when a deployment configuration is invalid."""


def mask_ip_address(address: str, enabled: bool = True) -> str:
    """Hide the middle two IPv4 octets for display without changing stored data."""
    if not enabled or not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", address):
        return address
    parts = address.split(".")
    if any(int(part) > 255 for part in parts):
        return address
    return f"{parts[0]}.**.**.{parts[3]}"


@dataclass(frozen=True)
class RestartCommandStep:
    command: str | None = None
    local_script_path: Path | None = None
    working_directory: str | None = None
    delay_after: float = 0


@dataclass(frozen=True)
class ServerParameters:
    name: str
    file_path: Path
    ip_address: str
    username: str
    port: int
    password: str | None = None
    key_filename: Path | None = None
    default_open_paths: tuple[str, ...] = ()
    default_open_commands: tuple[str | None, ...] = ()
    auth_method: str = "AUTO"
    default_open_path_index: int | None = None
    default_open_command_enabled: tuple[bool, ...] = ()

    @property
    def target(self) -> str:
        return f"{self.username}@{self.ip_address}:{self.port}"


@dataclass(frozen=True)
class DeploymentConfig:
    name: str
    file_path: Path
    ip_address: str
    username: str
    source_code_path: str
    restart_steps: tuple[RestartCommandStep, ...]
    port: int
    project_path: Path
    jar_file: str | None
    build_timeout: int = 900
    idea_project_path: Path | None = None
    target_branch: str | None = None
    health_check_command: str | None = None
    health_check_timeout: int = 120
    health_check_interval: float = 3.0
    health_check_success_count: int = 2
    health_check_expected_text: str | None = None
    health_check_instance_command: str | None = None
    rollback_command: str | None = None
    rollback_path: str | None = None
    password: str | None = None
    key_filename: Path | None = None
    auth_method: str = "AUTO"

    @property
    def target(self) -> str:
        return f"{self.username}@{self.ip_address}:{self.port}"

    @property
    def git_integration_enabled(self) -> bool:
        return self.idea_project_path is not None and self.target_branch is not None


_SERVER_REQUIRED_KEYS = (
    "IP_ADDRESS",
    "USERNAME",
    "PORT",
)

_SERVER_PARAMETER_KEYS = _SERVER_REQUIRED_KEYS + (
    "AUTH_METHOD",
    "SOURCE_CODE_PATH",
    "DEFAULT_OPEN_PATH",
    "DEFAULT_OPEN_COMMAND",
    "DEFAULT_OPEN_PATH_SELECTED",
    "DEFAULT_OPEN_COMMAND_ENABLED",
)

_SERVER_ONLY_KEYS = _SERVER_PARAMETER_KEYS + ("PASSWORD", "KEY_FILENAME")

_DEFAULT_OPEN_PATH_PATTERN = re.compile(r"DEFAULT_OPEN_PATH_(\d+)")
_DEFAULT_OPEN_COMMAND_PATTERN = re.compile(r"DEFAULT_OPEN_COMMAND_(\d+)")
_DEFAULT_OPEN_PATH_SELECTED_PATTERN = re.compile(r"DEFAULT_OPEN_PATH_SELECTED_(\d+)")
_DEFAULT_OPEN_COMMAND_ENABLED_PATTERN = re.compile(r"DEFAULT_OPEN_COMMAND_ENABLED_(\d+)")

def load_server_parameters(file_path: str | Path) -> ServerParameters:
    path = Path(file_path).resolve()
    _ensure_configuration_saved(path)
    values = _read_values(path)

    unexpected_keys = sorted(
        key
        for key in values
        if key not in _SERVER_ONLY_KEYS
        and _DEFAULT_OPEN_PATH_PATTERN.fullmatch(key) is None
        and _DEFAULT_OPEN_COMMAND_PATTERN.fullmatch(key) is None
        and _DEFAULT_OPEN_PATH_SELECTED_PATTERN.fullmatch(key) is None
        and _DEFAULT_OPEN_COMMAND_ENABLED_PATTERN.fullmatch(key) is None
    )
    if unexpected_keys:
        raise ConfigurationError(
            "服务器配置文件只能保存基础连接配置，请移除："
            + ", ".join(unexpected_keys)
        )

    missing = [key for key in _SERVER_REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise ConfigurationError(f"缺少服务器配置：{', '.join(missing)}")

    try:
        port = int(values["PORT"])
    except ValueError as exc:
        raise ConfigurationError("PORT 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("PORT 必须在 1 到 65535 之间")

    key_value = values.get("KEY_FILENAME", "").strip()
    key_filename = _local_path(key_value) if key_value else None
    auth_method = _authentication_method(values, key_filename)
    if auth_method == "KEY" and key_filename is None:
        raise ConfigurationError("使用 SSH 私钥登录时，请填写私钥文件路径")
    if auth_method == "KEY" and key_filename is not None and not key_filename.is_file():
        raise ConfigurationError(f"SSH 私钥不存在：{key_filename}")

    default_open_targets = _default_open_targets(values)
    return ServerParameters(
        name=path.stem,
        file_path=path,
        ip_address=values["IP_ADDRESS"],
        username=values["USERNAME"],
        port=port,
        password=_server_password(values.get("PASSWORD", "")),
        key_filename=key_filename,
        default_open_paths=tuple(
            path for path, _command, _selected, _enabled in default_open_targets
        ),
        default_open_commands=tuple(command for _path, command, _selected, _enabled in default_open_targets),
        auth_method=auth_method,
        default_open_path_index=next(
            (index for index, (_path, _command, selected, _enabled) in enumerate(default_open_targets) if selected),
            None,
        ),
        default_open_command_enabled=tuple(
            enabled for _path, _command, _selected, enabled in default_open_targets
        ),
    )


def _default_open_targets(
    values: dict[str, str]
) -> tuple[tuple[str, str | None, bool, bool], ...]:
    targets: list[tuple[str, str | None, bool, bool]] = []

    def flag(key: str, default: bool = False) -> bool:
        value = values.get(key, "").strip().upper()
        if not value:
            return default
        if value not in {"YES", "NO"}:
            raise ConfigurationError(f"{key} 只能填写 YES 或 NO")
        return value == "YES"

    legacy_path = values.get("DEFAULT_OPEN_PATH", "").strip()
    legacy_command = values.get("DEFAULT_OPEN_COMMAND", "").strip()
    if legacy_command and not legacy_path:
        raise ConfigurationError("默认命令缺少对应的默认目录：DEFAULT_OPEN_COMMAND")
    legacy_selected = flag("DEFAULT_OPEN_PATH_SELECTED")
    legacy_command_enabled = flag(
        "DEFAULT_OPEN_COMMAND_ENABLED", bool(legacy_command)
    )
    if (legacy_selected or legacy_command_enabled) and not legacy_path:
        raise ConfigurationError("默认选项缺少对应的默认目录：DEFAULT_OPEN_PATH")
    if legacy_path:
        targets.append((
            legacy_path,
            legacy_command or None,
            legacy_selected,
            legacy_command_enabled,
        ))
    indexed_paths: dict[int, str] = {}
    indexed_commands: dict[int, str] = {}
    option_indexes: set[int] = set()
    for key, value in values.items():
        path_match = _DEFAULT_OPEN_PATH_PATTERN.fullmatch(key)
        command_match = _DEFAULT_OPEN_COMMAND_PATTERN.fullmatch(key)
        selected_match = _DEFAULT_OPEN_PATH_SELECTED_PATTERN.fullmatch(key)
        enabled_match = _DEFAULT_OPEN_COMMAND_ENABLED_PATTERN.fullmatch(key)
        match = path_match or command_match or selected_match or enabled_match
        if match is None:
            continue
        index_text = match.group(1)
        if int(index_text) <= 0 or index_text != str(int(index_text)):
            raise ConfigurationError(f"默认目录或命令编号无效：{key}")
        index = int(index_text)
        if path_match is not None and value.strip():
            indexed_paths[index] = value.strip()
        elif command_match is not None and value.strip():
            indexed_commands[index] = value.strip()
        elif selected_match is not None or enabled_match is not None:
            option_indexes.add(index)
    path_indexes = set(indexed_paths)
    orphan_commands = sorted(index for index in indexed_commands if index not in path_indexes)
    if orphan_commands:
        keys = ", ".join(f"DEFAULT_OPEN_COMMAND_{index}" for index in orphan_commands)
        raise ConfigurationError(f"默认命令缺少对应的默认目录：{keys}")
    for index in sorted(set(indexed_paths) | option_indexes):
        selected = flag(f"DEFAULT_OPEN_PATH_SELECTED_{index}")
        enabled = flag(
            f"DEFAULT_OPEN_COMMAND_ENABLED_{index}",
            bool(indexed_commands.get(index)),
        )
        path = indexed_paths.get(index, "")
        if (selected or enabled) and not path:
            raise ConfigurationError(
                f"默认选项缺少对应的默认目录：DEFAULT_OPEN_PATH_{index}"
            )
        if path and all(existing_path != path for existing_path, *_rest in targets):
            targets.append((path, indexed_commands.get(index), selected, enabled))
    if sum(1 for _path, _command, selected, _enabled in targets if selected) > 1:
        raise ConfigurationError("默认访问位置只能选择一个")
    return tuple(targets)


def _authentication_method(
    values: dict[str, str], key_filename: Path | None
) -> str:
    method = values.get("AUTH_METHOD", "").strip().upper()
    if not method:
        return "KEY" if key_filename is not None else "PASSWORD"
    if method not in {"PASSWORD", "KEY"}:
        raise ConfigurationError("AUTH_METHOD 只能填写 PASSWORD 或 KEY")
    return method


def _ensure_configuration_saved(path: Path) -> None:
    draft_path = (
        path.parent.parent
        / ".drafts"
        / "parameter"
        / f"{path.name}.draft"
    )
    if draft_path.is_file():
        raise ConfigurationError(
            f"服务器配置 {path.name} 存在未保存的暂存内容；"
            "请先在配置页面点击保存"
        )


def _read_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigurationError(f"配置文件不存在：{path}")

    values: dict[str, str] = {}
    key_lines: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ConfigurationError("配置文件必须使用 UTF-8 编码") from exc
    except OSError as exc:
        raise ConfigurationError(f"无法读取配置文件：{exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"第 {line_number} 行格式错误，应为 KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip().upper()
        if not key:
            raise ConfigurationError(f"第 {line_number} 行的配置名为空")
        if key in values:
            raise ConfigurationError(
                f"第 {line_number} 行的配置项 {key} 与第 {key_lines[key]} 行重复；"
                "配置名不区分大小写"
            )
        values[key] = value.strip()
        key_lines[key] = line_number

    return values


def _local_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded).resolve()


def _server_password(value: str) -> str | None:
    password = value.strip()
    if not password:
        return None
    if not is_protected(password):
        return password
    try:
        return unprotect_text(password) or None
    except PasswordProtectionError as exc:
        raise ConfigurationError(f"无法解密服务器密码：{exc}") from exc
