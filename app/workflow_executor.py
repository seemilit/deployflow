"""Execute an ordered workflow task one step at a time."""

from __future__ import annotations

import os
import posixpath
import locale
import math
import queue
import re
import shlex
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from builder import MavenBuilder
from config import ConfigurationError, ServerParameters, load_server_parameters
from deployer import FabricDeployer
from workflow import WORKFLOW_TYPE_BY_KEY, WorkflowStep, WorkflowTask


LogCallback = Callable[[str], None]
StatusCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]


class WorkflowExecutionError(RuntimeError):
    """Raised when one ordered workflow step cannot be completed."""


@dataclass(frozen=True)
class _UploadedFileSwap:
    target_path: str
    backup_path: str
    had_previous: bool


@dataclass(frozen=True)
class _UploadedFolderSwap:
    target_root: str
    backup_root: str
    files: tuple[tuple[str, str | None], ...]
    created_directories: tuple[str, ...]
    created_target_root: bool


class WorkflowExecutor:
    def __init__(
        self,
        parameter_dir: Path,
        script_dir: Path,
        connect_timeout: int = 20,
        command_timeout: int = 300,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.parameter_dir = parameter_dir.resolve()
        self.script_dir = script_dir.resolve()
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._cancel_event = cancel_event or threading.Event()
        self._connections: dict[str, Any] = {}
        self._parameters: dict[str, ServerParameters] = {}
        self._artifacts: dict[str, Path] = {}
        self._upload_swaps: list[
            tuple[str, _UploadedFileSwap | _UploadedFolderSwap]
        ] = []

    def execute(
        self,
        task: WorkflowTask,
        log: LogCallback,
        status: StatusCallback,
        progress: ProgressCallback,
    ) -> None:
        try:
            for position, step in enumerate(task.steps, start=1):
                self._check_cancelled()
                definition = WORKFLOW_TYPE_BY_KEY[step.type]
                status(f"正在执行第 {position}/{len(task.steps)} 步：{definition.label}……")
                log(f"[{position}/{len(task.steps)}] {definition.label}")
                try:
                    self._execute_step(step, log, progress)
                except WorkflowExecutionError:
                    raise
                except Exception as exc:
                    detail = str(exc) or exc.__class__.__name__
                    raise WorkflowExecutionError(
                        f"第 {step.index} 步“{definition.label}”执行失败：{detail}"
                    ) from exc
                log(f"第 {position} 步完成")
                self._check_cancelled()
        except Exception:
            if self._upload_swaps:
                status("任务失败，正在清理上传临时备份……")
            self._discard_upload_backups(log, task_succeeded=False)
            raise
        else:
            self._discard_upload_backups(log, task_succeeded=True)
        finally:
            self._close_connections()

    def _execute_step(
        self,
        step: WorkflowStep,
        log: LogCallback,
        progress: ProgressCallback,
    ) -> None:
        handlers = {
            "SERVER_PARAMETER": self._connect,
            "MERGE_BRANCH": self._merge_branch,
            "PUSH_BRANCH": self._push_branch,
            "BUILD": self._build,
            "LOCAL_COMMAND": self._local_command,
            "LOCAL_SCRIPT": self._local_script,
            "UPLOAD": self._upload,
            "REMOTE_COMMAND": self._remote_command,
            "REMOTE_SCRIPT": self._remote_script,
            "WAIT": self._wait,
            "HEALTH_CHECK": self._health_check,
        }
        handler = handlers[step.type]
        if step.type == "UPLOAD":
            handler(step, log, progress)
        else:
            handler(step, log)

    def _connect(self, step: WorkflowStep, log: LogCallback) -> None:
        self._check_cancelled()
        name = step.values["CONNECTION_NAME"].strip()
        if name in self._connections:
            raise WorkflowExecutionError(f"连接名称重复：{name}")
        parameter_path = self._referenced_file(
            self.parameter_dir,
            step.values["PARAMETER_FILE"],
            "服务器配置文件",
        )
        parameters = load_server_parameters(parameter_path)
        connection = self._new_connection(parameters)
        log(f"连接服务器：{parameters.target}")
        try:
            connection.open()
            self._check_cancelled()
            transport = connection.client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
        except Exception as exc:
            try:
                connection.close()
            except Exception:
                pass
            detail = str(exc).lower()
            if "known_hosts" in detail or "host key" in detail:
                raise WorkflowExecutionError(
                    "服务器主机密钥尚未信任或已发生变化，请先在“配置”页面连接并确认指纹"
                ) from exc
            raise WorkflowExecutionError(f"连接服务器失败：{exc}") from exc
        self._connections[name] = connection
        self._parameters[name] = parameters
        log(f"连接成功：{name}")

    def _new_connection(self, parameters: ServerParameters) -> Any:
        try:
            from fabric import Config, Connection
            from paramiko import RejectPolicy, SSHConfig
        except ImportError as exc:
            raise WorkflowExecutionError(
                f"SSH 依赖加载失败（{type(exc).__name__}）：{exc}"
            ) from exc

        connect_kwargs: dict[str, Any] = {"allow_agent": False, "look_for_keys": False}
        if parameters.auth_method == "PASSWORD":
            connect_kwargs.update(allow_agent=False, look_for_keys=False)
            if parameters.password:
                connect_kwargs["password"] = parameters.password
        elif parameters.auth_method == "KEY":
            if parameters.key_filename:
                connect_kwargs["key_filename"] = str(parameters.key_filename)
        else:
            if parameters.password:
                connect_kwargs.update(
                    password=parameters.password,
                    allow_agent=False,
                    look_for_keys=False,
                )
            if parameters.key_filename:
                connect_kwargs["key_filename"] = str(parameters.key_filename)
        connection = Connection(
            host=parameters.ip_address,
            port=parameters.port,
            user=parameters.username,
            connect_kwargs=connect_kwargs,
            connect_timeout=self.connect_timeout,
            config=Config(ssh_config=SSHConfig(), lazy=True),
        )
        known_hosts_path = self.parameter_dir.parent / "known_hosts"
        try:
            if known_hosts_path.is_file():
                connection.client.load_host_keys(str(known_hosts_path))
            connection.client.set_missing_host_key_policy(RejectPolicy())
        except (OSError, ValueError) as exc:
            raise WorkflowExecutionError(f"无法读取 SSH 主机密钥记录：{exc}") from exc
        return connection

    def _merge_branch(self, step: WorkflowStep, log: LogCallback) -> None:
        target_path = self._directory(
            step.values["TARGET_PROJECT_PATH"], "执行合并的项目目录"
        )
        source_path_value = step.values.get("SOURCE_PROJECT_PATH", "").strip()
        source_path = self._directory(source_path_value, "源项目目录")
        if source_path == target_path:
            raise WorkflowExecutionError(
                "源项目目录和目标项目目录不能相同，否则切换目标分支后无法正确读取源分支"
            )
        target_branch = step.values["TARGET_BRANCH"].strip()
        commit_message = (
            step.values.get("COMMIT_MESSAGE", "").strip()
            or "自动提交暂存修改"
        )
        log(f"目标项目目录：{target_path}")
        log(f"目标分支：{target_branch}")
        current_target_branch = self._git(
            target_path, "symbolic-ref", "--quiet", "--short", "HEAD"
        ).strip()
        target_changes = self._git(target_path, "status", "--porcelain").strip()
        if current_target_branch != target_branch:
            if target_changes:
                raise WorkflowExecutionError(
                    f"目标项目当前位于 {current_target_branch}，且存在未提交修改；"
                    f"无法安全切换到目标分支 {target_branch}"
                )
            log(f"切换目标分支：{target_branch}")
            self._git_logged(target_path, log, "switch", target_branch)
        else:
            log(f"当前已位于目标分支：{target_branch}")
        target_remote = self._branch_remote(target_path, target_branch)
        log(f"拉取目标分支最新代码：{target_remote}/{target_branch}")
        pull_result = self._git_process(
            target_path,
            "pull",
            "--ff-only",
            target_remote,
            target_branch,
        )
        self._log_process(pull_result, log)
        if pull_result.returncode != 0:
            detail = (
                pull_result.stderr.strip()
                or pull_result.stdout.strip()
                or str(pull_result.returncode)
            )
            raise WorkflowExecutionError(
                "拉取目标分支失败，当前分支未执行合并，已停止后续操作："
                f"{detail}"
            )

        log(f"源项目目录：{source_path}")
        source_branch = self._git(
            source_path, "symbolic-ref", "--quiet", "--short", "HEAD"
        ).strip()
        if not source_branch:
            raise WorkflowExecutionError("源项目当前处于 detached HEAD，无法读取分支")
        source_commit = self._git(
            source_path, "rev-parse", "--verify", f"{source_branch}^{{commit}}"
        ).strip()
        log(f"动态读取源分支：{source_branch}@{source_commit[:12]}")
        self._git_logged(
            target_path, log, "fetch", "--no-tags", str(source_path), source_commit
        )

        target_commit = self._git(
            target_path, "rev-parse", "--verify", "HEAD^{commit}"
        ).strip()
        source_already_merged = self._git_process(
            target_path, "merge-base", "--is-ancestor", source_commit, target_commit
        )
        merge_required = True
        if source_already_merged.returncode == 0:
            log(
                f"源分支 {source_branch} 的提交已包含在目标分支 "
                f"{target_branch} 中，跳过合并"
            )
            merge_required = False
        elif source_already_merged.returncode != 1:
            detail = (
                source_already_merged.stderr.strip()
                or source_already_merged.stdout.strip()
                or str(source_already_merged.returncode)
            )
            raise WorkflowExecutionError(f"比较目标分支和源分支失败：{detail}")

        if merge_required:
            same_content = self._git_process(
                target_path, "diff", "--quiet", target_commit, source_commit, "--"
            )
            if same_content.returncode == 0:
                log(
                    f"目标分支 {target_branch} 与源分支 {source_branch} "
                    "的文件内容没有差异，跳过合并"
                )
                merge_required = False
            elif same_content.returncode != 1:
                detail = (
                    same_content.stderr.strip()
                    or same_content.stdout.strip()
                    or str(same_content.returncode)
                )
                raise WorkflowExecutionError(
                    f"比较目标分支和源分支内容失败：{detail}"
                )

        if merge_required:
            log(f"预检查合并冲突：{source_branch} -> {target_branch}")
            merge_check = self._git_process(
                target_path, "merge-tree", "--write-tree", target_commit, source_commit
            )
            if merge_check.returncode == 1:
                self._log_process(merge_check, log)
                raise WorkflowExecutionError(
                    f"检测到 {source_branch} 合并到 {target_branch} 时存在冲突；"
                    "未执行实际合并，目标项目保持在合并前状态，请手动处理"
                )
            if merge_check.returncode != 0:
                detail = (
                    merge_check.stderr.strip()
                    or merge_check.stdout.strip()
                    or str(merge_check.returncode)
                )
                raise WorkflowExecutionError(
                    "无法执行合并冲突预检查，请确认本机 Git 版本不低于 2.38："
                    f"{detail}"
                )
            log("合并冲突预检查通过")

            status_before_merge = self._git(
                target_path,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            )
            log(f"合并源分支 {source_branch} -> 目标分支 {target_branch}")
            result = self._git_process(
                target_path, "merge", "--no-ff", "--no-edit", source_commit
            )
            self._log_process(result, log)
            if result.returncode != 0:
                detail = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or str(result.returncode)
                )
                merge_in_progress = (
                    self._git_process(
                        target_path,
                        "rev-parse",
                        "--verify",
                        "--quiet",
                        "MERGE_HEAD",
                    ).returncode
                    == 0
                )
                abort_result = None
                if merge_in_progress:
                    abort_result = self._git_process(target_path, "merge", "--abort")
                    self._log_process(abort_result, log)

                head_after_failure = self._git(
                    target_path, "rev-parse", "--verify", "HEAD^{commit}"
                ).strip()
                status_after_failure = self._git(
                    target_path,
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                )
                merge_still_in_progress = (
                    self._git_process(
                        target_path,
                        "rev-parse",
                        "--verify",
                        "--quiet",
                        "MERGE_HEAD",
                    ).returncode
                    == 0
                )
                restored = (
                    head_after_failure == target_commit
                    and status_after_failure == status_before_merge
                    and not merge_still_in_progress
                    and (abort_result is None or abort_result.returncode == 0)
                )
                if restored:
                    raise WorkflowExecutionError(
                        "分支合并失败，目标项目已恢复到合并前状态，"
                        f"已停止后续操作：{detail}"
                    )
                raise WorkflowExecutionError(
                    "分支合并失败，且无法确认目标项目已完全恢复；"
                    "已停止后续操作，请立即人工检查仓库状态："
                    f"{detail}"
                )
            log("分支合并成功，Git 已自动创建合并提交")
        else:
            self._commit_changes(
                target_path,
                commit_message,
                log,
                f"目标分支 {target_branch}",
            )

        log(f"推送目标分支：{target_remote}/{target_branch}")
        self._git_logged(target_path, log, "push", target_remote, target_branch)

    def _push_branch(self, step: WorkflowStep, log: LogCallback) -> None:
        project_path = self._directory(
            step.values["PROJECT_PATH"], "执行推送的本地项目目录"
        )
        branch = self._git(
            project_path, "symbolic-ref", "--quiet", "--short", "HEAD"
        ).strip()
        if not branch:
            raise WorkflowExecutionError("当前项目处于 detached HEAD，无法推送")
        commit_message = (
            step.values.get("COMMIT_MESSAGE", "").strip() or "自动提交任务修改"
        )
        log(f"拉取当前分支最新代码：{branch}")
        pull_result = self._git_process(project_path, "pull", "--ff-only")
        self._log_process(pull_result, log)
        if pull_result.returncode != 0:
            detail = (
                pull_result.stderr.strip()
                or pull_result.stdout.strip()
                or str(pull_result.returncode)
            )
            raise WorkflowExecutionError(
                "拉取当前分支失败，未执行提交和推送，已停止后续操作："
                f"{detail}"
            )
        self._commit_changes(project_path, commit_message, log, "当前分支")
        log(f"推送当前分支：{branch}（使用 Git 已配置的上游仓库）")
        self._git_logged(project_path, log, "push")

    def _branch_remote(self, repository: Path, branch: str) -> str:
        configured = self._git_process(
            repository, "config", "--get", f"branch.{branch}.remote"
        )
        remote = configured.stdout.strip() if configured.returncode == 0 else ""
        if remote and remote != ".":
            return remote
        remotes = [
            value.strip()
            for value in self._git(repository, "remote").splitlines()
            if value.strip()
        ]
        if "origin" in remotes:
            return "origin"
        if len(remotes) == 1:
            return remotes[0]
        raise WorkflowExecutionError(
            f"目标分支 {branch} 没有配置远程仓库，且无法自动确定使用哪个 remote"
        )

    def _commit_changes(
        self,
        repository: Path,
        message: str,
        log: LogCallback,
        label: str,
    ) -> None:
        staged = self._git_process(
            repository, "diff", "--cached", "--quiet", "--exit-code"
        )
        merge_in_progress = (
            self._git_process(
                repository, "rev-parse", "--verify", "--quiet", "MERGE_HEAD"
            ).returncode
            == 0
        )
        if staged.returncode == 0 and not merge_in_progress:
            log(f"{label}没有已暂存（git add）的修改，跳过提交")
            self._log_uncommitted_changes(repository, log)
            return
        if staged.returncode != 1:
            detail = staged.stderr.strip() or staged.stdout.strip()
            raise WorkflowExecutionError(f"检查暂存区失败：{detail}")
        log(f"提交{label}修改：{message}")
        self._git_logged(repository, log, "commit", "-m", message)
        self._log_uncommitted_changes(repository, log)

    def _commit_merge_result(
        self,
        repository: Path,
        log: LogCallback,
    ) -> None:
        merge_in_progress = (
            self._git_process(
                repository, "rev-parse", "--verify", "--quiet", "MERGE_HEAD"
            ).returncode
            == 0
        )
        if merge_in_progress:
            log("使用 Git 自动生成的合并说明提交")
            self._git_logged(repository, log, "commit", "--no-edit")
            self._log_uncommitted_changes(repository, log)
            return

        staged = self._git_process(
            repository, "diff", "--cached", "--quiet", "--exit-code"
        )
        if staged.returncode == 1:
            log("源分支已合并，提交目标分支中已有的暂存修改")
            self._git_logged(
                repository,
                log,
                "commit",
                "-m",
                "自动提交暂存修改",
            )
        elif staged.returncode != 0:
            detail = staged.stderr.strip() or staged.stdout.strip()
            raise WorkflowExecutionError(f"检查暂存区失败：{detail}")
        else:
            log("没有新的合并结果或已暂存修改，跳过提交")
        self._log_uncommitted_changes(repository, log)

    def _log_uncommitted_changes(
        self,
        repository: Path,
        log: LogCallback,
    ) -> None:
        remaining = self._git(repository, "status", "--porcelain").splitlines()
        if remaining:
            log(
                f"保留 {len(remaining)} 项未暂存或未跟踪修改，"
                "这些内容不会进入本次提交和合并"
            )

    def _build(self, step: WorkflowStep, log: LogCallback) -> None:
        project_path = self._directory(step.values["PROJECT_PATH"], "项目目录")
        timeout = self._positive_int(step.values["BUILD_TIMEOUT"], "打包超时")
        artifact_name = step.values["ARTIFACT_NAME"].strip()
        if artifact_name in self._artifacts:
            raise WorkflowExecutionError(f"产物名称重复：{artifact_name}")
        build_config = SimpleNamespace(
            project_path=project_path,
            jar_file=step.values.get("JAR_FILE", "").strip() or None,
            build_timeout=timeout,
        )
        artifact = MavenBuilder().build(
            build_config,
            log,
            cancel_event=self._cancel_event,
        )
        self._artifacts[artifact_name] = artifact
        log(f"保存产物引用：{artifact_name} -> {artifact}")

    def _local_command(self, step: WorkflowStep, log: LogCallback) -> None:
        command = step.values["COMMAND"].strip()
        directory_value = step.values.get("PATH", "").strip()
        directory = (
            self._directory(directory_value, "本地工作目录")
            if directory_value
            else Path.cwd()
        )
        timeout = self._positive_int(step.values["TIMEOUT"], "执行超时")
        log(f"在本地目录执行命令：{directory}")
        log(f"> {command}")
        self._run_local_process(command, directory, timeout, log)

    def _local_script(self, step: WorkflowStep, log: LogCallback) -> None:
        script_path = self._referenced_file(
            self.script_dir,
            step.values["SCRIPT_FILE"],
            "本地脚本文件",
        )
        self._ensure_script_saved(script_path)
        directory_value = step.values.get("PATH", "").strip()
        directory = (
            self._directory(directory_value, "本地工作目录")
            if directory_value
            else script_path.parent
        )
        arguments = step.values.get("ARGUMENTS", "").strip()
        timeout = self._positive_int(step.values["TIMEOUT"], "执行超时")
        suffix = script_path.suffix.lower()
        if suffix in {".bat", ".cmd"}:
            command = subprocess.list2cmdline([str(script_path)])
        elif suffix == ".ps1":
            command = subprocess.list2cmdline(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                ]
            )
        elif suffix in {".sh", ".bash"}:
            command = subprocess.list2cmdline(["bash", str(script_path)])
        else:
            raise WorkflowExecutionError(
                f"不支持的本地脚本类型：{script_path.suffix}"
            )
        if arguments:
            command = f"{command} {arguments}"
        log(f"在本地目录执行脚本：{directory}")
        log(f"> {command}")
        self._run_local_process(command, directory, timeout, log)

    def _run_local_process(
        self,
        command: str,
        directory: Path,
        timeout: int,
        log: LogCallback,
    ) -> None:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                shell=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise WorkflowExecutionError(f"无法启动本地命令：{exc}") from exc

        output_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=1024)

        def read_output() -> None:
            try:
                if process.stdout is not None:
                    for raw_line in process.stdout:
                        output_queue.put(raw_line.rstrip(b"\r\n"))
            finally:
                output_queue.put(None)

        threading.Thread(
            target=read_output,
            name="local-command-output-reader",
            daemon=True,
        ).start()
        deadline = time.monotonic() + timeout
        output_finished = False
        while not output_finished:
            if self._cancel_event.is_set():
                WorkflowExecutor._terminate_local_process(process)
                raise WorkflowExecutionError("任务已由用户停止，本地进程已终止")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                WorkflowExecutor._terminate_local_process(process)
                raise WorkflowExecutionError(
                    f"本地命令执行超过 {timeout} 秒，已终止"
                )
            try:
                line = output_queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                output_finished = True
            elif line:
                log(self._decode_local_output(line))

        remaining = deadline - time.monotonic()
        if self._cancel_event.is_set():
            WorkflowExecutor._terminate_local_process(process)
            raise WorkflowExecutionError("任务已由用户停止，本地进程已终止")
        if remaining <= 0:
            WorkflowExecutor._terminate_local_process(process)
            raise WorkflowExecutionError(f"本地命令执行超过 {timeout} 秒，已终止")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            WorkflowExecutor._terminate_local_process(process)
            raise WorkflowExecutionError(
                f"本地命令执行超过 {timeout} 秒，已终止"
            ) from exc
        if return_code != 0:
            raise WorkflowExecutionError(f"本地命令退出码：{return_code}")

    @staticmethod
    def _decode_local_output(output: bytes) -> str:
        encodings = ("utf-8", locale.getpreferredencoding(False))
        decoded: str | None = None
        for encoding in dict.fromkeys(encodings):
            try:
                decoded = output.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            decoded = output.decode(
                locale.getpreferredencoding(False),
                errors="replace",
            )
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", decoded)

    @staticmethod
    def _terminate_local_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _upload(
        self,
        step: WorkflowStep,
        log: LogCallback,
        progress: ProgressCallback,
    ) -> None:
        self._check_cancelled()
        connection_name = step.values["CONNECTION_NAME"].strip()
        connection, _parameters = self._connection(connection_name)
        backup_enabled = (
            step.values.get("BACKUP_ENABLED", "NO").strip().upper() or "NO"
        ) == "YES"
        create_remote_path = (
            step.values.get("CREATE_REMOTE_PATH", "NO").strip().upper() or "NO"
        ) == "YES"
        backup_root = (
            self._remote_backup_root(
                step.values.get("REMOTE_PATH", "").strip(),
                step.values.get("BACKUP_PATH", "").strip(),
            )
            if backup_enabled
            else None
        )
        source_mode = step.values.get("SOURCE_MODE", "ARTIFACT").strip().upper()
        if source_mode == "ARTIFACT":
            artifact_name = step.values.get("ARTIFACT_NAME", "").strip()
            artifact = self._artifacts.get(artifact_name)
            if artifact is None or not artifact.is_file():
                raise WorkflowExecutionError(f"尚未生成可上传的产物：{artifact_name}")
            log(f"使用自动识别的打包产物：{artifact_name} -> {artifact}")
        elif source_mode == "LOCAL_FILE":
            local_path = step.values.get("LOCAL_PATH", "").strip()
            file_name = step.values.get("FILE_NAME", "").strip()
            if not local_path or not file_name:
                raise WorkflowExecutionError("手动上传需要填写本地文件目录和文件名称")
            artifact = (Path(local_path).expanduser() / file_name).resolve()
            if not artifact.is_file():
                raise WorkflowExecutionError(f"要上传的本地文件不存在：{artifact}")
            log(f"使用手动指定的本地文件：{artifact}")
        elif source_mode == "LOCAL_FOLDER":
            folder_value = step.values.get("FOLDER_PATH", "").strip()
            folder = Path(folder_value).expanduser().resolve()
            if not folder.is_dir():
                raise WorkflowExecutionError(f"要上传的本地文件夹不存在：{folder}")
            remote_directory = (
                step.values.get("REMOTE_PATH", "").strip()
            )
            if not remote_directory:
                raise WorkflowExecutionError(
                    "上传文件夹时必须填写服务器目录；服务器配置中也没有备用目录"
                )
            self._prepare_remote_upload_directory(
                connection,
                remote_directory,
                create_remote_path,
                log,
            )
            folder_mode = step.values.get(
                "FOLDER_MODE", "INCLUDE_FOLDER"
            ).strip().upper()
            if folder_mode not in {"INCLUDE_FOLDER", "CONTENTS_ONLY"}:
                raise WorkflowExecutionError(
                    f"不支持的文件夹上传方式：{folder_mode}"
                )
            swap = self._upload_folder(
                connection,
                connection_name,
                folder,
                remote_directory,
                folder_mode,
                backup_root,
                log,
                progress,
            )
            self._upload_swaps.append((connection_name, swap))
            return
        else:
            raise WorkflowExecutionError(f"不支持的上传文件来源：{source_mode}")
        remote_directory = (
            step.values.get("REMOTE_PATH", "").strip()
        )
        if not remote_directory:
            raise WorkflowExecutionError(
                "上传文件时必须填写服务器目录；服务器配置中也没有备用目录"
            )
        self._prepare_remote_upload_directory(
            connection,
            remote_directory,
            create_remote_path,
            log,
        )
        remote_file = posixpath.join(remote_directory, artifact.name)
        remote_temporary = posixpath.join(
            remote_directory,
            f".{artifact.name}.upload-{uuid.uuid4().hex}.tmp",
        )
        log(f"上传文件：{artifact} -> {connection_name}:{remote_file}")
        total_size = artifact.stat().st_size
        progress(0, total_size)
        log("上传进度：0%")
        last_logged_percent = 0

        def report_progress(transferred: int, total: int) -> None:
            nonlocal last_logged_percent
            self._check_cancelled()
            progress(transferred, total)
            percent = 100 if total <= 0 else min(100, int(transferred * 100 / total))
            if percent == 100 or percent >= last_logged_percent + 5:
                last_logged_percent = percent
                log(f"上传进度：{percent}%")

        sftp = connection.sftp()
        sftp.get_channel().settimeout(self.command_timeout)
        try:
            sftp.put(
                str(artifact),
                remote_temporary,
                callback=report_progress,
                confirm=True,
            )
            remote_size = int(sftp.stat(remote_temporary).st_size)
            if remote_size != total_size:
                raise WorkflowExecutionError(
                    f"上传文件大小校验失败：本地 {total_size} 字节，"
                    f"服务器 {remote_size} 字节"
                )
            self._check_cancelled()
            if backup_root is not None:
                self._backup_remote_file(
                    connection,
                    sftp,
                    remote_file,
                    backup_root,
                    log,
                )
            swap = self._activate_uploaded_file(
                sftp,
                remote_temporary,
                remote_file,
            )
            self._upload_swaps.append((connection_name, swap))
        except Exception:
            try:
                self._remove_sftp_path(sftp, remote_temporary)
            except Exception:
                pass
            raise
        progress(total_size, total_size)
        if last_logged_percent < 100:
            log("上传进度：100%")
        log(f"上传完成：{remote_file}")

    def _upload_folder(
        self,
        connection: Any,
        connection_name: str,
        folder: Path,
        remote_directory: str,
        folder_mode: str,
        backup_root: str | None,
        log: LogCallback,
        progress: ProgressCallback,
    ) -> _UploadedFolderSwap:
        files = sorted(
            (path for path in folder.rglob("*") if path.is_file()),
            key=lambda path: str(path).lower(),
        )
        directories = sorted(
            (path for path in folder.rglob("*") if path.is_dir()),
            key=lambda path: (len(path.parts), str(path).lower()),
        )
        total_size = sum(path.stat().st_size for path in files)
        remote_root = (
            posixpath.join(remote_directory, folder.name)
            if folder_mode == "INCLUDE_FOLDER"
            else remote_directory
        )
        normalized_remote_root = posixpath.normpath(remote_root)
        remote_parent = posixpath.dirname(normalized_remote_root) or "."
        remote_name = posixpath.basename(normalized_remote_root.rstrip("/")) or "root"
        upload_id = uuid.uuid4().hex
        staging_root = posixpath.join(
            remote_parent,
            f".{remote_name}.upload-{upload_id}",
        )
        log(f"上传文件夹：{folder} -> {connection_name}:{remote_root}")
        progress(0, total_size)
        log("上传进度：0%")

        sftp = connection.sftp()
        sftp.get_channel().settimeout(self.command_timeout)
        self._ensure_sftp_directory(sftp, remote_parent)
        self._ensure_sftp_directory(sftp, staging_root)
        try:
            for directory in directories:
                self._check_cancelled()
                relative = directory.relative_to(folder).as_posix()
                self._ensure_sftp_directory(
                    sftp,
                    posixpath.join(staging_root, relative),
                )

            completed_size = 0
            last_logged_percent = 0
            for local_file in files:
                self._check_cancelled()
                relative = local_file.relative_to(folder).as_posix()
                remote_file = posixpath.join(staging_root, relative)
                file_size = local_file.stat().st_size

                def report_file_progress(
                    transferred: int,
                    _file_total: int,
                    base: int = completed_size,
                ) -> None:
                    nonlocal last_logged_percent
                    self._check_cancelled()
                    cumulative = base + transferred
                    progress(cumulative, total_size)
                    percent = (
                        100
                        if total_size <= 0
                        else min(100, int(cumulative * 100 / total_size))
                    )
                    if percent == 100 or percent >= last_logged_percent + 5:
                        last_logged_percent = percent
                        log(f"上传进度：{percent}%")

                sftp.put(
                    str(local_file),
                    remote_file,
                    callback=report_file_progress,
                    confirm=True,
                )
                remote_size = int(sftp.stat(remote_file).st_size)
                if remote_size != file_size:
                    raise WorkflowExecutionError(
                        f"上传文件大小校验失败：{relative}，"
                        f"本地 {file_size} 字节，服务器 {remote_size} 字节"
                    )
                completed_size += file_size

            relative_files = [
                path.relative_to(folder).as_posix()
                for path in files
            ]
            relative_directories = [
                path.relative_to(folder).as_posix()
                for path in directories
            ]
            self._check_cancelled()
            if backup_root is not None:
                self._backup_remote_folder(
                    connection,
                    sftp,
                    normalized_remote_root,
                    relative_files,
                    folder_mode,
                    backup_root,
                    log,
                )
            log("文件传输完成，正在检查并替换服务器同名文件……")
            swap = self._activate_uploaded_contents(
                sftp,
                staging_root,
                normalized_remote_root,
                relative_files,
                relative_directories,
                upload_id,
                log,
            )
        except Exception:
            try:
                self._remove_sftp_path(sftp, staging_root)
            except Exception:
                pass
            raise

        progress(total_size, total_size)
        if last_logged_percent < 100:
            log("上传进度：100%")
        log(f"文件夹上传完成：{remote_root}（{len(files)} 个文件）")
        return swap

    @staticmethod
    def _remote_backup_root(remote_directory: str, backup_path: str) -> str:
        if not backup_path:
            raise WorkflowExecutionError("启用上传前备份后必须填写服务器备份目录")
        if posixpath.isabs(backup_path):
            return posixpath.normpath(backup_path)
        return posixpath.normpath(posixpath.join(remote_directory, backup_path))

    def _backup_remote_file(
        self,
        connection: Any,
        sftp: Any,
        target_file: str,
        backup_root: str,
        log: LogCallback,
    ) -> None:
        target_attributes = self._sftp_attributes(sftp, target_file)
        if target_attributes is None:
            log(f"服务器原文件不存在，跳过备份：{target_file}")
            return
        if not stat.S_ISREG(target_attributes.st_mode):
            raise WorkflowExecutionError(
                f"服务器上传目标不是普通文件，无法备份：{target_file}"
            )
        backup_directory = self._dated_backup_directory(
            connection,
            sftp,
            backup_root,
        )
        file_name = posixpath.basename(target_file)
        stem, extension = posixpath.splitext(file_name)
        backup_file = self._next_backup_path(
            sftp,
            backup_directory,
            stem,
            extension,
        )
        try:
            self._copy_remote_path(
                connection,
                target_file,
                backup_file,
                directory=False,
            )
        except Exception:
            try:
                self._remove_sftp_path(sftp, backup_file)
            except Exception:
                pass
            raise
        log(f"已备份服务器文件：{target_file} -> {backup_file}")

    def _backup_remote_folder(
        self,
        connection: Any,
        sftp: Any,
        target_root: str,
        relative_files: list[str],
        folder_mode: str,
        backup_root: str,
        log: LogCallback,
    ) -> None:
        if folder_mode == "INCLUDE_FOLDER":
            target_attributes = self._sftp_attributes(sftp, target_root)
            if target_attributes is None:
                log(f"服务器原文件夹不存在，跳过备份：{target_root}")
                return
            if not stat.S_ISDIR(target_attributes.st_mode):
                raise WorkflowExecutionError(
                    f"服务器上传目标不是文件夹，无法备份：{target_root}"
                )
            normalized_target = posixpath.normpath(target_root)
            normalized_backup = posixpath.normpath(backup_root)
            if (
                normalized_backup == normalized_target
                or normalized_backup.startswith(normalized_target.rstrip("/") + "/")
            ):
                raise WorkflowExecutionError(
                    "保留最外层文件夹时，服务器备份目录不能位于被备份文件夹内部"
                )
            backup_directory = self._dated_backup_directory(
                connection,
                sftp,
                backup_root,
            )
            backup_folder = self._next_backup_path(
                sftp,
                backup_directory,
                posixpath.basename(normalized_target.rstrip("/")) or "folder",
                "",
            )
            try:
                self._copy_remote_path(
                    connection,
                    normalized_target,
                    backup_folder,
                    directory=True,
                )
            except Exception:
                try:
                    self._remove_sftp_path(sftp, backup_folder)
                except Exception:
                    pass
                raise
            log(f"已备份服务器文件夹：{target_root} -> {backup_folder}")
            return

        existing_files: list[tuple[str, str]] = []
        for relative in relative_files:
            target_file = posixpath.join(target_root, relative)
            attributes = self._sftp_attributes(sftp, target_file)
            if attributes is None:
                continue
            if not stat.S_ISREG(attributes.st_mode):
                raise WorkflowExecutionError(
                    f"服务器上传目标不是普通文件，无法备份：{target_file}"
                )
            existing_files.append((target_file, relative))
        if not existing_files:
            log("本次文件夹上传不会覆盖服务器文件，跳过备份")
            return

        backup_directory = self._dated_backup_directory(
            connection,
            sftp,
            backup_root,
        )
        target_name = posixpath.basename(posixpath.normpath(target_root)) or "contents"
        snapshot_root = self._next_backup_path(
            sftp,
            backup_directory,
            f"{target_name}_contents",
            "",
        )
        self._ensure_sftp_directory(sftp, snapshot_root)
        try:
            for target_file, relative in existing_files:
                backup_file = posixpath.join(snapshot_root, relative)
                self._ensure_sftp_directory(
                    sftp,
                    posixpath.dirname(backup_file) or snapshot_root,
                )
                self._copy_remote_path(
                    connection,
                    target_file,
                    backup_file,
                    directory=False,
                )
        except Exception:
            try:
                self._remove_sftp_path(sftp, snapshot_root)
            except Exception:
                pass
            raise
        log(
            f"已备份本次会覆盖的服务器文件：{snapshot_root}"
            f"（{len(existing_files)} 个文件）"
        )

    def _dated_backup_directory(
        self,
        connection: Any,
        sftp: Any,
        backup_root: str,
    ) -> str:
        result = connection.run(
            "date +%Y%m%d",
            warn=True,
            hide=True,
            pty=False,
            timeout=self.command_timeout,
        )
        date_value = result.stdout.strip()
        if not result.ok or len(date_value) != 8 or not date_value.isdigit():
            detail = result.stderr.strip() or result.stdout.strip() or "date 命令执行失败"
            raise WorkflowExecutionError(f"无法获取服务器日期：{detail}")
        backup_directory = posixpath.join(backup_root, date_value)
        self._ensure_sftp_directory(sftp, backup_directory)
        return backup_directory

    @classmethod
    def _next_backup_path(
        cls,
        sftp: Any,
        backup_directory: str,
        stem: str,
        extension: str,
    ) -> str:
        for index in range(1, 1_000_000):
            candidate = posixpath.join(
                backup_directory,
                f"{stem}_{index:03d}{extension}",
            )
            if cls._sftp_attributes(sftp, candidate) is None:
                return candidate
        raise WorkflowExecutionError(
            f"服务器备份目录中的同名备份数量过多：{backup_directory}"
        )

    def _copy_remote_path(
        self,
        connection: Any,
        source: str,
        target: str,
        *,
        directory: bool,
    ) -> None:
        option = "-a" if directory else "-p"
        result = connection.run(
            f"cp {option} -- {shlex.quote(source)} {shlex.quote(target)}",
            warn=True,
            hide=True,
            pty=False,
            timeout=self.command_timeout,
        )
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or f"退出码 {result.exited}"
            raise WorkflowExecutionError(f"服务器备份失败：{detail}")

    @staticmethod
    def _ensure_sftp_directory(sftp: Any, directory: str) -> None:
        normalized = posixpath.normpath(directory)
        absolute = normalized.startswith("/")
        current = "/" if absolute else ""
        for part in normalized.split("/"):
            if not part or part == ".":
                continue
            current = posixpath.join(current, part)
            try:
                attributes = sftp.stat(current)
            except OSError:
                sftp.mkdir(current)
                continue
            if not stat.S_ISDIR(attributes.st_mode):
                raise WorkflowExecutionError(f"服务器路径不是目录：{current}")

    def _prepare_remote_upload_directory(
        self,
        connection: Any,
        remote_directory: str,
        create_if_missing: bool,
        log: LogCallback,
    ) -> None:
        self._check_cancelled()
        sftp = connection.sftp()
        attributes = self._sftp_attributes(sftp, remote_directory)
        if attributes is not None:
            if not stat.S_ISDIR(attributes.st_mode):
                raise WorkflowExecutionError(
                    f"服务器上传路径存在，但不是目录：{remote_directory}"
                )
            return
        if not create_if_missing:
            raise WorkflowExecutionError(
                "服务器上传目录不存在，且未开启自动创建目录："
                f"{remote_directory}"
            )
        log(f"服务器上传目录不存在，正在自动创建：{remote_directory}")
        try:
            self._ensure_sftp_directory(sftp, remote_directory)
        except Exception as exc:
            raise WorkflowExecutionError(
                f"自动创建服务器上传目录失败：{remote_directory}（{exc}）"
            ) from exc
        log(f"服务器上传目录创建完成：{remote_directory}")

    @staticmethod
    def _sftp_attributes(sftp: Any, path: str) -> Any | None:
        try:
            return sftp.lstat(path)
        except OSError:
            return None

    @staticmethod
    def _rename_sftp_path(sftp: Any, source: str, target: str) -> None:
        posix_rename = getattr(sftp, "posix_rename", None)
        if callable(posix_rename):
            posix_rename(source, target)
        else:
            sftp.rename(source, target)

    @classmethod
    def _remove_sftp_path(cls, sftp: Any, path: str) -> None:
        attributes = cls._sftp_attributes(sftp, path)
        if attributes is None:
            return
        if stat.S_ISDIR(attributes.st_mode):
            for entry in sftp.listdir_attr(path):
                cls._remove_sftp_path(
                    sftp,
                    posixpath.join(path, entry.filename),
                )
            sftp.rmdir(path)
        else:
            sftp.remove(path)

    @classmethod
    def _activate_uploaded_file(
        cls,
        sftp: Any,
        temporary_path: str,
        target_path: str,
    ) -> _UploadedFileSwap:
        target_attributes = cls._sftp_attributes(sftp, target_path)
        if target_attributes is not None and not stat.S_ISREG(target_attributes.st_mode):
            raise WorkflowExecutionError(
                f"服务器目标路径存在，但不是普通文件：{target_path}"
            )
        backup_path = f"{target_path}.backup-{uuid.uuid4().hex}"
        backup_created = False
        try:
            if target_attributes is not None:
                cls._rename_sftp_path(sftp, target_path, backup_path)
                backup_created = True
            cls._rename_sftp_path(sftp, temporary_path, target_path)
        except Exception as exc:
            rollback_error: Exception | None = None
            backup_exists = cls._sftp_attributes(sftp, backup_path) is not None
            if backup_created or backup_exists:
                try:
                    if cls._sftp_attributes(sftp, target_path) is not None:
                        cls._remove_sftp_path(sftp, target_path)
                    cls._rename_sftp_path(sftp, backup_path, target_path)
                    backup_created = False
                except Exception as restore_exc:
                    rollback_error = restore_exc
            if rollback_error is not None:
                raise WorkflowExecutionError(
                    "服务器文件替换失败，并且无法恢复原文件："
                    f"{rollback_error}"
                ) from exc
            result_detail = (
                "原文件已恢复"
                if target_attributes is not None
                else "目标文件未被替换"
            )
            raise WorkflowExecutionError(
                f"服务器文件替换失败，{result_detail}：{exc}"
            ) from exc
        return _UploadedFileSwap(target_path, backup_path, target_attributes is not None)

    def _activate_uploaded_contents(
        self,
        sftp: Any,
        staging_root: str,
        target_root: str,
        relative_files: list[str],
        relative_directories: list[str],
        upload_id: str,
        log: LogCallback,
    ) -> _UploadedFolderSwap:
        target_attributes = self._sftp_attributes(sftp, target_root)
        if target_attributes is not None and not stat.S_ISDIR(target_attributes.st_mode):
            raise WorkflowExecutionError(
                f"服务器目标路径存在，但不是目录：{target_root}"
            )
        created_target_root = target_attributes is None
        self._ensure_sftp_directory(sftp, target_root)

        for relative in relative_directories:
            self._check_cancelled()
            target_directory = posixpath.join(target_root, relative)
            attributes = self._sftp_attributes(sftp, target_directory)
            if attributes is not None and not stat.S_ISDIR(attributes.st_mode):
                raise WorkflowExecutionError(
                    f"服务器路径不是目录：{target_directory}"
                )
        existing_target_files: set[str] = set()
        for relative in relative_files:
            self._check_cancelled()
            target_file = posixpath.join(target_root, relative)
            attributes = self._sftp_attributes(sftp, target_file)
            if attributes is not None and not stat.S_ISREG(attributes.st_mode):
                raise WorkflowExecutionError(
                    f"服务器目标路径存在，但不是普通文件：{target_file}"
                )
            if attributes is not None:
                existing_target_files.add(target_file)

        backup_root = posixpath.join(
            posixpath.dirname(target_root) or ".",
            f".{posixpath.basename(target_root.rstrip('/')) or 'root'}.backup-{upload_id}",
        )
        created_directories: list[str] = []
        activated: list[tuple[str, str | None, bool]] = []
        ensured_target_directories = {target_root}
        ensured_backup_directories: set[str] = set()
        last_logged_percent = 0
        total_files = len(relative_files)
        try:
            for relative in sorted(
                relative_directories,
                key=lambda value: (value.count("/"), value),
            ):
                self._check_cancelled()
                target_directory = posixpath.join(target_root, relative)
                if self._sftp_attributes(sftp, target_directory) is None:
                    self._ensure_sftp_directory(sftp, target_directory)
                    created_directories.append(target_directory)
                ensured_target_directories.add(target_directory)

            for position, relative in enumerate(relative_files, start=1):
                self._check_cancelled()
                staged_file = posixpath.join(staging_root, relative)
                target_file = posixpath.join(target_root, relative)
                target_parent = posixpath.dirname(target_file) or target_root
                if target_parent not in ensured_target_directories:
                    self._ensure_sftp_directory(sftp, target_parent)
                    ensured_target_directories.add(target_parent)
                backup_file: str | None = None
                if target_file in existing_target_files:
                    backup_file = posixpath.join(backup_root, relative)
                    backup_parent = posixpath.dirname(backup_file) or backup_root
                    if backup_parent not in ensured_backup_directories:
                        self._ensure_sftp_directory(sftp, backup_parent)
                        ensured_backup_directories.add(backup_parent)
                    activated.append((target_file, backup_file, False))
                    self._rename_sftp_path(sftp, target_file, backup_file)
                else:
                    activated.append((target_file, None, False))
                self._rename_sftp_path(sftp, staged_file, target_file)
                activated[-1] = (target_file, backup_file, True)
                percent = 100 if total_files <= 0 else int(position * 100 / total_files)
                if percent == 100 or percent >= last_logged_percent + 10:
                    last_logged_percent = percent
                    log(
                        f"服务器文件更新进度：{percent}% "
                        f"（{position}/{total_files}）"
                    )
        except Exception as exc:
            rollback_errors: list[str] = []
            for target_file, backup_file, installed in reversed(activated):
                try:
                    if installed and self._sftp_attributes(sftp, target_file) is not None:
                        self._remove_sftp_path(sftp, target_file)
                    if (
                        backup_file is not None
                        and self._sftp_attributes(sftp, backup_file) is not None
                    ):
                        self._rename_sftp_path(sftp, backup_file, target_file)
                except Exception as restore_exc:
                    rollback_errors.append(f"{target_file}: {restore_exc}")
            for directory in reversed(created_directories):
                try:
                    sftp.rmdir(directory)
                except Exception:
                    pass
            if rollback_errors:
                raise WorkflowExecutionError(
                    "文件夹上传完成前发生错误，并且部分原文件无法恢复："
                    + "；".join(rollback_errors)
                ) from exc
            try:
                self._remove_sftp_path(sftp, backup_root)
            except Exception:
                pass
            raise WorkflowExecutionError(
                f"文件夹上传完成前发生错误，已恢复原文件：{exc}"
            ) from exc
        finally:
            try:
                self._remove_sftp_path(sftp, staging_root)
            except Exception:
                pass

        return _UploadedFolderSwap(
            target_root=target_root,
            backup_root=backup_root,
            files=tuple(
                (target_file, backup_file)
                for target_file, backup_file, _installed in activated
            ),
            created_directories=tuple(created_directories),
            created_target_root=created_target_root,
        )

    def _rollback_uploads(self, log: LogCallback) -> tuple[int, list[str]]:
        if not self._upload_swaps:
            return 0, []
        log("任务后续步骤失败，开始恢复本次上传前的服务器文件")
        restored_count = 0
        errors: list[str] = []
        for connection_name, swap in reversed(self._upload_swaps):
            connection = self._connections.get(connection_name)
            if connection is None:
                errors.append(f"连接 {connection_name} 已不存在")
                continue
            try:
                sftp = connection.sftp()
                if isinstance(swap, _UploadedFileSwap):
                    self._rollback_uploaded_file(sftp, swap)
                else:
                    self._rollback_uploaded_folder(sftp, swap)
                restored_count += 1
                log(f"已恢复连接 {connection_name} 上的上传内容")
            except Exception as exc:
                errors.append(f"连接 {connection_name}：{exc}")
        self._upload_swaps.clear()
        return restored_count, errors

    @classmethod
    def _rollback_uploaded_file(cls, sftp: Any, swap: _UploadedFileSwap) -> None:
        if swap.had_previous:
            backup_attributes = cls._sftp_attributes(sftp, swap.backup_path)
            if backup_attributes is None or not stat.S_ISREG(backup_attributes.st_mode):
                raise WorkflowExecutionError(
                    f"上传备份不存在或不是普通文件：{swap.backup_path}"
                )
        target_attributes = cls._sftp_attributes(sftp, swap.target_path)
        if target_attributes is not None:
            if not stat.S_ISREG(target_attributes.st_mode):
                raise WorkflowExecutionError(
                    f"上传目标已变成非普通文件，无法安全恢复：{swap.target_path}"
                )
            sftp.remove(swap.target_path)
        if swap.had_previous:
            cls._rename_sftp_path(sftp, swap.backup_path, swap.target_path)

    @classmethod
    def _rollback_uploaded_folder(
        cls,
        sftp: Any,
        swap: _UploadedFolderSwap,
    ) -> None:
        errors: list[str] = []
        for target_file, backup_file in reversed(swap.files):
            try:
                if backup_file is not None:
                    backup_attributes = cls._sftp_attributes(sftp, backup_file)
                    if (
                        backup_attributes is None
                        or not stat.S_ISREG(backup_attributes.st_mode)
                    ):
                        raise WorkflowExecutionError(
                            f"上传备份不存在或不是普通文件：{backup_file}"
                        )
                target_attributes = cls._sftp_attributes(sftp, target_file)
                if target_attributes is not None:
                    if not stat.S_ISREG(target_attributes.st_mode):
                        raise WorkflowExecutionError(
                            f"上传目标已变成非普通文件：{target_file}"
                        )
                    sftp.remove(target_file)
                if backup_file is not None:
                    cls._rename_sftp_path(sftp, backup_file, target_file)
            except Exception as exc:
                errors.append(f"{target_file}：{exc}")
        for directory in reversed(swap.created_directories):
            try:
                sftp.rmdir(directory)
            except OSError:
                pass
        if swap.created_target_root:
            try:
                sftp.rmdir(swap.target_root)
            except OSError:
                pass
        if not errors:
            try:
                cls._remove_sftp_path(sftp, swap.backup_root)
            except Exception:
                pass
        if errors:
            raise WorkflowExecutionError("；".join(errors))

    def _discard_upload_backups(
        self,
        log: LogCallback,
        task_succeeded: bool,
    ) -> None:
        for connection_name, swap in self._upload_swaps:
            connection = self._connections.get(connection_name)
            if connection is None:
                log(f"警告：连接 {connection_name} 已关闭，无法清理上传备份")
                continue
            backup_path = (
                swap.backup_path
                if isinstance(swap, _UploadedFileSwap)
                else swap.backup_root
            )
            try:
                self._remove_sftp_path(connection.sftp(), backup_path)
            except Exception as exc:
                task_state = "任务已成功" if task_succeeded else "任务后续步骤失败"
                log(f"警告：{task_state}，但清理上传临时备份失败：{backup_path}（{exc}）")
        self._upload_swaps.clear()

    def _remote_command(self, step: WorkflowStep, log: LogCallback) -> None:
        name = step.values["CONNECTION_NAME"].strip()
        connection, _parameters = self._connection(name)
        command = step.values["COMMAND"].strip()
        directory = step.values.get("PATH", "").strip() or None
        timeout = self._positive_int(
            step.values.get("TIMEOUT", "300") or "300",
            "执行超时",
        )
        display_directory = directory or "SSH 默认当前目录"
        log(f"在 {name}:{display_directory} 执行：{command}")
        FabricDeployer(
            connect_timeout=self.connect_timeout,
            command_timeout=timeout,
            cancel_event=self._cancel_event,
        )._run_streamed_command(connection, directory, command, log)

    def _remote_script(self, step: WorkflowStep, log: LogCallback) -> None:
        name = step.values["CONNECTION_NAME"].strip()
        connection, _parameters = self._connection(name)
        script_path = self._referenced_file(
            self.script_dir,
            step.values["SCRIPT_FILE"],
            "脚本文件",
        )
        self._ensure_script_saved(script_path)
        if script_path.suffix.lower() not in {".sh", ".bash"}:
            raise WorkflowExecutionError(
                "远程脚本只能使用 .sh 或 .bash 文件："
                f"{script_path.name}"
            )
        directory = step.values.get("PATH", "").strip() or None
        timeout = self._positive_int(
            step.values.get("TIMEOUT", "300") or "300",
            "执行超时",
        )
        display_directory = directory or "SSH 默认当前目录"
        log(f"在 {name}:{display_directory} 执行脚本：{script_path.name}")
        FabricDeployer(
            connect_timeout=self.connect_timeout,
            command_timeout=timeout,
            cancel_event=self._cancel_event,
        )._run_streamed_script(connection, directory, script_path, log)
        delay = self._non_negative_float(step.values.get("DELAY", "0"), "脚本完成后等待")
        if delay:
            log(f"等待 {delay:g} 秒后继续")
            self._wait_cancelable(delay)

    def _wait(self, step: WorkflowStep, log: LogCallback) -> None:
        seconds = self._non_negative_float(step.values["SECONDS"], "等待秒数")
        log(f"等待 {seconds:g} 秒")
        self._wait_cancelable(seconds)

    def _health_check(self, step: WorkflowStep, log: LogCallback) -> None:
        name = step.values["CONNECTION_NAME"].strip()
        connection, _parameters = self._connection(name)
        directory = None
        command = step.values["COMMAND"].strip()
        timeout = self._positive_float(step.values["TIMEOUT"], "最长等待")
        interval = self._positive_float(step.values["INTERVAL"], "检查间隔")
        required_successes = self._positive_int(step.values["SUCCESS_COUNT"], "连续成功次数")
        deadline = time.monotonic() + timeout
        successes = 0
        attempts = 0
        while time.monotonic() < deadline:
            self._check_cancelled()
            attempts += 1
            result = self._run_remote_result(
                connection,
                command,
                directory,
                timeout=max(1, min(self.command_timeout, int(interval) + 1)),
            )
            if result.ok:
                successes += 1
                log(f"健康检查通过（{successes}/{required_successes}）")
                if successes >= required_successes:
                    return
            else:
                successes = 0
                log(f"第 {attempts} 次健康检查未通过")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._wait_cancelable(min(interval, remaining))
        raise WorkflowExecutionError(f"健康检查在 {timeout:g} 秒内未通过")

    def _run_remote(
        self,
        connection: Any,
        command: str,
        directory: str | None,
        log: LogCallback,
    ) -> None:
        result = self._run_remote_result(connection, command, directory)
        for output in (result.stdout, result.stderr):
            for line in output.splitlines():
                if line.strip():
                    log(line.rstrip())
        if not result.ok:
            raise WorkflowExecutionError(f"远程命令退出码：{result.exited}")

    def _run_remote_result(
        self,
        connection: Any,
        command: str,
        directory: str | None,
        timeout: int | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "hide": True,
            "warn": True,
            "pty": False,
            "timeout": timeout or self.command_timeout,
        }
        shell_command = (
            f"cd {shlex.quote(directory)} && {command}"
            if directory
            else command
        )
        return connection.run(
            FabricDeployer.login_shell_command(shell_command),
            **kwargs,
        )

    def _connection(self, name: str) -> tuple[Any, ServerParameters]:
        connection = self._connections.get(name)
        parameters = self._parameters.get(name)
        if connection is None or parameters is None:
            raise WorkflowExecutionError(
                f"连接“{name}”尚未建立，请先增加并执行服务器连接步骤"
            )
        return connection, parameters

    def _close_connections(self) -> None:
        for connection in self._connections.values():
            try:
                connection.close()
            except Exception:
                pass
        self._connections.clear()
        self._parameters.clear()

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise WorkflowExecutionError("任务已由用户停止")

    def _wait_cancelable(self, seconds: float) -> None:
        if self._cancel_event.wait(timeout=seconds):
            raise WorkflowExecutionError("任务已由用户停止")

    @staticmethod
    def _referenced_file(root: Path, value: str, label: str) -> Path:
        reference = value.strip()
        if not reference:
            raise WorkflowExecutionError(f"未填写{label}")
        reference_path = Path(reference)
        if (
            reference_path.is_absolute()
            or reference_path.drive
            or reference_path.name != reference
        ):
            raise WorkflowExecutionError(
                f"{label}只能填写文件名，不能包含目录：{value}"
            )
        path = (root / reference).resolve()
        if path.parent != root or not path.is_file():
            raise WorkflowExecutionError(f"{label}不存在：{reference}")
        return path

    @staticmethod
    def _ensure_script_saved(script_path: Path) -> None:
        draft_path = (
            script_path.parent.parent
            / ".drafts"
            / "script"
            / f"{script_path.name}.draft"
        )
        if draft_path.is_file():
            raise WorkflowExecutionError(
                f"脚本 {script_path.name} 存在未保存的暂存内容；"
                "请先在脚本页面点击保存"
            )

    @staticmethod
    def _directory(value: str, label: str) -> Path:
        path = Path(value.strip()).expanduser().resolve()
        if not path.is_dir():
            raise WorkflowExecutionError(f"{label}不存在：{path}")
        return path

    @staticmethod
    def _positive_int(value: str, label: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise ConfigurationError(f"{label}必须是整数") from exc
        if number <= 0:
            raise ConfigurationError(f"{label}必须大于 0")
        return number

    @staticmethod
    def _positive_float(value: str, label: str) -> float:
        number = WorkflowExecutor._non_negative_float(value, label)
        if number <= 0:
            raise ConfigurationError(f"{label}必须大于 0")
        return number

    @staticmethod
    def _non_negative_float(value: str, label: str) -> float:
        try:
            number = float(value)
        except ValueError as exc:
            raise ConfigurationError(f"{label}必须是数字") from exc
        if not math.isfinite(number):
            raise ConfigurationError(f"{label}必须是有限数字，不能使用 NaN 或 Infinity")
        if number < 0:
            raise ConfigurationError(f"{label}不能小于 0")
        return number

    def _git_process(
        self,
        repository: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        self._check_cancelled()
        environment = os.environ.copy()
        environment["LC_ALL"] = "C.UTF-8"
        environment["LANG"] = "C.UTF-8"
        return subprocess.run(
            ["git", *arguments],
            cwd=str(repository),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _git(self, repository: Path, *arguments: str) -> str:
        result = self._git_process(repository, *arguments)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
            raise WorkflowExecutionError(f"Git 命令失败：{detail}")
        return result.stdout

    def _git_logged(self, repository: Path, log: LogCallback, *arguments: str) -> None:
        result = self._git_process(repository, *arguments)
        self._log_process(result, log)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
            raise WorkflowExecutionError(f"Git 命令失败：{detail}")

    @staticmethod
    def _log_process(result: subprocess.CompletedProcess[str], log: LogCallback) -> None:
        for output in (result.stdout, result.stderr):
            for line in output.splitlines():
                if line.strip():
                    log(line.rstrip())
