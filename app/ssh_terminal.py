"""Basic interactive SSH terminal session used by the desktop UI."""

from __future__ import annotations

import base64
import codecs
import hashlib
import os
import queue
import shlex
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import msvcrt
except ImportError:  # pragma: no cover - the desktop application targets Windows
    msvcrt = None

from config import ServerParameters


OutputCallback = Callable[[str], None]
ClosedCallback = Callable[[str | None], None]
HostKeyConfirmationCallback = Callable[[str, str, str], bool]
_KNOWN_HOSTS_LOCK = threading.Lock()


@contextmanager
def _known_hosts_write_lock(path: Path) -> Iterator[None]:
    """Serialize known_hosts updates across threads and Windows processes."""

    with _KNOWN_HOSTS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if msvcrt is None:
            yield
            return

        lock_path = path.with_name(f"{path.name}.lock")
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


class SSHSessionError(RuntimeError):
    """Raised when an interactive SSH session cannot be used."""


class _OwnedSFTPClient:
    """SFTP proxy that also owns and closes its dedicated SSH client."""

    def __init__(
        self,
        client: Any,
        sftp: Any,
        released: Callable[[Any], None],
    ) -> None:
        self._client = client
        self._sftp = sftp
        self._released = released
        self._close_lock = threading.Lock()
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sftp, name)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._sftp.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass
        self._released(self._client)


class InteractiveSSHSession:
    def __init__(
        self,
        output: OutputCallback,
        closed: ClosedCallback,
        confirm_host_key: HostKeyConfirmationCallback | None = None,
        known_hosts_path: Path | None = None,
    ) -> None:
        self._output = output
        self._closed = closed
        self._confirm_host_key = confirm_host_key
        self._known_hosts_path = (
            known_hosts_path.expanduser().resolve()
            if known_hosts_path is not None
            else (Path.home() / ".ssh" / "known_hosts").resolve()
        )
        self._client: Any | None = None
        self._channel: Any | None = None
        self._connect_arguments: dict[str, Any] | None = None
        self._monitor_client: Any | None = None
        self._monitor_client_lock = threading.Lock()
        self._monitor_command_lock = threading.Lock()
        self._auxiliary_clients: set[Any] = set()
        self._auxiliary_connection_lock = threading.Lock()
        self._auxiliary_retry_after = 0.0
        self._reader: threading.Thread | None = None
        self._sender: threading.Thread | None = None
        self._send_queue: queue.Queue[bytes] = queue.Queue(maxsize=1024)
        self._urgent_send_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._intentional_close = False
        self._generation = 0
        self._pty_width = 160
        self._pty_height = 48
        self._directory_token = ""
        self._shell_pid: int | None = None
        self._directory_retry_after = 0.0
        self._directory_sftp: Any | None = None
        self._directory_sftp_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        with self._lock:
            channel = self._channel
        return bool(channel is not None and not channel.closed)

    def connect(
        self,
        parameters: ServerParameters,
        initial_directory: str | None = None,
        initial_command: str | None = None,
        track_directory: bool = False,
    ) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise SSHSessionError(
                "缺少 SSH 依赖，请执行：python -m pip install -r requirements.txt"
            ) from exc

        class ConfirmingHostKeyPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(
                policy_self,
                ssh_client: Any,
                hostname: str,
                key: Any,
            ) -> None:
                del policy_self
                fingerprint = "SHA256:" + base64.b64encode(
                    hashlib.sha256(key.asbytes()).digest()
                ).decode("ascii").rstrip("=")
                confirmer = self._confirm_host_key
                if confirmer is None or not confirmer(
                    hostname,
                    key.get_name(),
                    fingerprint,
                ):
                    raise paramiko.SSHException("未信任服务器主机密钥，连接已取消")

                try:
                    with _known_hosts_write_lock(self._known_hosts_path):
                        self._known_hosts_path.parent.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
                        if self._known_hosts_path.is_file():
                            ssh_client.load_host_keys(str(self._known_hosts_path))
                        saved_keys = ssh_client.get_host_keys().lookup(hostname) or {}
                        saved_key = saved_keys.get(key.get_name())
                        if saved_key is not None and saved_key != key:
                            raise paramiko.SSHException(
                                "该服务器已被另一连接保存为不同的主机密钥"
                            )
                        ssh_client.get_host_keys().add(hostname, key.get_name(), key)
                        temporary_path = self._known_hosts_path.with_name(
                            f".{self._known_hosts_path.name}.{os.getpid()}."
                            f"{threading.get_ident()}.tmp"
                        )
                        try:
                            ssh_client.get_host_keys().save(str(temporary_path))
                            temporary_path.replace(self._known_hosts_path)
                        finally:
                            temporary_path.unlink(missing_ok=True)
                except OSError as exc:
                    raise paramiko.SSHException(
                        f"无法保存服务器主机密钥：{exc}"
                    ) from exc

        self.close(notify=False)
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
            if self._known_hosts_path.is_file():
                client.load_host_keys(str(self._known_hosts_path))
        except (OSError, ValueError) as exc:
            client.close()
            raise SSHSessionError(f"无法读取 SSH 主机密钥记录：{exc}") from exc
        client.set_missing_host_key_policy(ConfirmingHostKeyPolicy())

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._client = client
            self._channel = None
            self._connect_arguments = None
            self._directory_token = os.urandom(16).hex() if track_directory else ""
            directory_token = self._directory_token
            self._shell_pid = None
            self._stop.clear()
            self._intentional_close = False

        connect_arguments: dict[str, Any] = {
            "hostname": parameters.ip_address,
            "port": parameters.port,
            "username": parameters.username,
            "timeout": 20,
            "auth_timeout": 20,
            "banner_timeout": 20,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if parameters.auth_method == "PASSWORD":
            connect_arguments["allow_agent"] = False
            connect_arguments["look_for_keys"] = False
            if parameters.password:
                connect_arguments["password"] = parameters.password
        elif parameters.auth_method == "KEY":
            if parameters.key_filename:
                connect_arguments["key_filename"] = str(parameters.key_filename)
        else:
            if parameters.password:
                connect_arguments["password"] = parameters.password
                connect_arguments["allow_agent"] = False
                connect_arguments["look_for_keys"] = False
            if parameters.key_filename:
                connect_arguments["key_filename"] = str(parameters.key_filename)

        try:
            client.connect(**connect_arguments)
            transport = client.get_transport()
            self._configure_transport(transport)
            with self._lock:
                cancelled = (
                    generation != self._generation or self._client is not client
                )
            if cancelled:
                raise SSHSessionError("SSH 连接已取消")
            if initial_directory or track_directory:
                if transport is None:
                    raise paramiko.SSHException("SSH 传输通道不可用")
                channel = transport.open_session(timeout=20)
                channel.get_pty(
                    term="xterm-256color",
                    width=self._pty_width,
                    height=self._pty_height,
                )
                startup_command = 'exec "${SHELL:-/bin/bash}" -l'
                if track_directory:
                    # Identify this PTY's shell without adding commands to its
                    # interactive input/history or changing startup files.
                    startup_command = (
                        "printf '\\033]777;DeployFlowShell;"
                        f"{directory_token};%s\\007' \"$$\"; "
                        + startup_command
                    )
                if initial_directory:
                    startup_command = (
                        f"cd -- {shlex.quote(initial_directory)} || exit; "
                        + startup_command
                    )
                if track_directory:
                    startup_command = "exec /bin/sh -c " + shlex.quote(startup_command)
                channel.exec_command(startup_command)
                if initial_directory and initial_command and initial_command.strip():
                    channel.sendall(
                        (initial_command.rstrip("\r\n") + "\n").encode("utf-8")
                    )
            else:
                channel = client.invoke_shell(
                    term="xterm-256color",
                    width=self._pty_width,
                    height=self._pty_height,
                )
            channel.settimeout(0.1)
        except Exception as exc:
            client.close()
            with self._lock:
                cancelled = generation != self._generation
                if self._client is client:
                    self._client = None
                    self._channel = None
            if cancelled or isinstance(exc, SSHSessionError):
                raise SSHSessionError("SSH 连接已取消") from exc
            if isinstance(exc, paramiko.BadHostKeyException):
                raise SSHSessionError(
                    "服务器主机密钥与 known_hosts 中保存的记录不一致，"
                    "为避免连接到错误服务器，已拒绝本次连接"
                ) from exc
            raise SSHSessionError(f"SSH 连接失败：{exc}") from exc

        with self._lock:
            if generation != self._generation or self._client is not client:
                channel.close()
                client.close()
                raise SSHSessionError("SSH 连接已取消")
            self._channel = channel
            self._connect_arguments = dict(connect_arguments)
            send_queue: queue.Queue[bytes] = queue.Queue(maxsize=1024)
            urgent_send_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
            self._send_queue = send_queue
            self._urgent_send_queue = urgent_send_queue

        self._sender = threading.Thread(
            target=self._send_output,
            args=(generation, channel, send_queue, urgent_send_queue),
            name="ssh-terminal-sender",
            daemon=True,
        )
        self._sender.start()

        self._reader = threading.Thread(
            target=self._read_output,
            args=(generation, channel),
            name="ssh-terminal-reader",
            daemon=True,
        )
        self._reader.start()

    def register_shell(self, token: str, pid: int) -> bool:
        """Accept only the shell identity emitted by this connection attempt."""
        with self._lock:
            if (
                not token or token != self._directory_token or pid <= 1
                or self._channel is None or self._channel.closed
            ):
                return False
            self._shell_pid = pid
            self._directory_retry_after = 0.0
            return True

    def shell_process_id(self) -> int | None:
        """Return the active shell PID for read-only queries on an isolated SFTP connection."""
        with self._lock:
            if self._channel is None or self._channel.closed:
                return None
            return self._shell_pid

    def query_working_directory(self) -> str | None:
        """Read the actual Linux shell cwd over a separate, reusable SFTP channel."""
        with self._directory_sftp_lock:
            with self._lock:
                generation = self._generation
                pid = self._shell_pid
                sftp = self._directory_sftp
                if (
                    pid is None or self._channel is None or self._channel.closed
                    or time.monotonic() < self._directory_retry_after
                ):
                    return None
            try:
                if sftp is None:
                    sftp = self.open_isolated_sftp(allow_shared_fallback=False)
                    sftp.get_channel().settimeout(3)
                    with self._lock:
                        current = generation == self._generation
                        if current:
                            self._directory_sftp = sftp
                    if not current:
                        sftp.close()
                        return None
                directory = sftp.readlink(f"/proc/{pid}/cwd")
                with self._lock:
                    if generation != self._generation:
                        return None
                if (
                    isinstance(directory, str) and directory.startswith("/")
                    and not any(ord(char) < 32 or ord(char) == 127 for char in directory)
                ):
                    return directory
            except Exception:
                with self._lock:
                    if self._directory_sftp is sftp:
                        self._directory_sftp = None
                    if generation == self._generation:
                        self._directory_retry_after = time.monotonic() + 30
                if sftp is not None:
                    try:
                        sftp.close()
                    except Exception:
                        pass
            return None

    def send_line(self, command: str) -> None:
        self.send_raw(command.rstrip("\r\n") + "\n")

    def send_raw(self, value: str) -> None:
        self._queue_payload(value.encode("utf-8"), urgent=False)

    def send_urgent(self, value: str) -> None:
        self._queue_payload(value.encode("utf-8"), urgent=True)

    def send_bytes(self, payload: bytes) -> None:
        self._queue_payload(bytes(payload), urgent=False)

    def _queue_payload(self, payload: bytes, urgent: bool) -> None:
        with self._lock:
            channel = self._channel
            send_queue = self._urgent_send_queue if urgent else self._send_queue
        if channel is None or channel.closed:
            raise SSHSessionError("SSH 尚未连接")
        try:
            send_queue.put_nowait(payload)
        except queue.Full as exc:
            raise SSHSessionError("SSH 发送队列已满，请稍后重试") from exc

    def resize_pty(self, width: int, height: int) -> None:
        width = max(20, int(width))
        height = max(4, int(height))
        with self._lock:
            self._pty_width = width
            self._pty_height = height
            channel = self._channel
        if channel is None or channel.closed:
            return
        try:
            channel.resize_pty(width=width, height=height)
        except Exception as exc:
            raise SSHSessionError(f"调整远程终端大小失败：{exc}") from exc

    def interrupt(self) -> None:
        with self._lock:
            send_queue = self._send_queue
        while True:
            try:
                send_queue.get_nowait()
            except queue.Empty:
                break
        self.send_urgent("\x03")

    def open_sftp(self) -> Any:
        """Open an SFTP channel on the active interactive SSH connection."""
        with self._lock:
            client = self._client
            channel = self._channel
        if client is None or channel is None or channel.closed:
            raise SSHSessionError("SSH 尚未连接")
        try:
            return client.open_sftp()
        except Exception as exc:
            raise SSHSessionError(f"无法打开 SFTP 通道：{exc}") from exc

    def open_isolated_sftp(self, allow_shared_fallback: bool = False) -> Any:
        """Open a dedicated SFTP transport, optionally falling back to the terminal connection."""
        try:
            client = self._create_auxiliary_client()
        except SSHSessionError as isolated_error:
            if not allow_shared_fallback:
                raise isolated_error
            try:
                return self.open_sftp()
            except SSHSessionError as fallback_error:
                raise SSHSessionError(
                    f"独立 SFTP 连接失败（{isolated_error}），"
                    f"当前 SSH 连接回退也失败（{fallback_error}）"
                ) from fallback_error
        with self._lock:
            channel = self._channel
            connected = channel is not None and not channel.closed
            if connected:
                self._auxiliary_clients.add(client)
        if not connected:
            client.close()
            raise SSHSessionError("SSH 尚未连接")
        try:
            sftp = client.open_sftp()
        except Exception as exc:
            self._release_auxiliary_client(client)
            try:
                client.close()
            except Exception:
                pass
            if not allow_shared_fallback:
                raise SSHSessionError(f"无法打开独立 SFTP 连接：{exc}") from exc
            try:
                return self.open_sftp()
            except SSHSessionError as fallback_error:
                raise SSHSessionError(
                    f"无法打开独立 SFTP 连接（{exc}），"
                    f"当前 SSH 连接回退也失败（{fallback_error}）"
                ) from fallback_error
        return _OwnedSFTPClient(client, sftp, self._release_auxiliary_client)

    def _create_auxiliary_client(self) -> Any:
        with self._auxiliary_connection_lock:
            with self._lock:
                generation = self._generation
                if time.monotonic() < self._auxiliary_retry_after:
                    raise SSHSessionError("独立连接暂不可用，使用当前 SSH 连接")
            try:
                client = self._connect_auxiliary_client()
            except SSHSessionError:
                with self._lock:
                    if generation == self._generation:
                        self._auxiliary_retry_after = time.monotonic() + 60
                raise
            with self._lock:
                if generation == self._generation:
                    self._auxiliary_retry_after = 0.0
            return client

    def _connect_auxiliary_client(self) -> Any:
        try:
            import paramiko
        except ImportError as exc:
            raise SSHSessionError("缺少 SSH 依赖，请安装 requirements.txt 中的依赖") from exc
        with self._lock:
            connect_arguments = (
                dict(self._connect_arguments)
                if self._connect_arguments is not None else None
            )
            generation = self._generation
            channel = self._channel
        if connect_arguments is None or channel is None or channel.closed:
            raise SSHSessionError("SSH 尚未连接")
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
            if self._known_hosts_path.is_file():
                client.load_host_keys(str(self._known_hosts_path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(**connect_arguments)
            self._configure_transport(client.get_transport())
            with self._lock:
                valid = (
                    generation == self._generation
                    and self._channel is not None
                    and not self._channel.closed
                )
            if not valid:
                raise SSHSessionError("SSH 连接已关闭")
            return client
        except Exception as exc:
            client.close()
            if isinstance(exc, SSHSessionError):
                raise
            raise SSHSessionError(f"建立辅助 SSH 连接失败：{exc}") from exc

    def _release_auxiliary_client(self, client: Any) -> None:
        with self._lock:
            self._auxiliary_clients.discard(client)

    @staticmethod
    def _configure_transport(transport: Any | None) -> None:
        if transport is None:
            return
        transport.set_keepalive(30)
        sock = getattr(transport, "sock", None)
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (AttributeError, OSError):
                pass

    def query_system_status(self) -> str:
        """Read Linux system status through an independent SSH connection."""
        command = (
            "export LC_ALL=C; "
            "printf 'SYSTEM '; "
            "(. /etc/os-release 2>/dev/null; printf '%s' \"${PRETTY_NAME:-Linux}\"); "
            "printf '\\n'; "
            "printf 'KERNEL '; uname -srmo 2>/dev/null || uname -a; "
            "printf 'UPTIME '; cut -d. -f1 /proc/uptime; "
            "printf 'LOAD '; awk '{print $1, $2, $3}' /proc/loadavg; "
            "grep '^cpu ' /proc/stat | head -n 1; "
            "grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo; "
            "printf 'DF_BEGIN\\n'; df -Pk 2>/dev/null; "
            "printf 'PS_BEGIN\\n'; "
            "ps -eo pid=,pcpu=,pmem=,comm=,args= --sort=-pcpu 2>/dev/null "
            "| head -n 12; "
            "printf 'PORT_BEGIN\\n'; "
            "(ss -H -lntup 2>/dev/null || true)"
        )
        output, error, status = self._execute_auxiliary_command(command, 10)
        if status != 0 or not output.strip():
            raise SSHSessionError(error or "服务器不支持 Linux 系统状态读取")
        return output

    def terminate_process(self, pid: int) -> None:
        """Request graceful termination of one remote process."""
        pid = int(pid)
        if pid <= 1:
            raise SSHSessionError("不允许停止系统核心进程")
        output, error, status = self._execute_auxiliary_command(
            f"kill -TERM {pid}", 10
        )
        output = output.strip()
        error = error.strip()
        if status != 0:
            raise SSHSessionError(error or output or "停止远程进程失败")

    def copy_remote_path(self, source: str, target: str) -> None:
        """Copy on the server, without transferring file contents through the UI."""
        if not source.startswith("/") or not target.startswith("/") or not source.strip("/") or not target.strip("/"):
            raise SSHSessionError("复制需要明确的服务器文件路径，不能操作根目录")
        output, error, status = self._execute_mutating_command(
            f"cp -a -- {shlex.quote(source)} {shlex.quote(target)}", 300
        )
        if status != 0:
            raise SSHSessionError(error.strip() or output.strip() or "服务器端复制失败")

    def capture_process_command(self, pid: int) -> dict[str, Any]:
        """Read a restart snapshot over SFTP; no interpreter is needed remotely."""
        pid = int(pid)
        if pid <= 1:
            raise SSHSessionError("不允许重启系统核心进程")
        sftp = self.open_isolated_sftp()
        try:
            sftp.get_channel().settimeout(8)

            def read(name: str, limit: int = 1048576) -> bytes:
                with sftp.open(f"/proc/{pid}/{name}", "rb") as stream:
                    value = stream.read(limit + 1)
                if len(value) > limit:
                    raise SSHSessionError("进程启动信息过大，未记录不完整的命令")
                return value

            def identity() -> str:
                fields = read("stat", 65536).rsplit(b")", 1)[1].split()
                if fields[0] == b"Z":
                    raise SSHSessionError("进程已经退出")
                return fields[19].decode("ascii")

            start_time = identity()
            raw_args = read("cmdline")
            if not raw_args.endswith(b"\0"):
                raise SSHSessionError("没有完整的启动命令，未停止原进程")
            args = [part.decode("utf-8") for part in raw_args[:-1].split(b"\0")]
            if not args or not args[0]:
                raise SSHSessionError("没有可恢复的启动命令")
            directory = sftp.readlink(f"/proc/{pid}/cwd")
            executable = sftp.readlink(f"/proc/{pid}/exe")
            status = dict(
                line.split(b":", 1) for line in read("status").splitlines() if b":" in line
            )
            environment = [
                part.decode("utf-8") for part in read("environ").split(b"\0") if b"=" in part
            ]
            units = {
                part for line in read("cgroup").decode("utf-8").splitlines()
                for part in line.split(":", 2)[-1].split("/")
                if part.endswith(".service")
                and part not in {"ssh.service", "sshd.service"}
                and not part.startswith("user@")
            }
            if identity() != start_time:
                raise SSHSessionError("进程已经改变，请重新右键记录启动命令")
            return {
                "pid": pid, "start_time": start_time,
                "command": shlex.join(args), "args": args,
                "directory": directory, "executable": executable,
                "uid": int(status[b"Uid"].split()[1]),
                "gid": int(status[b"Gid"].split()[1]),
                "groups": [int(value) for value in status.get(b"Groups", b"").split()],
                "umask": int(status.get(b"Umask", b"0022").strip(), 8),
                "environment": environment, "units": sorted(units),
            }
        except SSHSessionError:
            raise
        except Exception as exc:
            raise SSHSessionError(f"无法记录完整启动命令，未停止原进程：{exc}") from exc
        finally:
            sftp.close()

    def restart_process(self, pid: int, recorded: dict[str, Any] | None = None) -> str:
        """Stop the captured process, then execute its recorded command once."""
        snapshot = recorded if recorded is not None else self.capture_process_command(pid)
        pid = int(pid)
        if pid <= 1 or snapshot["pid"] != pid:
            raise SSHSessionError("启动记录与所选进程不匹配")
        env = dict(value.split("=", 1) for value in snapshot["environment"])
        if env.get("LISTEN_FDS", "0") not in {"", "0"} or env.get("SUPERVISOR_ENABLED") or "PM2_HOME" in env:
            raise SSHSessionError("该进程由管理器托管，直接执行可能重复启动；未停止原进程")
        executable = str(snapshot["executable"])
        directory = shlex.quote(str(snapshot["directory"]))
        launch = (
            f"cd -- {directory} && umask {snapshot['umask']:03o} && exec "
            + shlex.join(["env", "-i", "--", *snapshot["environment"],
                          executable, *snapshot["args"][1:]])
        )
        group_option = (
            "--groups " + shlex.quote(",".join(str(group) for group in snapshot["groups"]))
            if snapshot["groups"] else "--clear-groups"
        )
        preflight = shlex.quote(f"test -d {directory} && test -x {directory} && test -x {shlex.quote(executable)}")
        # Pass the script through stdin, not a shell command argument containing
        # the original environment. No Python or generated remote script file.
        script = f"""set -f
pid={pid}
expected={shlex.quote(snapshot["start_time"])}
fail() {{ printf '%s\\n' "$*" >&2; exit 1; }}
start_id() {{
    [ -r "/proc/$pid/stat" ] || return 1
    IFS= read -r value < "/proc/$pid/stat" || return 1
    value=${{value##*) }}
    set -- $value
    [ "$1" != Z ] || return 1
    printf '%s' "${{20}}"
}}
[ "$(start_id)" = "$expected" ] || fail '进程已改变，请重新右键记录启动命令'
ancestor=$$
while [ "$ancestor" -gt 1 ] 2>/dev/null; do
    [ "$ancestor" != "$pid" ] || fail '不能重启当前 SSH 控制连接依赖的进程'
    ancestor=$(ps -o ppid= -p "$ancestor" | tr -d ' ')
done
for unit in {shlex.join(snapshot["units"])}; do
    policy=$(systemctl show --property=Restart -- "$unit" 2>/dev/null) || fail '无法确认自动重启策略，未停止原进程'
    [ "$policy" = 'Restart=no' ] || fail '服务配置了自动重启，不能同时直接执行原命令，未停止原进程'
done
command -v nohup >/dev/null 2>&1 || fail '服务器缺少 nohup，未停止原进程'
set --
if [ "$(id -u)" != "{snapshot['uid']}" ] || [ "$(id -g)" != "{snapshot['gid']}" ]; then
    [ "$(id -u)" = 0 ] || fail '无法按原账号启动，未停止原进程'
    command -v setpriv >/dev/null 2>&1 || fail '切换原账号需要 setpriv，未停止原进程'
    set -- setpriv --reuid {snapshot['uid']} --regid {snapshot['gid']} {group_option}
fi
"$@" /bin/sh -c {preflight} || fail '原程序或工作目录无法访问，未停止原进程'
logfile=$(umask 077; mktemp /tmp/deployflow-restart-{pid}.XXXXXX) || fail '无法创建启动日志，未停止原进程'
exec 3>>"$logfile" 4>&3
if [ -f "/proc/$pid/fd/1" ]; then exec 3>>"/proc/$pid/fd/1"; fi
if [ -f "/proc/$pid/fd/2" ]; then exec 4>>"/proc/$pid/fd/2"; fi
[ "$(start_id)" = "$expected" ] || fail '进程已改变，未执行重启'
kill -TERM "$pid" || fail '停止原进程失败'
count=0
while [ "$(start_id)" = "$expected" ]; do
    [ "$count" -lt 15 ] || fail '原进程仍未退出，没有强杀，也没有重复启动'
    sleep 1
    count=$((count + 1))
done
nohup "$@" /bin/sh -c {shlex.quote(launch)} < /dev/null >&3 2>&4 &
new_pid=$!
sleep 1
if ! kill -0 "$new_pid" 2>/dev/null; then
    wait "$new_pid"
    code=$?
    [ "$code" = 0 ] || fail "原进程已停止，新命令执行失败（$code）；请检查原日志或 $logfile"
    printf '%s\\n' "原命令已执行并返回 0；若程序转入后台，请刷新服务列表确认。日志：$logfile"
else
    printf '%s\\n' "已按记录的命令和工作目录启动，PID：$new_pid；原输出文件继续使用，其他输出见：$logfile"
fi
"""
        output, error, status = self._execute_mutating_command(
            "/bin/sh -s", 45, script.encode("utf-8")
        )
        if status != 0:
            raise SSHSessionError(error.strip() or output.strip() or "重启结果未确认，请检查服务状态")
        return output.strip()

    def _execute_mutating_command(
        self, command: str, timeout: int, stdin_data: bytes | None = None,
    ) -> tuple[str, str, int]:
        """Execute exactly once; drain both streams and always close the channel."""
        client = self._create_auxiliary_client()
        channel = None
        try:
            with self._lock:
                shell = self._channel
                if shell is None or shell.closed:
                    raise SSHSessionError("SSH 尚未连接")
                self._auxiliary_clients.add(client)
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise SSHSessionError("SSH 连接已断开")
            channel = transport.open_session(timeout=10)
            channel.settimeout(timeout)
            channel.exec_command(command)
            if stdin_data is not None:
                channel.sendall(stdin_data)
            channel.shutdown_write()
            output, error = bytearray(), bytearray()
            deadline = time.monotonic() + timeout
            while True:
                if channel.recv_ready():
                    chunk = channel.recv(65536)
                    output.extend(chunk[:max(0, 1048576 - len(output))])
                if channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(65536)
                    error.extend(chunk[:max(0, 1048576 - len(error))])
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline or self._stop.is_set():
                    raise SSHSessionError("操作超时或连接已关闭，服务器结果尚未确认；请检查后再操作，不会自动重试")
                time.sleep(0.02)
            return (
                output.decode("utf-8", errors="replace"),
                error.decode("utf-8", errors="replace"), channel.recv_exit_status(),
            )
        except SSHSessionError:
            raise
        except Exception as exc:
            raise SSHSessionError(f"服务器操作中断，未自动重试：{exc}") from exc
        finally:
            if channel is not None:
                channel.close()
            self._release_auxiliary_client(client)
            client.close()

    def _execute_auxiliary_command(
        self,
        command: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        with self._monitor_command_lock:
            last_error: Exception | None = None
            for attempt in range(2):
                with self._monitor_client_lock:
                    client = self._monitor_client
                transport = client.get_transport() if client is not None else None
                if transport is None or not transport.is_active():
                    if client is not None:
                        with self._monitor_client_lock:
                            if self._monitor_client is client:
                                self._monitor_client = None
                        try:
                            client.close()
                        except Exception:
                            pass
                    try:
                        client = self._create_auxiliary_client()
                    except SSHSessionError as exc:
                        last_error = exc
                        break
                    with self._lock:
                        connected = (
                            self._channel is not None
                            and not self._channel.closed
                            and self._connect_arguments is not None
                        )
                        if connected:
                            with self._monitor_client_lock:
                                previous_client = self._monitor_client
                                self._monitor_client = client
                        else:
                            previous_client = None
                    if not connected:
                        client.close()
                        raise SSHSessionError("SSH 尚未连接")
                    if previous_client is not None and previous_client is not client:
                        try:
                            previous_client.close()
                        except Exception:
                            pass
                try:
                    return self._execute_client_command(client, command, timeout)
                except Exception as exc:
                    last_error = exc
                    with self._monitor_client_lock:
                        if self._monitor_client is client:
                            self._monitor_client = None
                    try:
                        client.close()
                    except Exception:
                        pass
            detail = str(last_error) if last_error is not None else "未知错误"
            raise SSHSessionError(f"执行独立 SSH 命令失败：{detail}")

    @staticmethod
    def _execute_client_command(
        client: Any,
        command: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        return output, error, status

    def _execute_main_command(
        self,
        command: str,
        timeout: int,
    ) -> tuple[str, str, int]:
        with self._lock:
            client = self._client
            channel = self._channel
        if client is None or channel is None or channel.closed:
            raise SSHSessionError("SSH 尚未连接")
        return self._execute_client_command(client, command, timeout)

    def close(self, notify: bool = True) -> None:
        with self._lock:
            channel = self._channel
            client = self._client
            auxiliary_clients = list(self._auxiliary_clients)
            self._auxiliary_clients.clear()
            had_connection = channel is not None or client is not None
            self._generation += 1
            self._intentional_close = True
            self._stop.set()
            self._channel = None
            self._client = None
            self._connect_arguments = None
            directory_sftp, self._directory_sftp = self._directory_sftp, None
            self._directory_token = ""
            self._shell_pid = None
            self._directory_retry_after = 0.0
            self._auxiliary_retry_after = 0.0
            self._reader = None
            self._sender = None
            with self._monitor_client_lock:
                monitor_client, self._monitor_client = self._monitor_client, None

        if directory_sftp is not None:
            try:
                directory_sftp.close()
            except Exception:
                pass
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if monitor_client is not None:
            try:
                monitor_client.close()
            except Exception:
                pass
        for auxiliary_client in auxiliary_clients:
            try:
                auxiliary_client.close()
            except Exception:
                pass
        if notify and had_connection:
            self._closed(None)

    def _send_output(
        self,
        generation: int,
        channel: Any,
        send_queue: queue.Queue[bytes],
        urgent_send_queue: queue.Queue[bytes],
    ) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    is_current = (
                        generation == self._generation
                        and self._channel is channel
                        and self._send_queue is send_queue
                        and self._urgent_send_queue is urgent_send_queue
                    )
                if not is_current or channel.closed:
                    return
                try:
                    payload = urgent_send_queue.get_nowait()
                    urgent = True
                except queue.Empty:
                    try:
                        payload = send_queue.get(timeout=0.01)
                        urgent = False
                    except queue.Empty:
                        continue
                if not self._send_payload(
                    generation,
                    channel,
                    payload,
                    None if urgent else urgent_send_queue,
                ):
                    return
        except Exception as exc:
            with self._lock:
                should_notify = (
                    generation == self._generation
                    and self._channel is channel
                    and not self._intentional_close
                )
            if should_notify:
                self.close(notify=False)
                self._closed(f"发送命令失败：{exc}")

    def _send_payload(
        self,
        generation: int,
        channel: Any,
        payload: bytes,
        urgent_send_queue: queue.Queue[bytes] | None = None,
    ) -> bool:
        offset = 0
        inactivity_deadline = time.monotonic() + 30
        while offset < len(payload):
            if urgent_send_queue is not None:
                try:
                    urgent_payload = urgent_send_queue.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if not self._send_payload(
                        generation,
                        channel,
                        urgent_payload,
                    ):
                        return False
                    return True
            with self._lock:
                is_current = (
                    generation == self._generation and self._channel is channel
                )
            if self._stop.is_set() or not is_current or channel.closed:
                return False
            try:
                sent = channel.send(payload[offset : offset + 4096])
            except socket.timeout:
                if time.monotonic() >= inactivity_deadline:
                    raise SSHSessionError("远端长时间未接收输入")
                continue
            if sent <= 0:
                raise SSHSessionError("SSH 通道已停止接收输入")
            offset += sent
            inactivity_deadline = time.monotonic() + 30
        return True

    def _read_output(self, generation: int, channel: Any) -> None:
        error_message: str | None = None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while not self._stop.is_set():
                with self._lock:
                    is_current = (
                        generation == self._generation
                        and self._channel is channel
                    )
                if not is_current or channel.closed:
                    break
                received = False
                while channel.recv_ready():
                    received = True
                    data = channel.recv(16384)
                    if not data:
                        break
                    decoded = decoder.decode(data)
                    if decoded:
                        self._output(decoded)
                if not received:
                    self._stop.wait(0.002)
        except Exception as exc:
            if not self._stop.is_set():
                error_message = str(exc)
        finally:
            remaining = decoder.decode(b"", final=True)
            if remaining:
                with self._lock:
                    is_current = (
                        generation == self._generation
                        and self._channel is channel
                    )
                if is_current:
                    self._output(remaining)
            with self._lock:
                should_notify = (
                    generation == self._generation
                    and self._channel is channel
                    and not self._intentional_close
                )
            if should_notify:
                self.close(notify=False)
                self._closed(error_message)
