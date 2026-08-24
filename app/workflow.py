"""Ordered task-workflow configuration.

New task files use STEP_<number>_<FIELD> keys.  Each step owns only the
parameters it needs, so server A and server B can be used in one task.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from config import ConfigurationError


_STEP_KEY = re.compile(r"STEP_(\d+)_([A-Z][A-Z0-9_]*)")


@dataclass(frozen=True)
class WorkflowFieldDefinition:
    key: str
    label: str
    required: bool = False
    selector: str = "text"
    default: str = ""


@dataclass(frozen=True)
class WorkflowTypeDefinition:
    key: str
    label: str
    fields: tuple[WorkflowFieldDefinition, ...]


WORKFLOW_TYPES = (
    WorkflowTypeDefinition("SERVER_PARAMETER", "连接服务器", (
        WorkflowFieldDefinition("CONNECTION_NAME", "连接名称", True),
        WorkflowFieldDefinition("PARAMETER_FILE", "服务器配置文件", True, "parameter"),
    )),
    WorkflowTypeDefinition("MERGE_BRANCH", "合并分支（pull → merge → commit → push）", (
        WorkflowFieldDefinition(
            "TARGET_PROJECT_PATH", "目标项目目录（在该目录将其他分支合并到本分支并推送）", True
        ),
        WorkflowFieldDefinition(
            "TARGET_BRANCH", "目标分支（接收源分支代码并推送）", True
        ),
        WorkflowFieldDefinition(
            "SOURCE_PROJECT_PATH", "源项目目录（自动读取该目录当前分支作为待合并分支）", True
        ),
        WorkflowFieldDefinition(
            "COMMIT_MESSAGE", "无代码差异时已暂存修改的提交说明", False,
            default="自动提交暂存修改"
        ),
    )),
    WorkflowTypeDefinition("PUSH_BRANCH", "推送分支（pull → commit → push）", (
        WorkflowFieldDefinition("PROJECT_PATH", "执行推送的本地项目目录", True),
        WorkflowFieldDefinition(
            "COMMIT_MESSAGE", "已暂存修改的提交说明", False,
            default="自动提交任务修改"
        ),
    )),
    WorkflowTypeDefinition("BUILD", "Maven 打包", (
        WorkflowFieldDefinition("PROJECT_PATH", "项目目录", True),
        WorkflowFieldDefinition("JAR_FILE", "JAR 文件（可留空自动识别）"),
        WorkflowFieldDefinition("ARTIFACT_NAME", "产物名称", True, default="artifact"),
        WorkflowFieldDefinition("BUILD_TIMEOUT", "打包超时（秒）", True, default="900"),
    )),
    WorkflowTypeDefinition("LOCAL_COMMAND", "执行本地命令（Windows）", (
        WorkflowFieldDefinition("COMMAND", "本地命令", True),
        WorkflowFieldDefinition("PATH", "本地工作目录（留空使用程序当前目录）"),
        WorkflowFieldDefinition("TIMEOUT", "执行超时（秒）", True, default="900"),
    )),
    WorkflowTypeDefinition("LOCAL_SCRIPT", "执行本地脚本（Windows）", (
        WorkflowFieldDefinition("SCRIPT_FILE", "本地脚本文件", True, "local_script"),
        WorkflowFieldDefinition("ARGUMENTS", "脚本参数（可留空）"),
        WorkflowFieldDefinition("PATH", "本地工作目录（留空使用脚本所在目录）"),
        WorkflowFieldDefinition("TIMEOUT", "执行超时（秒）", True, default="900"),
    )),
    WorkflowTypeDefinition("UPLOAD", "上传文件", (
        WorkflowFieldDefinition("CONNECTION_NAME", "使用连接", True, "connection"),
        WorkflowFieldDefinition("SOURCE_MODE", "文件来源", False, "upload_source", "ARTIFACT"),
        WorkflowFieldDefinition("ARTIFACT_NAME", "自动识别的打包产物", False, "artifact"),
        WorkflowFieldDefinition("LOCAL_PATH", "本地文件目录（手动上传）"),
        WorkflowFieldDefinition("FILE_NAME", "文件名称（手动上传）"),
        WorkflowFieldDefinition("FOLDER_PATH", "本地文件夹路径（上传文件夹）"),
        WorkflowFieldDefinition(
            "FOLDER_MODE", "文件夹上传方式", False, "folder_mode", "INCLUDE_FOLDER"
        ),
        WorkflowFieldDefinition("REMOTE_PATH", "服务器上传目录", True),
        WorkflowFieldDefinition(
            "CREATE_REMOTE_PATH",
            "服务器上传目录不存在时是否创建",
            False,
            "create_remote_path",
            "NO",
        ),
        WorkflowFieldDefinition(
            "BACKUP_ENABLED", "是否备份文件/文件夹", False, "backup_enabled", "NO"
        ),
        WorkflowFieldDefinition(
            "BACKUP_PATH", "备份目录（服务器真实路径）"
        ),
    )),
    WorkflowTypeDefinition("REMOTE_COMMAND", "执行远程命令", (
        WorkflowFieldDefinition("CONNECTION_NAME", "使用连接", True, "connection"),
        WorkflowFieldDefinition("COMMAND", "执行命令", True),
        WorkflowFieldDefinition("PATH", "执行目录（可留空，不读取配置默认路径）"),
        WorkflowFieldDefinition("TIMEOUT", "执行超时（秒）", False, default="300"),
    )),
    WorkflowTypeDefinition("REMOTE_SCRIPT", "执行脚本", (
        WorkflowFieldDefinition("CONNECTION_NAME", "使用连接", True, "connection"),
        WorkflowFieldDefinition("SCRIPT_FILE", "脚本文件", True, "remote_script"),
        WorkflowFieldDefinition("PATH", "执行目录（可留空，不读取配置默认路径）"),
        WorkflowFieldDefinition("TIMEOUT", "执行超时（秒）", False, default="300"),
        WorkflowFieldDefinition("DELAY", "完成后等待（秒）", False, default="0"),
    )),
    WorkflowTypeDefinition("WAIT", "等待", (
        WorkflowFieldDefinition("SECONDS", "等待秒数", True, default="1"),
    )),
    WorkflowTypeDefinition("HEALTH_CHECK", "健康检查", (
        WorkflowFieldDefinition("CONNECTION_NAME", "使用连接", True, "connection"),
        WorkflowFieldDefinition("COMMAND", "检查命令", True),
        WorkflowFieldDefinition("TIMEOUT", "最长等待（秒）", True, default="120"),
        WorkflowFieldDefinition("INTERVAL", "检查间隔（秒）", True, default="3"),
        WorkflowFieldDefinition("SUCCESS_COUNT", "连续成功次数", True, default="2"),
    )),
)
WORKFLOW_TYPE_BY_KEY = {definition.key: definition for definition in WORKFLOW_TYPES}


@dataclass(frozen=True)
class WorkflowStep:
    index: int
    type: str
    values: dict[str, str]


@dataclass(frozen=True)
class WorkflowTask:
    name: str
    file_path: Path
    steps: tuple[WorkflowStep, ...]


def is_workflow_task(path: str | Path) -> bool:
    content = Path(path).read_text(encoding="utf-8-sig")
    if bool(re.search(r"(?m)^\s*STEP_\d+_TYPE\s*=", content)):
        return True
    try:
        document = json.loads(content)
    except json.JSONDecodeError:
        return False
    return isinstance(document, dict) and isinstance(document.get("steps"), list)


def load_workflow_task(path: str | Path) -> WorkflowTask:
    file_path = Path(path).resolve()
    content = file_path.read_text(encoding="utf-8-sig")
    try:
        document = json.loads(content)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("steps"), list):
        return _load_object_workflow(file_path, document["steps"])
    values: dict[int, dict[str, str]] = {}
    for line_number, raw_line in enumerate(
        content.splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"第 {line_number} 行必须使用 KEY=VALUE 格式")
        key, value = (part.strip() for part in line.split("=", 1))
        match = _STEP_KEY.fullmatch(key.upper())
        if match is None:
            raise ConfigurationError(
                f"第 {line_number} 行存在不支持的配置项：{key}"
            )
        index = int(match.group(1))
        if index <= 0:
            raise ConfigurationError("步骤编号必须从 1 开始")
        step_values = values.setdefault(index, {})
        field = match.group(2)
        if field in step_values:
            raise ConfigurationError(f"第 {line_number} 行重复配置 STEP_{index}_{field}")
        step_values[field] = value

    if not values:
        raise ConfigurationError("任务中没有流程步骤，请点击“增加步骤”创建")
    indexes = sorted(values)

    steps: list[WorkflowStep] = []
    for index in indexes:
        step_values = values[index]
        step_type = step_values.get("TYPE", "").upper()
        if step_type not in WORKFLOW_TYPE_BY_KEY:
            raise ConfigurationError(f"第 {index} 步的 TYPE 不支持：{step_type or '未填写'}")
        _validate_step(index, step_type, step_values)
        steps.append(WorkflowStep(index, step_type, step_values))
    _validate_unique_names(steps)
    return WorkflowTask(file_path.stem, file_path, tuple(steps))


def _load_object_workflow(file_path: Path, raw_steps: list[object]) -> WorkflowTask:
    if not raw_steps:
        raise ConfigurationError("任务中没有流程步骤，请点击“增加步骤”创建")
    steps: list[WorkflowStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise ConfigurationError(f"第 {index} 步必须是步骤对象")
        step_type = str(raw_step.get("type", "")).strip().upper()
        raw_values = raw_step.get("properties", {})
        if not isinstance(raw_values, dict):
            raise ConfigurationError(f"第 {index} 步的 properties 必须是对象")
        values = {str(key).upper(): str(value) for key, value in raw_values.items()}
        values["TYPE"] = step_type
        if step_type not in WORKFLOW_TYPE_BY_KEY:
            raise ConfigurationError(f"第 {index} 步的类型不支持：{step_type or '未填写'}")
        _validate_step(index, step_type, values)
        steps.append(WorkflowStep(index, step_type, values))
    _validate_unique_names(steps)
    return WorkflowTask(file_path.stem, file_path, tuple(steps))


def _validate_step(index: int, step_type: str, values: dict[str, str]) -> None:
    definition = WORKFLOW_TYPE_BY_KEY[step_type]
    allowed = {"TYPE", *(field.key for field in definition.fields)}
    unsupported = sorted(set(values) - allowed)
    if unsupported:
        raise ConfigurationError(
            f"第 {index} 步 {step_type} 存在不支持的参数：{', '.join(unsupported)}"
        )
    missing = [
        field.key
        for field in definition.fields
        if field.required and not values.get(field.key, "").strip()
    ]
    if missing:
        raise ConfigurationError(f"第 {index} 步 {step_type} 缺少：{', '.join(missing)}")
    if step_type == "UPLOAD":
        create_remote_path = (
            values.get("CREATE_REMOTE_PATH", "NO").strip().upper() or "NO"
        )
        if create_remote_path not in {"YES", "NO"}:
            raise ConfigurationError(
                f"第 {index} 步 UPLOAD 的 CREATE_REMOTE_PATH 只能填写 YES 或 NO"
            )
        backup_enabled = values.get("BACKUP_ENABLED", "NO").strip().upper() or "NO"
        if backup_enabled not in {"YES", "NO"}:
            raise ConfigurationError(
                f"第 {index} 步 UPLOAD 的 BACKUP_ENABLED 只能填写 YES 或 NO"
            )
        backup_path = values.get("BACKUP_PATH", "").strip()
        if backup_enabled == "YES" and not backup_path:
            raise ConfigurationError(
                f"第 {index} 步 UPLOAD 启用备份后必须填写 BACKUP_PATH"
            )
        if backup_enabled == "NO" and backup_path:
            raise ConfigurationError(
                f"第 {index} 步 UPLOAD 未启用备份，不能填写 BACKUP_PATH"
            )


def _validate_unique_names(steps: list[WorkflowStep]) -> None:
    for step_type, field, label in (
        ("SERVER_PARAMETER", "CONNECTION_NAME", "连接名称"),
        ("BUILD", "ARTIFACT_NAME", "产物名称"),
    ):
        seen: dict[str, int] = {}
        for step in steps:
            if step.type != step_type:
                continue
            value = step.values.get(field, "").strip()
            previous_index = seen.get(value)
            if previous_index is not None:
                raise ConfigurationError(
                    f"第 {step.index} 步的{label}“{value}”与第 "
                    f"{previous_index} 步重复"
                )
            seen[value] = step.index
