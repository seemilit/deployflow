"""Safe branch inspection, merge, and push operations."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from config import DeploymentConfig


LogCallback = Callable[[str], None]


class GitOperationError(RuntimeError):
    """Raised when a Git inspection, merge, or push operation fails."""


@dataclass(frozen=True)
class SourceBranchInfo:
    branch: str
    commit: str
    has_uncommitted_changes: bool


class GitIntegrator:
    def __init__(self, cancel_event: threading.Event | None = None) -> None:
        self._cancel_event = cancel_event or threading.Event()

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise GitOperationError("任务已由用户停止")

    def inspect_source(self, config: DeploymentConfig) -> SourceBranchInfo:
        if not config.git_integration_enabled or config.idea_project_path is None:
            raise GitOperationError("未配置 IDEA_PROJECT_PATH 和 TARGET_BRANCH")

        self._ensure_repository(config.idea_project_path, "IDEA 项目")
        self._ensure_repository(config.project_path, "打包项目")

        branch_result = self._run(
            config.idea_project_path,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or not branch:
            raise GitOperationError("IDEA 项目当前处于 detached HEAD，无法获取分支名称")

        status = self._run(
            config.idea_project_path,
            "status",
            "--porcelain",
        ).stdout
        commit = self._run(
            config.idea_project_path,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ).stdout.strip()
        if not commit:
            raise GitOperationError("无法获取 IDEA 项目当前提交哈希")
        return SourceBranchInfo(
            branch=branch,
            commit=commit,
            has_uncommitted_changes=bool(status.strip()),
        )

    def merge_and_push(
        self,
        config: DeploymentConfig,
        source: SourceBranchInfo,
        log: LogCallback,
    ) -> None:
        if config.idea_project_path is None or config.target_branch is None:
            raise GitOperationError("Git 合并参数不完整")

        self._ensure_clean_build_repository(config.project_path)
        self._validate_branch_name(config.project_path, config.target_branch)

        log(f"获取目标分支：origin/{config.target_branch}")
        self._run_logged(
            config.project_path,
            log,
            "fetch",
            "origin",
            f"+refs/heads/{config.target_branch}:"
            f"refs/remotes/origin/{config.target_branch}",
        )

        local_ref = f"refs/heads/{config.target_branch}"
        has_local_branch = (
            self._run(
                config.project_path,
                "show-ref",
                "--verify",
                "--quiet",
                local_ref,
                check=False,
            ).returncode
            == 0
        )

        if has_local_branch:
            self._run_logged(
                config.project_path,
                log,
                "switch",
                config.target_branch,
            )
        else:
            self._run_logged(
                config.project_path,
                log,
                "switch",
                "-c",
                config.target_branch,
                "--track",
                f"origin/{config.target_branch}",
            )

        log(f"同步目标分支：{config.target_branch}")
        self._run_logged(
            config.project_path,
            log,
            "pull",
            "--ff-only",
            "origin",
            config.target_branch,
        )

        local_target_commit = self._run(
            config.project_path,
            "rev-parse",
            "--verify",
            config.target_branch,
        ).stdout.strip()
        remote_target_commit = self._run(
            config.project_path,
            "rev-parse",
            "--verify",
            f"origin/{config.target_branch}",
        ).stdout.strip()
        if local_target_commit != remote_target_commit:
            raise GitOperationError(
                f"本地目标分支 {config.target_branch} 含有尚未推送或不属于本次部署的提交，"
                "为避免把额外代码推送到远程，已停止部署。请先人工同步或清理打包目录"
            )

        log(
            f"读取 IDEA 确认时的提交：{source.branch} "
            f"({source.commit[:12]})"
        )
        self._run_logged(
            config.project_path,
            log,
            "fetch",
            "--no-tags",
            str(config.idea_project_path),
            source.commit,
        )

        commit_result = self._run(
            config.project_path,
            "cat-file",
            "-e",
            f"{source.commit}^{{commit}}",
            check=False,
        )
        if commit_result.returncode != 0:
            raise GitOperationError(
                "无法从 IDEA 项目读取确认时记录的提交，已停止合并："
                f"{source.commit}"
            )

        merge_required = True
        source_already_merged = self._run(
            config.project_path,
            "merge-base",
            "--is-ancestor",
            source.commit,
            local_target_commit,
            check=False,
        )
        if source_already_merged.returncode == 0:
            log(
                f"源分支 {source.branch} 的提交已包含在目标分支 "
                f"{config.target_branch} 中，跳过合并"
            )
            merge_required = False
        elif source_already_merged.returncode != 1:
            raise GitOperationError(
                "比较目标分支和源分支失败："
                f"{self._result_message(source_already_merged)}"
            )

        if merge_required:
            same_content = self._run(
                config.project_path,
                "diff",
                "--quiet",
                local_target_commit,
                source.commit,
                "--",
                check=False,
            )
            if same_content.returncode == 0:
                log(
                    f"目标分支 {config.target_branch} 与源分支 {source.branch} "
                    "的文件内容没有差异，跳过合并"
                )
                merge_required = False
            elif same_content.returncode != 1:
                raise GitOperationError(
                    "比较目标分支和源分支内容失败："
                    f"{self._result_message(same_content)}"
                )

        if merge_required:
            log(
                f"预检查合并冲突：{source.branch}@{source.commit[:12]} "
                f"-> {config.target_branch}"
            )
            merge_check = self._run(
                config.project_path,
                "merge-tree",
                "--write-tree",
                local_target_commit,
                source.commit,
                check=False,
                timeout=300,
            )
            if merge_check.returncode == 1:
                self._write_output(merge_check, log)
                raise GitOperationError(
                    f"检测到 {source.branch} 合并到 {config.target_branch} 时存在冲突；"
                    "未执行实际合并，打包项目保持在合并前状态，请手动处理"
                )
            if merge_check.returncode != 0:
                raise GitOperationError(
                    "无法执行合并冲突预检查，请确认本机 Git 版本不低于 2.38："
                    f"{self._result_message(merge_check)}"
                )
            log("合并冲突预检查通过")

            log(
                f"合并 {source.branch}@{source.commit[:12]} "
                f"-> {config.target_branch}"
            )
            merge_result = self._run(
                config.project_path,
                "merge",
                "--no-ff",
                "--no-edit",
                source.commit,
                check=False,
                timeout=300,
            )
            self._write_output(merge_result, log)
            if merge_result.returncode != 0:
                merge_head = self._run(
                    config.project_path,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "MERGE_HEAD",
                    check=False,
                )
                detail = self._result_message(merge_result)
                if merge_head.returncode != 0:
                    current_commit = self._run(
                        config.project_path,
                        "rev-parse",
                        "--verify",
                        "HEAD^{commit}",
                    ).stdout.strip()
                    remaining_changes = self._run(
                        config.project_path,
                        "status",
                        "--porcelain",
                    ).stdout.strip()
                    if current_commit == local_target_commit and not remaining_changes:
                        raise GitOperationError(
                            "分支合并失败，但未改变打包项目，已停止后续操作：\n"
                            f"{detail}"
                        )
                    raise GitOperationError(
                        "分支合并失败，且无法确认打包项目状态未改变，"
                        "请人工检查后再继续：\n"
                        f"{detail}"
                    )

                abort_result = self._run(
                    config.project_path,
                    "merge",
                    "--abort",
                    check=False,
                )
                self._write_output(abort_result, log)
                current_commit = self._run(
                    config.project_path,
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                ).stdout.strip()
                remaining_changes = self._run(
                    config.project_path,
                    "status",
                    "--porcelain",
                    check=False,
                ).stdout.strip()
                merge_still_in_progress = self._run(
                    config.project_path,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "MERGE_HEAD",
                    check=False,
                ).returncode == 0
                if (
                    abort_result.returncode == 0
                    and current_commit == local_target_commit
                    and not remaining_changes
                    and not merge_still_in_progress
                ):
                    raise GitOperationError(
                        f"分支合并失败，已停止后续操作并恢复到合并前状态：\n{detail}"
                    )
                abort_detail = self._result_message(abort_result)
                if remaining_changes:
                    abort_detail += "；撤销后工作目录仍存在修改"
                raise GitOperationError(
                    "分支合并失败；无法确认打包项目已完全恢复，"
                    "请人工检查后再继续：\n"
                    f"合并错误：{detail}\n撤销结果：{abort_detail}"
                )
            log("分支合并成功，Git 已自动创建合并提交")

        log(f"推送目标分支：origin/{config.target_branch}")
        self._run_logged(
            config.project_path,
            log,
            "push",
            "origin",
            config.target_branch,
            timeout=300,
        )
        log("分支合并并推送成功")

    def _ensure_repository(self, path: Path, label: str) -> None:
        result = self._run(
            path,
            "rev-parse",
            "--is-inside-work-tree",
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip().lower() != "true":
            raise GitOperationError(f"{label}不是有效的 Git 工作目录：{path}")

    def _ensure_clean_build_repository(self, path: Path) -> None:
        status = self._run(path, "status", "--porcelain").stdout
        if status.strip():
            raise GitOperationError(
                "打包项目目录存在未提交修改，为避免覆盖文件，已停止合并"
            )

    def _validate_branch_name(self, path: Path, branch: str) -> None:
        result = self._run(
            path,
            "check-ref-format",
            "--branch",
            branch,
            check=False,
        )
        if result.returncode != 0:
            raise GitOperationError(f"目标分支名称无效：{branch}")

    def _run_logged(
        self,
        repository: Path,
        log: LogCallback,
        *arguments: str,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run(repository, *arguments, timeout=timeout)
        self._write_output(result, log)
        return result

    @staticmethod
    def _write_output(
        result: subprocess.CompletedProcess[str],
        log: LogCallback,
    ) -> None:
        for output in (result.stdout, result.stderr):
            for line in output.splitlines():
                if line.strip():
                    log(line.rstrip())

    def _run(
        self,
        repository: Path,
        *arguments: str,
        check: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        self._check_cancelled()
        environment = os.environ.copy()
        environment["LC_ALL"] = "C.UTF-8"
        environment["LANG"] = "C.UTF-8"
        try:
            result = subprocess.run(
                ["git", "-C", str(repository), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise GitOperationError("未找到 git，请先安装 Git 并配置 PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitOperationError("Git 操作超时") from exc
        except OSError as exc:
            raise GitOperationError(f"无法执行 Git：{exc}") from exc

        if check and result.returncode != 0:
            raise GitOperationError(GitIntegrator._result_message(result))
        return result

    @staticmethod
    def _result_message(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr.strip() or result.stdout.strip() or "Git 操作失败")
