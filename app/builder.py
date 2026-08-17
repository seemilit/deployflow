"""Maven build and artifact discovery services."""

from __future__ import annotations

import locale
import os
import queue
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

from config import DeploymentConfig


LogCallback = Callable[[str], None]


class BuildError(RuntimeError):
    """Raised when Maven packaging or artifact discovery fails."""


class MavenBuilder:
    def build(
        self,
        config: DeploymentConfig,
        log: LogCallback,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        if cancel_event is not None and cancel_event.is_set():
            raise BuildError("任务已由用户停止")
        log(f"开始打包：{config.project_path}")
        process = self._start_maven(config.project_path)
        return_code = self._wait_for_build(
            process,
            timeout=config.build_timeout,
            log=log,
            cancel_event=cancel_event,
        )
        if return_code != 0:
            raise BuildError(f"Maven 打包失败，退出码：{return_code}")

        artifact = self._find_artifact(config)
        if config.jar_file is None:
            log(f"自动识别 JAR：{artifact.name}")
        log(f"打包完成：{artifact}")
        return artifact

    @staticmethod
    def _wait_for_build(
        process: subprocess.Popen[str],
        timeout: int,
        log: LogCallback,
        cancel_event: threading.Event | None = None,
    ) -> int:
        output_queue: queue.Queue[str | None] = queue.Queue(maxsize=1024)

        def read_output() -> None:
            try:
                if process.stdout is not None:
                    for raw_line in process.stdout:
                        output_queue.put(raw_line.rstrip())
            finally:
                output_queue.put(None)

        threading.Thread(
            target=read_output,
            name="maven-output-reader",
            daemon=True,
        ).start()

        deadline = time.monotonic() + timeout
        output_finished = False
        while not output_finished:
            if cancel_event is not None and cancel_event.is_set():
                MavenBuilder._terminate_process_tree(process)
                raise BuildError("任务已由用户停止，Maven 打包进程已终止")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                MavenBuilder._terminate_process_tree(process)
                raise BuildError(f"Maven 打包超时（{timeout} 秒），已停止本次部署")
            try:
                line = output_queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                output_finished = True
            elif line:
                log(line)

        remaining = deadline - time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            MavenBuilder._terminate_process_tree(process)
            raise BuildError("任务已由用户停止，Maven 打包进程已终止")
        if remaining <= 0:
            MavenBuilder._terminate_process_tree(process)
            raise BuildError(f"Maven 打包超时（{timeout} 秒），已停止本次部署")
        try:
            return process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            MavenBuilder._terminate_process_tree(process)
            raise BuildError(
                f"Maven 打包超时（{timeout} 秒），已停止本次部署"
            ) from exc

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
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
        else:
            try:
                process.terminate()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    @staticmethod
    def _start_maven(project_path: Path) -> subprocess.Popen[str]:
        if os.name == "nt":
            wrapper = project_path / "mvnw.cmd"
            commands = (
                [str(wrapper), "clean", "package"],
                ["mvn.cmd", "clean", "package"],
                ["mvn", "clean", "package"],
            )
        else:
            wrapper = project_path / "mvnw"
            commands = (
                [str(wrapper), "clean", "package"],
                ["mvn", "clean", "package"],
            )

        last_not_found: FileNotFoundError | None = None
        for command in commands:
            if Path(command[0]) == wrapper and not wrapper.is_file():
                continue
            try:
                return subprocess.Popen(
                    command,
                    cwd=str(project_path),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding=locale.getpreferredencoding(False),
                    errors="replace",
                    bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except FileNotFoundError as exc:
                last_not_found = exc
                continue
            except OSError as exc:
                raise BuildError(f"无法启动 Maven：{exc}") from exc

        raise BuildError(
            "未找到 Maven Wrapper、mvn.cmd 或 mvn，请先配置项目 Wrapper 或 Maven PATH"
        ) from last_not_found

    @staticmethod
    def _find_artifact(config: DeploymentConfig) -> Path:
        if not config.jar_file:
            candidates = MavenBuilder._automatic_jar_candidates(config.project_path)
            if not candidates:
                raise BuildError(
                    "Maven 打包成功，但未在 target 目录中找到可部署的 JAR 文件"
                )
            if len(candidates) > 1:
                raise BuildError(
                    "找到多个可部署的 JAR，无法安全地自动选择，请填写 JAR_FILE：\n"
                    + MavenBuilder._format_candidates(config.project_path, candidates)
                )
            return candidates[0].resolve()

        configured_path = Path(config.jar_file)
        if configured_path.is_absolute():
            direct_path = configured_path
        else:
            direct_path = config.project_path / configured_path

        if direct_path.is_file():
            return direct_path.resolve()

        if configured_path.is_absolute():
            raise BuildError(
                f"JAR_FILE 配置的绝对路径不存在：{configured_path}"
            )

        if configured_path.parent != Path("."):
            raise BuildError(
                f"JAR_FILE 配置的相对路径不存在：{direct_path}"
            )

        jar_name = configured_path.name
        candidates = [
            candidate
            for candidate in config.project_path.rglob(jar_name)
            if candidate.is_file()
            and "target" in {
                part.lower()
                for part in candidate.relative_to(config.project_path).parts[:-1]
            }
        ]
        if not candidates:
            raise BuildError(
                f"打包成功，但未找到 {config.jar_file}；请检查 JAR_FILE 配置"
            )

        if len(candidates) > 1:
            raise BuildError(
                f"找到多个名为 {jar_name} 的 JAR，无法安全地自动选择，"
                "请在 JAR_FILE 中填写相对项目目录的完整路径：\n"
                + MavenBuilder._format_candidates(config.project_path, candidates)
            )
        return candidates[0].resolve()

    @staticmethod
    def _format_candidates(project_path: Path, candidates: list[Path]) -> str:
        lines: list[str] = []
        for candidate in sorted(candidates, key=lambda path: str(path).lower()):
            try:
                display_path = candidate.relative_to(project_path)
            except ValueError:
                display_path = candidate
            lines.append(f"- {display_path}")
        return "\n".join(lines)

    @staticmethod
    def _automatic_jar_candidates(project_path: Path) -> list[Path]:
        candidates: list[Path] = []
        for candidate in project_path.rglob("*.jar"):
            if not candidate.is_file() or candidate.parent.name.lower() != "target":
                continue
            lower_name = candidate.name.lower()
            if lower_name.startswith("original-") or lower_name.endswith(
                ("-sources.jar", "-javadoc.jar", "-tests.jar", "-test.jar")
            ):
                continue
            candidates.append(candidate)

        spring_boot_jars = [
            candidate
            for candidate in candidates
            if MavenBuilder._is_spring_boot_executable_jar(candidate)
        ]
        if spring_boot_jars:
            return spring_boot_jars

        executable_jars = [
            candidate
            for candidate in candidates
            if MavenBuilder._has_main_class(candidate)
        ]
        return executable_jars or candidates

    @staticmethod
    def _is_spring_boot_executable_jar(path: Path) -> bool:
        try:
            with zipfile.ZipFile(path) as archive:
                return any(name.startswith("BOOT-INF/") for name in archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return False

    @staticmethod
    def _has_main_class(path: Path) -> bool:
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = archive.read("META-INF/MANIFEST.MF").decode(
                    "utf-8", errors="replace"
                )
        except (KeyError, OSError, zipfile.BadZipFile):
            return False
        return any(
            line.lower().startswith("main-class:")
            for line in manifest.splitlines()
        )
