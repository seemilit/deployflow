"""SSH upload, restart, and remote health-check services."""

from __future__ import annotations

import codecs
import posixpath
import re
import shlex
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import DeploymentConfig


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]
StatusCallback = Callable[[str], None]


class DeploymentError(RuntimeError):
    """Raised when a remote deployment step fails."""


@dataclass(frozen=True)
class _ArtifactSwap:
    target_path: str
    backup_path: str
    had_previous: bool


class _RemoteLineStream:
    """Incrementally decode remote output without retaining the whole stream."""

    _MAX_PARTIAL_LINE = 8192

    def __init__(self, log: LogCallback):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._log = log

    def feed(self, data: bytes) -> None:
        if data:
            self._buffer += self._decoder.decode(data)
            self._drain(final=False)

    def finish(self) -> None:
        self._buffer += self._decoder.decode(b"", final=True)
        self._drain(final=True)

    def _drain(self, final: bool) -> None:
        while True:
            lf_index = self._buffer.find("\n")
            cr_index = self._buffer.find("\r")
            indexes = [index for index in (lf_index, cr_index) if index >= 0]
            if not indexes:
                break

            end = min(indexes)
            separator_length = 1
            if (
                self._buffer[end] == "\r"
                and end + 1 < len(self._buffer)
                and self._buffer[end + 1] == "\n"
            ):
                separator_length = 2
            self._write_line(self._buffer[:end])
            self._buffer = self._buffer[end + separator_length :]

        while len(self._buffer) > self._MAX_PARTIAL_LINE:
            self._write_line(self._buffer[: self._MAX_PARTIAL_LINE])
            self._buffer = self._buffer[self._MAX_PARTIAL_LINE :]

        if final and self._buffer:
            self._write_line(self._buffer)
            self._buffer = ""

    def _write_line(self, line: str) -> None:
        if line.strip():
            self._log(line.rstrip())


class FabricDeployer:
    _ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
    _SHELLS = {"bash", "sh"}
    _WRAPPERS = {"command", "env", "exec", "nice", "nohup", "setsid", "sudo", "timeout"}

    def __init__(
        self,
        connect_timeout: int = 20,
        command_timeout: int = 300,
        cancel_event: threading.Event | None = None,
    ):
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._cancel_event = cancel_event or threading.Event()

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise DeploymentError("任务已由用户停止")

    def _wait_cancelable(self, seconds: float) -> None:
        if self._cancel_event.wait(timeout=seconds):
            raise DeploymentError("任务已由用户停止")

    def deploy(
        self,
        config: DeploymentConfig,
        artifact: Path,
        progress: ProgressCallback,
        status: StatusCallback,
        log: LogCallback,
    ) -> bool:
        self._check_cancelled()
        try:
            from fabric import Config, Connection
            from paramiko import RejectPolicy, SSHConfig
        except ImportError as exc:
            raise DeploymentError("缺少 Fabric，请先执行：pip install fabric") from exc

        connect_kwargs: dict[str, Any] = {"allow_agent": False, "look_for_keys": False}
        if config.auth_method == "PASSWORD":
            connect_kwargs["allow_agent"] = False
            connect_kwargs["look_for_keys"] = False
            if config.password:
                connect_kwargs["password"] = config.password
        elif config.auth_method == "KEY":
            if config.key_filename:
                connect_kwargs["key_filename"] = str(config.key_filename)
        else:
            if config.password:
                connect_kwargs["password"] = config.password
                connect_kwargs["allow_agent"] = False
                connect_kwargs["look_for_keys"] = False
            if config.key_filename:
                connect_kwargs["key_filename"] = str(config.key_filename)

        connection = Connection(
            host=config.ip_address,
            port=config.port,
            user=config.username,
            connect_kwargs=connect_kwargs,
            connect_timeout=self.connect_timeout,
            config=Config(ssh_config=SSHConfig(), lazy=True),
        )
        known_hosts_path = (
            config.file_path.parent.parent / "known_hosts"
        ).resolve()
        try:
            if known_hosts_path.is_file():
                connection.client.load_host_keys(str(known_hosts_path))
            connection.client.set_missing_host_key_policy(RejectPolicy())
        except (OSError, ValueError) as exc:
            raise DeploymentError(f"无法读取 SSH 主机密钥记录：{exc}") from exc

        remote_file = posixpath.join(config.source_code_path, artifact.name)
        remote_temporary = posixpath.join(
            config.source_code_path,
            f".{artifact.name}.upload-{uuid.uuid4().hex}.tmp",
        )
        swap: _ArtifactSwap | None = None

        try:
            log(f"连接服务器：{config.target}")
            try:
                connection.open()
                self._check_cancelled()
            except Exception as exc:
                connection_error = str(exc).lower()
                if "known_hosts" in connection_error or "host key" in connection_error:
                    raise DeploymentError(
                        "服务器主机密钥尚未信任或已发生变化。请先在“参数”页面"
                        "点击“连接”，核对并保存服务器指纹后再部署"
                    ) from exc
                raise
            transport = connection.client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
            self._ensure_remote_deployment_path(connection, config, log)
            baseline_instance_id: str | None = None
            if config.health_check_instance_command:
                status("正在记录部署前的服务实例……")
                with connection.cd(config.source_code_path):
                    baseline_instance_id = self._read_health_instance(
                        connection,
                        config.health_check_instance_command,
                        self._preflight_timeout,
                    )
                log(f"部署前服务实例标识：{baseline_instance_id}")
            log(f"进入服务器目录：{config.source_code_path}")
            status("正在上传……")
            try:
                self._check_cancelled()
                self._upload_with_progress(
                    connection,
                    artifact,
                    remote_temporary,
                    progress,
                    log,
                )
                swap = self._activate_uploaded_artifact(
                    connection,
                    remote_temporary,
                    remote_file,
                    log,
                )
            except Exception:
                self._remove_remote_file(connection, remote_temporary, log)
                raise

            try:
                with connection.cd(config.source_code_path):
                    self._run_restart_steps(connection, config, status, log)

                    if not config.health_check_command:
                        log("未配置 HEALTH_CHECK_COMMAND，无法确认服务是否启动成功")
                        return False

                    status("正在等待服务启动……")
                    self._wait_for_health(
                        connection,
                        config,
                        log,
                        baseline_instance_id=baseline_instance_id,
                    )
                    status("服务启动成功")
                    return True
            except Exception as exc:
                original_detail = str(exc) or exc.__class__.__name__
                raise DeploymentError(
                    "JAR 已上传成功，后续重启或健康检查失败；"
                    f"已上传内容保持不变：{original_detail}"
                ) from exc
        except DeploymentError:
            raise
        except Exception as exc:
            raise DeploymentError(f"SSH 部署失败：{exc}") from exc
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _activate_uploaded_artifact(
        self,
        connection: Any,
        temporary_path: str,
        target_path: str,
        log: LogCallback,
    ) -> _ArtifactSwap:
        backup_path = f"{target_path}.backup"
        backup_temporary = f"{backup_path}.tmp-{uuid.uuid4().hex}"
        had_previous = self._remote_regular_file_exists(connection, target_path)

        if had_previous:
            log(f"备份当前制品：{target_path} -> {backup_path}")
            try:
                self._run_remote_file_operation(
                    connection,
                    f"cp -p {shlex.quote(target_path)} {shlex.quote(backup_temporary)}",
                    "无法创建服务器制品备份",
                )
                self._run_remote_file_operation(
                    connection,
                    f"mv -f {shlex.quote(backup_temporary)} {shlex.quote(backup_path)}",
                    "无法保存服务器制品备份",
                )
            finally:
                self._remove_remote_file(connection, backup_temporary, log)

        self._run_remote_file_operation(
            connection,
            f"mv -f {shlex.quote(temporary_path)} {shlex.quote(target_path)}",
            "无法原子替换服务器制品",
        )
        log(f"服务器制品已原子替换：{target_path}")
        if had_previous:
            log(f"上一版本备份保留在：{backup_path}")
        return _ArtifactSwap(target_path, backup_path, had_previous)

    def _attempt_rollback(
        self,
        connection: Any,
        config: DeploymentConfig,
        swap: _ArtifactSwap,
        status: StatusCallback,
        log: LogCallback,
    ) -> str:
        status("部署失败，正在尝试回滚……")
        log("重启或健康检查失败，开始回滚服务器制品")

        if swap.had_previous:
            rollback_temporary = (
                f"{swap.target_path}.rollback-{uuid.uuid4().hex}.tmp"
            )
            try:
                if not self._remote_regular_file_exists(connection, swap.backup_path):
                    raise DeploymentError(
                        f"服务器备份文件不存在：{swap.backup_path}"
                    )
                self._run_remote_file_operation(
                    connection,
                    f"cp -p {shlex.quote(swap.backup_path)} "
                    f"{shlex.quote(rollback_temporary)}",
                    "无法复制服务器备份制品",
                )
                self._run_remote_file_operation(
                    connection,
                    f"mv -f {shlex.quote(rollback_temporary)} "
                    f"{shlex.quote(swap.target_path)}",
                    "无法恢复服务器备份制品",
                )
                artifact_detail = f"已恢复上传目录中的上一版本：{swap.target_path}"
            except Exception as rollback_exc:
                detail = (
                    "自动恢复服务器制品失败，请立即人工检查服务器："
                    f"{rollback_exc}"
                )
                log(detail)
                return detail
            finally:
                self._remove_remote_file(connection, rollback_temporary, log)
        else:
            failed_path = f"{swap.target_path}.failed-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            try:
                moved_failed_artifact = False
                if self._remote_regular_file_exists(connection, swap.target_path):
                    self._run_remote_file_operation(
                        connection,
                        f"mv -f {shlex.quote(swap.target_path)} {shlex.quote(failed_path)}",
                        "无法移走本次部署的制品",
                    )
                    moved_failed_artifact = True
                if moved_failed_artifact:
                    artifact_detail = (
                        "服务器此前没有同名制品，已撤下本次制品；"
                        f"失败文件保留在 {failed_path}"
                    )
                else:
                    artifact_detail = (
                        "服务器此前没有可回滚版本，且上传路径下已找不到本次制品；"
                        "它可能已被执行步骤移动"
                    )
            except Exception as rollback_exc:
                artifact_detail = (
                    "服务器此前没有可回滚版本，且撤下本次制品失败："
                    f"{rollback_exc}"
                )

        log(artifact_detail)
        rollback_command = getattr(config, "rollback_command", None)
        if not rollback_command:
            detail = (
                f"{artifact_detail}；未配置 ROLLBACK_COMMAND，为避免重复执行有副作用的"
                "部署步骤，未自动重启旧版本，请人工确认服务状态"
            )
            log(detail)
            return detail

        try:
            rollback_directory = self._configured_remote_directory(
                config,
                getattr(config, "rollback_path", None),
            )
            rollback_baseline_instance_id: str | None = None
            if config.health_check_instance_command:
                with connection.cd(config.source_code_path):
                    rollback_baseline_instance_id = self._read_health_instance(
                        connection,
                        config.health_check_instance_command,
                        self._preflight_timeout,
                    )
                log(
                    "执行回滚前的服务实例标识："
                    f"{rollback_baseline_instance_id}"
                )
            status("正在执行回滚命令……")
            log(f"回滚命令执行目录：{rollback_directory}")
            log(f"执行回滚命令：{rollback_command}")
            with connection.cd(rollback_directory):
                self._run_checked(connection, rollback_command, log)
            if config.health_check_command:
                status("正在确认回滚版本启动状态……")
                with connection.cd(config.source_code_path):
                    self._wait_for_health(
                        connection,
                        config,
                        log,
                        verify_expected_text=False,
                        baseline_instance_id=rollback_baseline_instance_id,
                    )
            detail = f"{artifact_detail}；回滚命令已执行"
            if config.health_check_command:
                detail += "并通过健康检查"
            else:
                detail += "，但未配置健康检查，无法确认服务状态"
            log(detail)
            return detail
        except Exception as rollback_exc:
            detail = (
                f"{artifact_detail}；回滚命令执行或检查失败，请立即人工检查服务器："
                f"{rollback_exc}"
            )
            log(detail)
            return detail

    def _run_restart_steps(
        self,
        connection: Any,
        config: DeploymentConfig,
        status: StatusCallback,
        log: LogCallback,
    ) -> None:
        total_steps = len(config.restart_steps)
        for index, step in enumerate(config.restart_steps, start=1):
            self._check_cancelled()
            status(f"正在执行重启命令 {index}/{total_steps}……")
            remote_directory = self._step_remote_directory(config, step)
            log(f"步骤 {index}/{total_steps} 执行目录：{remote_directory}")
            with connection.cd(remote_directory):
                if step.command is not None:
                    log(f"执行重启命令 {index}/{total_steps}：{step.command}")
                    self._run_streamed_command(
                        connection,
                        remote_directory,
                        step.command,
                        log,
                    )
                elif step.local_script_path is not None:
                    log(
                        f"执行本地流式脚本 {index}/{total_steps}："
                        f"{step.local_script_path}"
                    )
                    self._run_streamed_script(
                        connection,
                        remote_directory,
                        step.local_script_path,
                        log,
                    )
                else:
                    raise DeploymentError(f"第 {index} 个重启步骤没有可执行内容")

            if index < total_steps and step.delay_after > 0:
                delay_text = f"{step.delay_after:g}"
                status(f"等待 {delay_text} 秒后执行下一条命令……")
                log(f"命令执行成功，等待 {delay_text} 秒后继续")
                self._wait_cancelable(step.delay_after)

    def _ensure_remote_deployment_path(
        self,
        connection: Any,
        config: DeploymentConfig,
        log: LogCallback,
    ) -> None:
        directory_result = connection.run(
            f"test -d {shlex.quote(config.source_code_path)}",
            warn=True,
            hide=True,
            timeout=self._preflight_timeout,
        )
        if not directory_result.ok:
            raise DeploymentError(
                f"服务器部署目录不存在或无法访问：{config.source_code_path}"
            )

        for index, step in enumerate(config.restart_steps, start=1):
            remote_directory = self._step_remote_directory(config, step)
            directory_result = connection.run(
                f"test -d {shlex.quote(remote_directory)}",
                warn=True,
                hide=True,
                timeout=self._preflight_timeout,
            )
            if not directory_result.ok:
                raise DeploymentError(
                    f"第 {index} 个执行步骤的服务器目录不存在：{remote_directory}"
                )
            with connection.cd(remote_directory):
                if step.command is not None:
                    log(f"检查重启命令 {index}：{step.command}")
                    self._ensure_remote_command(connection, step.command)
                elif step.local_script_path is not None:
                    if not step.local_script_path.is_file():
                        raise DeploymentError(
                            f"本地流式脚本不存在：{step.local_script_path}"
                        )
                    log(f"检查本地流式脚本 {index}：{step.local_script_path}")
                    self._ensure_remote_command(connection, "bash")

        rollback_command = getattr(config, "rollback_command", None)
        if rollback_command:
            rollback_directory = self._configured_remote_directory(
                config,
                getattr(config, "rollback_path", None),
            )
            directory_result = connection.run(
                f"test -d {shlex.quote(rollback_directory)}",
                warn=True,
                hide=True,
                timeout=self._preflight_timeout,
            )
            if not directory_result.ok:
                raise DeploymentError(
                    f"回滚命令的服务器目录不存在：{rollback_directory}"
                )
            with connection.cd(rollback_directory):
                log(f"检查回滚命令：{rollback_command}")
                self._ensure_remote_command(connection, rollback_command)

        log("执行命令和回滚命令检查通过")

    @property
    def _preflight_timeout(self) -> int:
        return max(1, min(30, self.connect_timeout))

    @staticmethod
    def _step_remote_directory(config: DeploymentConfig, step: Any) -> str:
        return FabricDeployer._configured_remote_directory(
            config,
            step.working_directory,
        )

    @staticmethod
    def _configured_remote_directory(
        config: DeploymentConfig,
        configured_value: str | None,
    ) -> str:
        configured_path = (configured_value or "").strip()
        if not configured_path:
            return posixpath.normpath(config.source_code_path)
        if posixpath.isabs(configured_path):
            return posixpath.normpath(configured_path)
        return posixpath.normpath(
            posixpath.join(config.source_code_path, configured_path)
        )

    def _ensure_remote_command(
        self,
        connection: Any,
        command: str,
        *,
        _depth: int = 0,
    ) -> None:
        if _depth > 3:
            raise DeploymentError("重启命令包装层级过深，无法安全检查")
        command_segments = self._split_shell_commands(command)
        if len(command_segments) > 1:
            for segment in command_segments:
                self._ensure_remote_command(
                    connection,
                    segment,
                    _depth=_depth + 1,
                )
            return
        try:
            command_parts = shlex.split(command, posix=True)
        except ValueError as exc:
            raise DeploymentError(f"重启命令格式错误：{exc}") from exc
        if not command_parts:
            raise DeploymentError("重启命令不能为空")

        wrappers, invocation = self._unwrap_command(command_parts)
        for wrapper in wrappers:
            if posixpath.basename(wrapper) not in {"command", "exec"}:
                self._ensure_remote_executable(connection, wrapper, "命令包装器")
        if not invocation:
            raise DeploymentError("重启命令没有实际要执行的程序")

        executable = invocation[0]
        executable_name = posixpath.basename(executable)
        if executable_name in self._SHELLS:
            self._ensure_remote_executable(connection, executable, "Shell")
            shell_script, inline_command = self._shell_target(invocation)
            if inline_command is not None:
                self._ensure_remote_command(
                    connection,
                    inline_command,
                    _depth=_depth + 1,
                )
            elif shell_script is not None:
                self._ensure_remote_script(connection, shell_script, executable=False)
            return

        if "/" in executable:
            self._ensure_remote_script(connection, executable, executable=True)
        else:
            self._ensure_remote_executable(connection, executable, "重启命令")

    @staticmethod
    def _split_shell_commands(command: str) -> list[str]:
        """Split common top-level shell operators while respecting quotes."""

        segments: list[str] = []
        start = 0
        index = 0
        single_quoted = False
        double_quoted = False
        escaped = False
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
            if single_quoted or double_quoted:
                index += 1
                continue

            operator_length = 0
            if command.startswith(("&&", "||", "|&"), index):
                operator_length = 2
            elif character in {";", "|", "\n", "\r"}:
                operator_length = 1
            elif character == "&":
                previous = command[index - 1] if index > 0 else ""
                following = command[index + 1] if index + 1 < len(command) else ""
                if previous != ">" and following != ">":
                    operator_length = 1
            if operator_length:
                segment = command[start:index].strip()
                if segment:
                    segments.append(segment)
                index += operator_length
                start = index
                continue
            index += 1

        final_segment = command[start:].strip()
        if final_segment:
            segments.append(final_segment)
        return segments or [command]

    def _unwrap_command(self, parts: list[str]) -> tuple[list[str], list[str]]:
        wrappers: list[str] = []
        index = 0
        while index < len(parts) and self._is_assignment(parts[index]):
            index += 1

        while index < len(parts):
            wrapper = parts[index]
            name = posixpath.basename(wrapper)
            if name not in self._WRAPPERS:
                break
            wrappers.append(wrapper)
            index += 1

            if name == "env":
                index = self._skip_env_options(parts, index)
            elif name == "sudo":
                index = self._skip_options(
                    parts,
                    index,
                    {
                        "-C", "-D", "-g", "-h", "-p", "-R", "-r", "-T", "-t", "-U", "-u",
                        "--chdir", "--chroot", "--close-from", "--command-timeout",
                        "--group", "--host", "--other-user", "--prompt", "--role", "--type", "--user",
                    },
                )
            elif name == "nice":
                index = self._skip_options(parts, index, {"-n", "--adjustment"})
            elif name == "timeout":
                index = self._skip_options(
                    parts,
                    index,
                    {"-k", "-s", "--kill-after", "--signal"},
                )
                if index < len(parts):
                    index += 1  # duration
            elif name == "exec":
                index = self._skip_options(parts, index, {"-a"})
            else:
                index = self._skip_options(parts, index, set())

            while index < len(parts) and self._is_assignment(parts[index]):
                index += 1

        return wrappers, parts[index:]

    def _skip_env_options(self, parts: list[str], index: int) -> int:
        index = self._skip_options(
            parts,
            index,
            {"-C", "-S", "-u", "--chdir", "--split-string", "--unset"},
        )
        while index < len(parts) and self._is_assignment(parts[index]):
            index += 1
        return index

    @staticmethod
    def _skip_options(
        parts: list[str],
        index: int,
        options_with_value: set[str],
    ) -> int:
        while index < len(parts):
            option = parts[index]
            if option == "--":
                return index + 1
            if not option.startswith("-") or option == "-":
                return index

            option_name = option.split("=", 1)[0]
            index += 1
            if (
                option_name in options_with_value
                and "=" not in option
                and index < len(parts)
            ):
                index += 1
        return index

    @staticmethod
    def _shell_target(parts: list[str]) -> tuple[str | None, str | None]:
        index = 1
        while index < len(parts):
            token = parts[index]
            if token == "--":
                index += 1
                break
            if token in {"-c", "+c"}:
                return None, parts[index + 1] if index + 1 < len(parts) else ""
            if token in {"-O", "+O", "-o", "+o", "--init-file", "--rcfile"}:
                index += 2
                continue
            if token.startswith(("-", "+")) and not token.startswith("--"):
                flags = token[1:]
                if "c" in flags:
                    return None, parts[index + 1] if index + 1 < len(parts) else ""
                index += 2 if "o" in flags else 1
                continue
            if token.startswith("--"):
                index += 1
                continue
            break
        return (parts[index], None) if index < len(parts) else (None, None)

    @classmethod
    def _is_assignment(cls, value: str) -> bool:
        return bool(cls._ASSIGNMENT_PATTERN.match(value))

    def _ensure_remote_executable(
        self,
        connection: Any,
        executable: str,
        label: str,
    ) -> None:
        if "/" in executable:
            result = connection.run(
                f"test -x {shlex.quote(executable)}",
                warn=True,
                hide=True,
                timeout=self._preflight_timeout,
            )
        else:
            result = connection.run(
                f"command -v {shlex.quote(executable)}",
                warn=True,
                hide=True,
                timeout=self._preflight_timeout,
            )
        if not result.ok:
            raise DeploymentError(f"服务器上找不到可执行的{label}：{executable}")

    def _ensure_remote_script(
        self,
        connection: Any,
        script_path: str,
        *,
        executable: bool,
    ) -> None:
        if any(character in script_path for character in "$*?[{~"):
            return
        quoted_script = shlex.quote(script_path)
        file_result = connection.run(
            f"test -f {quoted_script}",
            warn=True,
            hide=True,
            timeout=self._preflight_timeout,
        )
        if not file_result.ok:
            raise DeploymentError(f"重启命令引用的脚本不存在：{script_path}")
        if executable:
            executable_result = connection.run(
                f"test -x {quoted_script}",
                warn=True,
                hide=True,
                timeout=self._preflight_timeout,
            )
            if not executable_result.ok:
                raise DeploymentError(
                    f"重启脚本没有执行权限：{script_path}，请先执行 chmod +x"
                )

    def _wait_for_health(
        self,
        connection: Any,
        config: DeploymentConfig,
        log: LogCallback,
        *,
        verify_expected_text: bool = True,
        baseline_instance_id: str | None = None,
    ) -> None:
        command = config.health_check_command
        if not command:
            return

        expected_text = (
            getattr(config, "health_check_expected_text", None)
            if verify_expected_text
            else None
        )
        required_successes = max(
            2,
            int(getattr(config, "health_check_success_count", 2)),
        )
        deadline = time.monotonic() + config.health_check_timeout
        attempt = 0
        consecutive_successes = 0
        stable_instance_id: str | None = None
        last_detail = ""
        log(
            "开始健康检查："
            f"最长等待 {config.health_check_timeout} 秒，"
            f"每 {config.health_check_interval:g} 秒检查一次，"
            f"需要连续通过 {required_successes} 次"
        )
        if expected_text:
            log(f"健康检查结果还必须包含：{expected_text}")
        instance_command = getattr(config, "health_check_instance_command", None)
        if instance_command:
            if baseline_instance_id is None:
                raise DeploymentError("缺少部署前服务实例标识，无法确认本次重启")
            log("健康检查还会确认服务实例已变化并保持稳定")
        else:
            log("未配置 HEALTH_CHECK_INSTANCE_COMMAND，无法彻底排除旧进程误判")
        while True:
            self._check_cancelled()
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            command_timeout = max(1, min(15, int(remaining)))
            try:
                result = connection.run(
                    command,
                    warn=True,
                    hide=True,
                    timeout=command_timeout,
                )
            except Exception as exc:
                consecutive_successes = 0
                last_detail = str(exc)
            else:
                output = "\n".join(
                    value for value in (result.stdout, result.stderr) if value
                )
                contains_expected = not expected_text or expected_text in output
                health_passed = result.ok and contains_expected
                current_instance_id: str | None = None
                instance_failure_detail: str | None = None
                if health_passed and instance_command:
                    try:
                        current_instance_id = self._read_health_instance(
                            connection,
                            instance_command,
                            command_timeout,
                        )
                    except DeploymentError as exc:
                        health_passed = False
                        instance_failure_detail = str(exc)
                    else:
                        if current_instance_id == baseline_instance_id:
                            health_passed = False
                            instance_failure_detail = (
                                "健康接口已通过，但仍是部署前的服务实例"
                            )
                        elif (
                            stable_instance_id is not None
                            and current_instance_id != stable_instance_id
                        ):
                            log("服务实例在确认期间再次变化，重新累计连续成功次数")
                            consecutive_successes = 0
                        stable_instance_id = current_instance_id

                if health_passed:
                    consecutive_successes += 1
                    last_detail = (
                        f"已连续通过 {consecutive_successes}/"
                        f"{required_successes} 次"
                    )
                    log(
                        f"第 {attempt} 次健康检查通过，"
                        f"连续通过 {consecutive_successes}/{required_successes} 次"
                        + (
                            f"，实例 {current_instance_id}"
                            if current_instance_id is not None
                            else ""
                        )
                    )
                    if consecutive_successes >= required_successes:
                        self._write_remote_output(result.stdout, log)
                        self._write_remote_output(result.stderr, log)
                        log("健康检查已稳定通过，服务启动成功")
                        return
                else:
                    consecutive_successes = 0
                    stable_instance_id = None
                    if instance_failure_detail is not None:
                        last_detail = instance_failure_detail
                    elif result.ok and not contains_expected:
                        last_detail = f"输出中未找到期望内容：{expected_text}"
                    else:
                        last_detail = (
                            result.stderr.strip()
                            or result.stdout.strip()
                            or f"退出码 {result.exited}"
                        )

            if consecutive_successes == 0:
                log(f"第 {attempt} 次健康检查未通过，继续等待……")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._wait_cancelable(min(config.health_check_interval, remaining))

        detail = f"；最后结果：{last_detail}" if last_detail else ""
        raise DeploymentError(
            f"等待服务启动超时（{config.health_check_timeout} 秒）{detail}"
        )

    @staticmethod
    def _read_health_instance(
        connection: Any,
        command: str,
        timeout: int,
    ) -> str:
        try:
            result = connection.run(
                command,
                warn=True,
                hide=True,
                timeout=max(1, timeout),
            )
        except Exception as exc:
            raise DeploymentError(f"读取服务实例标识失败：{exc}") from exc
        instance_id = result.stdout.strip()
        if not result.ok:
            detail = result.stderr.strip() or f"退出码 {result.exited}"
            raise DeploymentError(f"读取服务实例标识失败：{detail}")
        if not instance_id:
            raise DeploymentError("服务实例标识命令没有输出内容")
        return instance_id

    def _run_streamed_command(
        self,
        connection: Any,
        remote_directory: str | None,
        command: str,
        log: LogCallback,
    ) -> None:
        transport = connection.client.get_transport()
        if transport is None or not transport.is_active():
            raise DeploymentError("SSH 连接已断开，无法执行远程命令")

        channel = transport.open_session(timeout=self.connect_timeout)
        stdout_stream = _RemoteLineStream(log)
        stderr_stream = _RemoteLineStream(log)
        deadline = time.monotonic() + self.command_timeout
        shell_command = (
            f"cd {shlex.quote(remote_directory)} && {command}"
            if remote_directory
            else command
        )
        remote_command = self.login_shell_command(shell_command)

        try:
            channel.exec_command(remote_command)
            channel.settimeout(0.0)
            while True:
                self._check_cancelled()
                activity = False
                while channel.recv_ready():
                    data = channel.recv(65536)
                    if not data:
                        break
                    stdout_stream.feed(data)
                    activity = True
                while channel.recv_stderr_ready():
                    data = channel.recv_stderr(65536)
                    if not data:
                        break
                    stderr_stream.feed(data)
                    activity = True
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"远程命令执行超过 {self.command_timeout} 秒，已停止"
                    )
                if not activity:
                    self._wait_cancelable(0.02)
            exit_status = channel.recv_exit_status()
        except DeploymentError:
            raise
        except Exception as exc:
            raise DeploymentError(f"远程命令执行失败：{exc}") from exc
        finally:
            stdout_stream.finish()
            stderr_stream.finish()
            channel.close()

        if exit_status != 0:
            raise DeploymentError(f"远程命令执行失败，退出码：{exit_status}")

    @staticmethod
    def login_shell_command(command: str) -> str:
        """Wrap a command so it receives login and interactive bash settings."""
        environment_command = (
            "if [ -f ~/.bashrc ]; then . ~/.bashrc; fi; "
            f"{command}"
        )
        return f"bash -lc {shlex.quote(environment_command)}"

    def _run_streamed_script(
        self,
        connection: Any,
        remote_directory: str | None,
        script_path: Path,
        log: LogCallback,
    ) -> None:
        try:
            script_content = script_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise DeploymentError(f"无法读取本地流式脚本：{exc}") from exc

        normalized_content = script_content.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized_content.endswith("\n"):
            normalized_content += "\n"
        payload = normalized_content.encode("utf-8")

        transport = connection.client.get_transport()
        if transport is None or not transport.is_active():
            raise DeploymentError("SSH 连接已断开，无法执行本地流式脚本")

        channel = transport.open_session(timeout=self.connect_timeout)
        stdout_stream = _RemoteLineStream(log)
        stderr_stream = _RemoteLineStream(log)
        deadline = time.monotonic() + self.command_timeout
        shell_command = (
            f"cd {shlex.quote(remote_directory)} && bash -s"
            if remote_directory
            else "bash -s"
        )
        remote_command = self.login_shell_command(shell_command)
        sent = 0
        write_closed = False

        try:
            channel.exec_command(remote_command)
            channel.settimeout(0.0)

            while True:
                self._check_cancelled()
                activity = False
                if sent < len(payload):
                    try:
                        sent_now = channel.send(payload[sent : sent + 65536])
                    except (socket.timeout, BlockingIOError):
                        sent_now = 0
                    if sent_now > 0:
                        sent += sent_now
                        activity = True
                    elif channel.closed:
                        raise DeploymentError("远程通道在脚本发送完成前已关闭")
                elif not write_closed:
                    channel.shutdown_write()
                    write_closed = True
                    activity = True

                while channel.recv_ready():
                    data = channel.recv(65536)
                    if not data:
                        break
                    stdout_stream.feed(data)
                    activity = True
                while channel.recv_stderr_ready():
                    data = channel.recv_stderr(65536)
                    if not data:
                        break
                    stderr_stream.feed(data)
                    activity = True

                if (
                    write_closed
                    and channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"本地流式脚本执行超时：{script_path.name}"
                    )
                if not activity:
                    self._wait_cancelable(0.02)

            exit_status = channel.recv_exit_status()
        except DeploymentError:
            raise
        except Exception as exc:
            raise DeploymentError(f"本地流式脚本执行失败：{exc}") from exc
        finally:
            stdout_stream.finish()
            stderr_stream.finish()
            channel.close()

        if exit_status != 0:
            raise DeploymentError(
                f"本地流式脚本执行失败，退出码：{exit_status}"
            )

    def _upload_with_progress(
        self,
        connection: Any,
        artifact: Path,
        remote_file: str,
        progress: ProgressCallback,
        log: LogCallback,
    ) -> None:
        total_size = artifact.stat().st_size
        progress(0, total_size)
        log(f"开始上传临时文件：{artifact.name} -> {remote_file}")
        last_percent = -1
        last_logged_percent = -5

        def report_progress(transferred: int, total: int) -> None:
            nonlocal last_percent, last_logged_percent
            self._check_cancelled()
            percent = 100 if total <= 0 else int(transferred * 100 / total)
            if percent != last_percent:
                last_percent = percent
                progress(transferred, total)
            if percent == 100 or percent >= last_logged_percent + 5:
                last_logged_percent = percent
                log(f"上传进度：{percent}%")

        try:
            sftp = connection.sftp()
            sftp.get_channel().settimeout(self.command_timeout)
            sftp.put(
                str(artifact),
                remote_file,
                callback=report_progress,
                confirm=True,
            )
            remote_size = int(sftp.stat(remote_file).st_size)
            if remote_size != total_size:
                raise DeploymentError(
                    "上传后的文件大小校验失败："
                    f"本地 {total_size} 字节，服务器 {remote_size} 字节"
                )
            self._check_cancelled()
            progress(total_size, total_size)
        except DeploymentError:
            raise
        except Exception as exc:
            raise DeploymentError(f"文件上传失败：{exc}") from exc

        log(f"临时文件上传完成并通过大小校验，共 {total_size} 字节")

    def _remote_regular_file_exists(self, connection: Any, path: str) -> bool:
        exists_result = connection.run(
            f"test -e {shlex.quote(path)}",
            warn=True,
            hide=True,
            timeout=self._preflight_timeout,
        )
        if not exists_result.ok:
            return False
        file_result = connection.run(
            f"test -f {shlex.quote(path)}",
            warn=True,
            hide=True,
            timeout=self._preflight_timeout,
        )
        if not file_result.ok:
            raise DeploymentError(f"服务器目标路径存在，但不是普通文件：{path}")
        return True

    def _run_remote_file_operation(
        self,
        connection: Any,
        command: str,
        error_message: str,
    ) -> None:
        result = connection.run(
            command,
            warn=True,
            hide=True,
            timeout=self.command_timeout,
        )
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or f"退出码 {result.exited}"
            raise DeploymentError(f"{error_message}：{detail}")

    @staticmethod
    def _remove_remote_file(
        connection: Any,
        remote_path: str,
        log: LogCallback,
    ) -> None:
        try:
            connection.sftp().remove(remote_path)
        except OSError:
            return
        except Exception as exc:
            log(f"清理服务器临时文件失败：{remote_path}（{exc}）")

    def _run_checked(
        self,
        connection: Any,
        command: str,
        log: LogCallback,
    ) -> None:
        result = connection.run(
            command,
            warn=True,
            hide=True,
            timeout=self.command_timeout,
        )
        self._write_remote_output(result.stdout, log)
        self._write_remote_output(result.stderr, log)
        if not result.ok:
            raise DeploymentError(f"重启命令执行失败，退出码：{result.exited}")

    @staticmethod
    def _write_remote_output(output: str | None, log: LogCallback) -> None:
        if not output:
            return
        for line in output.splitlines():
            if line.strip():
                log(line.rstrip())
