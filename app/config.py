"""Deployment configuration parsing and validation."""

from __future__ import annotations

import math
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from password_protection import PasswordProtectionError, is_protected, unprotect_text


class ConfigurationError(ValueError):
    """Raised when a deployment configuration is invalid."""


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
    "SOURCE_CODE_PATH",
    "DEFAULT_OPEN_PATH",
)

_TASK_KEYS = (
    "PROJECT_PATH",
)

_SERVER_ONLY_KEYS = _SERVER_PARAMETER_KEYS + ("PASSWORD", "KEY_FILENAME")

_DEFAULT_OPEN_PATH_PATTERN = re.compile(r"DEFAULT_OPEN_PATH_(\d+)")

_REQUIRED_KEYS = _SERVER_REQUIRED_KEYS + _TASK_KEYS

_TASK_STATIC_KEYS = frozenset(
    {
        "PARAMETER_FILE",
        "PROJECT_PATH",
        "IDEA_PROJECT_PATH",
        "TARGET_BRANCH",
        "BRANCH",
        "JAR_FILE",
        "BUILD_TIMEOUT",
        "RUN_SCRIPT_PATH",
        "SCRIPT_FILE",
        "SCRIPT_PATH",
        "RESTART_COMMAND",
        "RESTART_PATH",
        "RESTART_SCRIPT_PATH",
        "HEALTH_CHECK_COMMAND",
        "HEALTH_CHECK_TIMEOUT",
        "HEALTH_CHECK_INTERVAL",
        "HEALTH_CHECK_SUCCESS_COUNT",
        "HEALTH_CHECK_EXPECTED_TEXT",
        "HEALTH_CHECK_INSTANCE_COMMAND",
        "ROLLBACK_COMMAND",
        "ROLLBACK_PATH",
    }
)

_TASK_DYNAMIC_KEY_PATTERN = re.compile(
    r"RESTART_(?:COMMAND|LOCAL_SCRIPT|PATH|DELAY)_(\d+)"
)


def load_server_parameters(file_path: str | Path) -> ServerParameters:
    path = Path(file_path).resolve()
    _ensure_configuration_saved(path)
    values = _read_values(path)

    unexpected_keys = sorted(
        key
        for key in values
        if key not in _SERVER_ONLY_KEYS
        and _DEFAULT_OPEN_PATH_PATTERN.fullmatch(key) is None
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
    if key_filename is not None and not key_filename.is_file():
        raise ConfigurationError(f"SSH 私钥不存在：{key_filename}")

    return ServerParameters(
        name=path.stem,
        file_path=path,
        ip_address=values["IP_ADDRESS"],
        username=values["USERNAME"],
        port=port,
        password=_server_password(values.get("PASSWORD", "")),
        key_filename=key_filename,
        default_open_paths=_default_open_paths(values),
    )


def _default_open_paths(values: dict[str, str]) -> tuple[str, ...]:
    paths: list[str] = []
    legacy_path = values.get("DEFAULT_OPEN_PATH", "").strip()
    if legacy_path:
        paths.append(legacy_path)
    indexed_paths: list[tuple[int, str]] = []
    for key, value in values.items():
        match = _DEFAULT_OPEN_PATH_PATTERN.fullmatch(key)
        path = value.strip()
        if match is None:
            continue
        index_text = match.group(1)
        if int(index_text) <= 0 or index_text != str(int(index_text)):
            raise ConfigurationError(f"默认目录编号无效：{key}")
        if path:
            indexed_paths.append((int(index_text), path))
    for _index, path in sorted(indexed_paths):
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def load_config(file_path: str | Path) -> DeploymentConfig:
    path = Path(file_path).resolve()
    task_values = _read_values(path)
    _validate_task_keys(task_values)
    values = _compose_values(path, task_values)

    missing = [key for key in _REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise ConfigurationError(f"缺少任务或服务器配置：{', '.join(missing)}")

    restart_steps = _restart_steps(values, path.parent)

    try:
        port = int(values["PORT"])
    except ValueError as exc:
        raise ConfigurationError("PORT 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("PORT 必须在 1 到 65535 之间")

    project_path = _local_path(values["PROJECT_PATH"])
    if not project_path.is_dir():
        raise ConfigurationError(f"本地项目目录不存在：{project_path}")

    try:
        build_timeout = int(values.get("BUILD_TIMEOUT", "900"))
    except ValueError as exc:
        raise ConfigurationError("BUILD_TIMEOUT 必须是整数") from exc
    if build_timeout <= 0:
        raise ConfigurationError("BUILD_TIMEOUT 必须大于 0")

    target_branch_value = values.get("TARGET_BRANCH", "").strip()
    legacy_branch_value = values.get("BRANCH", "").strip()
    if (
        target_branch_value
        and legacy_branch_value
        and target_branch_value != legacy_branch_value
    ):
        raise ConfigurationError(
            "TARGET_BRANCH 和旧参数 BRANCH 不能配置为不同的分支"
        )
    target_branch = target_branch_value or legacy_branch_value or None
    idea_project_value = values.get("IDEA_PROJECT_PATH", "").strip()
    if bool(target_branch) != bool(idea_project_value):
        raise ConfigurationError(
            "TARGET_BRANCH（或 BRANCH）和 IDEA_PROJECT_PATH 必须同时配置"
        )

    idea_project_path = (
        _local_path(idea_project_value) if idea_project_value else None
    )
    if idea_project_path is not None and not idea_project_path.is_dir():
        raise ConfigurationError(f"IDEA 项目目录不存在：{idea_project_path}")
    if idea_project_path == project_path:
        raise ConfigurationError("IDEA_PROJECT_PATH 不能和 PROJECT_PATH 使用同一目录")

    key_filename_value = values.get("KEY_FILENAME", "").strip()
    key_filename = _local_path(key_filename_value) if key_filename_value else None
    if key_filename is not None and not key_filename.is_file():
        raise ConfigurationError(f"SSH 私钥不存在：{key_filename}")

    password = _server_password(values.get("PASSWORD", ""))
    if password is None and key_filename is None:
        # Fabric can still use the SSH agent or the user's default key files.
        password = None

    health_check_command = values.get("HEALTH_CHECK_COMMAND", "").strip() or None
    try:
        health_check_timeout = int(values.get("HEALTH_CHECK_TIMEOUT", "120"))
    except ValueError as exc:
        raise ConfigurationError("HEALTH_CHECK_TIMEOUT 必须是整数") from exc
    health_check_interval = _finite_float(
        values.get("HEALTH_CHECK_INTERVAL", "3"),
        "HEALTH_CHECK_INTERVAL",
    )
    if health_check_timeout <= 0:
        raise ConfigurationError("HEALTH_CHECK_TIMEOUT 必须大于 0")
    if health_check_interval <= 0:
        raise ConfigurationError("HEALTH_CHECK_INTERVAL 必须大于 0")

    try:
        health_check_success_count = int(
            values.get("HEALTH_CHECK_SUCCESS_COUNT", "2")
        )
    except ValueError as exc:
        raise ConfigurationError("HEALTH_CHECK_SUCCESS_COUNT 必须是整数") from exc
    if health_check_success_count < 2:
        raise ConfigurationError("HEALTH_CHECK_SUCCESS_COUNT 不能小于 2")

    health_check_expected_text = (
        values.get("HEALTH_CHECK_EXPECTED_TEXT", "").strip() or None
    )
    if health_check_expected_text and health_check_command is None:
        raise ConfigurationError(
            "配置 HEALTH_CHECK_EXPECTED_TEXT 时必须同时配置 HEALTH_CHECK_COMMAND"
        )

    health_check_instance_command = (
        values.get("HEALTH_CHECK_INSTANCE_COMMAND", "").strip() or None
    )
    if health_check_instance_command and health_check_command is None:
        raise ConfigurationError(
            "配置 HEALTH_CHECK_INSTANCE_COMMAND 时必须同时配置 HEALTH_CHECK_COMMAND"
        )

    rollback_command = values.get("ROLLBACK_COMMAND", "").strip() or None
    rollback_path = values.get("ROLLBACK_PATH", "").strip() or None
    if rollback_path and rollback_command is None:
        raise ConfigurationError(
            "配置 ROLLBACK_PATH 时必须同时配置 ROLLBACK_COMMAND"
        )
    if rollback_command is not None:
        rollback_command = _validate_restart_command(
            rollback_command,
            "ROLLBACK_COMMAND",
        )

    return DeploymentConfig(
        name=path.stem,
        file_path=path,
        ip_address=values["IP_ADDRESS"],
        username=values["USERNAME"],
        password=password,
        key_filename=key_filename,
        source_code_path=values.get("SOURCE_CODE_PATH", "").strip() or ".",
        restart_steps=restart_steps,
        port=port,
        project_path=project_path,
        jar_file=values.get("JAR_FILE", "").strip() or None,
        build_timeout=build_timeout,
        idea_project_path=idea_project_path,
        target_branch=target_branch,
        health_check_command=health_check_command,
        health_check_timeout=health_check_timeout,
        health_check_interval=health_check_interval,
        health_check_success_count=health_check_success_count,
        health_check_expected_text=health_check_expected_text,
        health_check_instance_command=health_check_instance_command,
        rollback_command=rollback_command,
        rollback_path=rollback_path,
    )


def _validate_task_keys(task_values: dict[str, str]) -> None:
    misplaced_server_keys = sorted(
        key
        for key in task_values
        if key in _SERVER_ONLY_KEYS
        or _DEFAULT_OPEN_PATH_PATTERN.fullmatch(key) is not None
    )
    if misplaced_server_keys:
        raise ConfigurationError(
            "服务器基础配置只能写在 conf/parameters 配置文件中："
            + ", ".join(misplaced_server_keys)
        )

    unsupported_keys: list[str] = []
    invalid_indexes: list[str] = []
    for key in task_values:
        if key in _TASK_STATIC_KEYS:
            continue
        match = _TASK_DYNAMIC_KEY_PATTERN.fullmatch(key)
        if match:
            index_text = match.group(1)
            if int(index_text) <= 0 or index_text != str(int(index_text)):
                invalid_indexes.append(key)
            continue
        unsupported_keys.append(key)

    if invalid_indexes:
        raise ConfigurationError(
            "执行步骤编号必须从 1 开始：" + ", ".join(sorted(invalid_indexes))
        )
    if unsupported_keys:
        raise ConfigurationError(
            "任务中存在不支持或可能拼写错误的参数："
            + ", ".join(sorted(unsupported_keys))
        )


def _compose_values(task_path: Path, task_values: dict[str, str]) -> dict[str, str]:
    """Combine a task with its required server parameter file."""
    parameter_reference = task_values.get("PARAMETER_FILE", "").strip()
    if not parameter_reference:
        raise ConfigurationError("缺少任务配置：PARAMETER_FILE")

    misplaced_task_keys = sorted(
        key
        for key in task_values
        if key in _SERVER_ONLY_KEYS
        or _DEFAULT_OPEN_PATH_PATTERN.fullmatch(key) is not None
    )
    if misplaced_task_keys:
        raise ConfigurationError(
            "以下服务器配置只能写在配置文件中："
            + ", ".join(misplaced_task_keys)
        )

    parameter_path = _resolve_parameter_path(parameter_reference, task_path)
    parameter_values = _read_values(parameter_path)
    misplaced_parameter_keys = sorted(
        key
        for key in parameter_values
        if key not in _SERVER_ONLY_KEYS
        and _DEFAULT_OPEN_PATH_PATTERN.fullmatch(key) is None
    )
    if misplaced_parameter_keys:
        raise ConfigurationError(
            f"配置文件 {parameter_path.name} 只能保存服务器基础配置，"
            f"请移除：{', '.join(misplaced_parameter_keys)}"
        )
    missing = [key for key in _SERVER_REQUIRED_KEYS if not parameter_values.get(key)]
    if missing:
        raise ConfigurationError(
            f"配置文件 {parameter_path.name} 缺少配置：{', '.join(missing)}"
        )

    values = dict(task_values)
    for key in _SERVER_PARAMETER_KEYS:
        values[key] = parameter_values.get(key, "")
    for key in ("PASSWORD", "KEY_FILENAME"):
        values[key] = parameter_values.get(key, "")
    return values


def _resolve_parameter_path(reference: str, task_path: Path) -> Path:
    parameter_root = task_path.parent.parent / "parameters"
    parameter_path = _resolve_conf_reference(
        reference,
        parameter_root,
        "PARAMETER_FILE",
    )
    _ensure_configuration_saved(parameter_path)
    return parameter_path


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


def _restart_steps(
    values: dict[str, str],
    config_directory: Path,
) -> tuple[RestartCommandStep, ...]:
    run_script_path = values.get("RUN_SCRIPT_PATH", "").strip()
    script_reference = values.get("SCRIPT_FILE", "").strip()

    explicit_indexed_keys = sorted(
        key
        for key, value in values.items()
        if value.strip()
        and re.fullmatch(
            r"RESTART_(?:COMMAND|LOCAL_SCRIPT|DELAY|PATH)_\d+",
            key,
        )
    )
    if run_script_path:
        run_conflicts: list[str] = []
        if script_reference:
            run_conflicts.append("SCRIPT_FILE")
        if values.get("RESTART_COMMAND", "").strip():
            run_conflicts.append("RESTART_COMMAND")
        run_conflicts.extend(explicit_indexed_keys)
        if run_conflicts:
            raise ConfigurationError(
                "遗留参数 RUN_SCRIPT_PATH 不能和新式执行步骤混用，请先将其迁移为 "
                "RESTART_COMMAND_N："
                + ", ".join(run_conflicts)
            )

    if explicit_indexed_keys:
        indexed_legacy_conflicts = sorted(
            key
            for key in (
                "RESTART_COMMAND",
                "RESTART_PATH",
                "RESTART_SCRIPT_PATH",
            )
            if values.get(key, "").strip()
        )
        if indexed_legacy_conflicts:
            raise ConfigurationError(
                "编号执行步骤不能和旧式重启参数混用："
                + ", ".join(indexed_legacy_conflicts)
            )

    if script_reference:
        conflicting_keys = sorted(
            key
            for key, value in values.items()
            if value.strip()
            and (
                key == "RESTART_COMMAND"
                or key == "RESTART_PATH"
                or key == "RESTART_SCRIPT_PATH"
                or re.fullmatch(
                    r"RESTART_(?:COMMAND|LOCAL_SCRIPT|DELAY|PATH)_\d+",
                    key,
                )
            )
        )
        if conflicting_keys:
            raise ConfigurationError(
                "SCRIPT_FILE 不能和以下重启配置同时使用："
                + ", ".join(conflicting_keys)
            )
        script_path = _resolve_script_path(script_reference, config_directory)
        if not script_path.is_file():
            raise ConfigurationError(f"任务引用的脚本不存在：{script_path}")
        if script_path.suffix.lower() not in {".sh", ".bash"}:
            raise ConfigurationError("SCRIPT_FILE 只能引用 .sh 或 .bash 脚本")
        return (
            RestartCommandStep(
                local_script_path=script_path,
                working_directory=values.get("SCRIPT_PATH", "").strip() or None,
            ),
        )

    if values.get("SCRIPT_PATH", "").strip():
        raise ConfigurationError("配置 SCRIPT_PATH 时必须同时配置 SCRIPT_FILE")

    indexed_commands: dict[int, str] = {}
    indexed_scripts: dict[int, Path] = {}
    for key, value in values.items():
        command_match = re.fullmatch(r"RESTART_COMMAND_(\d+)", key)
        script_match = re.fullmatch(r"RESTART_LOCAL_SCRIPT_(\d+)", key)
        if command_match and value.strip():
            index = int(command_match.group(1))
            if index <= 0:
                raise ConfigurationError("RESTART_COMMAND 编号必须从 1 开始")
            indexed_commands[index] = _validate_restart_command(value.strip(), key)
        elif script_match and value.strip():
            index = int(script_match.group(1))
            if index <= 0:
                raise ConfigurationError("RESTART_LOCAL_SCRIPT 编号必须从 1 开始")
            script_path = _resolve_script_path(value.strip(), config_directory)
            if not script_path.is_file():
                raise ConfigurationError(f"本地脚本不存在：{script_path}")
            if script_path.suffix.lower() not in {".sh", ".bash"}:
                raise ConfigurationError(
                    f"{key} 只能引用 conf/scripts 中的 .sh 或 .bash 脚本"
                )
            indexed_scripts[index] = script_path

    duplicate_indexes = sorted(set(indexed_commands) & set(indexed_scripts))
    if duplicate_indexes:
        index = duplicate_indexes[0]
        raise ConfigurationError(
            f"步骤 {index} 不能同时配置 RESTART_COMMAND_{index} 和 "
            f"RESTART_LOCAL_SCRIPT_{index}"
        )

    indexed_steps = sorted(set(indexed_commands) | set(indexed_scripts))
    for key, value in values.items():
        option_match = re.fullmatch(r"RESTART_(?:DELAY|PATH)_(\d+)", key)
        if option_match and value.strip():
            index = int(option_match.group(1))
            if index not in indexed_steps:
                raise ConfigurationError(
                    f"配置了 {key}，但缺少对应的第 {index} 个执行步骤"
                )

    if indexed_steps:
        expected_steps = list(range(1, indexed_steps[-1] + 1))
        if indexed_steps != expected_steps:
            raise ConfigurationError(
                "重启步骤编号必须从 1 开始并且连续，例如 1、2、3"
            )

        steps: list[RestartCommandStep] = []
        for index in indexed_steps:
            delay_key = f"RESTART_DELAY_{index}"
            delay_value = values.get(delay_key, "0").strip() or "0"
            delay = _finite_float(delay_value, delay_key)
            if delay < 0:
                raise ConfigurationError(f"{delay_key} 不能小于 0")
            steps.append(
                RestartCommandStep(
                    command=indexed_commands.get(index),
                    local_script_path=indexed_scripts.get(index),
                    working_directory=(
                        values.get(f"RESTART_PATH_{index}", "").strip() or None
                    ),
                    delay_after=delay,
                )
            )

        return tuple(steps)

    command = values.get("RESTART_COMMAND", "").strip()
    if command:
        if values.get("RESTART_SCRIPT_PATH", "").strip():
            raise ConfigurationError(
                "RESTART_COMMAND 不能和 RESTART_SCRIPT_PATH 同时使用"
            )
        return (
            RestartCommandStep(
                command=_validate_restart_command(command, "RESTART_COMMAND"),
                working_directory=values.get("RESTART_PATH", "").strip() or None,
            ),
        )

    legacy_restart_path = values.get("RESTART_SCRIPT_PATH", "").strip()
    legacy_working_directory = values.get("RESTART_PATH", "").strip() or None
    legacy_steps: list[RestartCommandStep] = []
    if run_script_path:
        legacy_steps.append(
            RestartCommandStep(
                command=_legacy_remote_script_command(run_script_path),
                working_directory=legacy_working_directory,
            )
        )
    if legacy_restart_path:
        legacy_steps.append(
            RestartCommandStep(
                command=(
                    f"{_legacy_remote_script_command(legacy_restart_path)} restart"
                ),
                working_directory=legacy_working_directory,
            )
        )
    if legacy_steps:
        return tuple(legacy_steps)

    if legacy_working_directory:
        raise ConfigurationError(
            "配置 RESTART_PATH 时必须同时配置一个执行命令或脚本"
        )

    raise ConfigurationError(
        "缺少配置项：RESTART_COMMAND_1（或 RESTART_COMMAND）"
    )


def _validate_restart_command(command: str, key: str) -> str:
    if _contains_uninspectable_shell_syntax(command):
        raise ConfigurationError(
            f"{key} 包含命令替换、反引号或 Shell 分组，无法在上传前可靠检查；"
            "请拆成多个执行步骤，或写入 conf/scripts 脚本"
        )
    try:
        if not shlex.split(command, posix=True):
            raise ValueError
    except ValueError as exc:
        raise ConfigurationError(f"{key} 格式错误，请检查引号是否完整") from exc
    return command


def _contains_uninspectable_shell_syntax(command: str) -> bool:
    """Reject syntax whose nested commands cannot be safely preflighted."""

    single_quoted = False
    double_quoted = False
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and not single_quoted:
            escaped = True
            index += 1
            continue
        if character == "'" and not double_quoted:
            single_quoted = not single_quoted
            index += 1
            continue
        if character == '"' and not single_quoted:
            double_quoted = not double_quoted
            index += 1
            continue
        if single_quoted:
            index += 1
            continue
        if character == "`":
            return True
        if command.startswith(("$(", "<(", ">("), index):
            return True
        if not double_quoted and character in {"(", ")"}:
            return True
        index += 1
    return False


def _legacy_remote_script_command(script_path: str) -> str:
    quoted_path = shlex.quote(script_path)
    if script_path.startswith(("/", "./", "../")):
        return quoted_path
    return f"./{quoted_path}"


def _resolve_script_path(value: str, task_directory: Path) -> Path:
    script_root = task_directory.parent / "scripts"
    script_path = _resolve_conf_reference(value, script_root, "脚本引用")
    draft_path = (
        script_root.parent
        / ".drafts"
        / "script"
        / f"{script_path.name}.draft"
    )
    if draft_path.is_file():
        raise ConfigurationError(
            f"脚本 {script_path.name} 存在未保存的暂存内容；"
            "请先在脚本页面点击保存"
        )
    return script_path


def _resolve_conf_reference(value: str, root: Path, key: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if path.is_absolute() or path.drive:
        raise ConfigurationError(f"{key} 不能使用绝对路径：{value}")
    if path.name != expanded:
        raise ConfigurationError(f"{key} 只能填写文件名，不能包含目录：{value}")

    resolved_root = root.resolve()
    resolved_path = (resolved_root / path).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ConfigurationError(f"{key} 必须位于目录 {resolved_root} 中")
    return resolved_path


def _finite_float(value: str, key: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{key} 必须是数字") from exc
    if not math.isfinite(number):
        raise ConfigurationError(f"{key} 必须是有限数字，不能使用 NaN 或 Infinity")
    return number


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
