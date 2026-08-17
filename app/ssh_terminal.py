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
        self._reader: threading.Thread | None = None
        self._sender: threading.Thread | None = None
        self._send_queue: queue.Queue[bytes] = queue.Queue(maxsize=1024)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._intentional_close = False
        self._generation = 0
        self._pty_width = 160
        self._pty_height = 48

    @property
    def connected(self) -> bool:
        with self._lock:
            channel = self._channel
        return bool(channel is not None and not channel.closed)

    def connect(
        self,
        parameters: ServerParameters,
        initial_directory: str | None = None,
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
        if parameters.password:
            connect_arguments["password"] = parameters.password
            connect_arguments["allow_agent"] = False
            connect_arguments["look_for_keys"] = False
        if parameters.key_filename:
            connect_arguments["key_filename"] = str(parameters.key_filename)

        try:
            client.connect(**connect_arguments)
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
            with self._lock:
                cancelled = (
                    generation != self._generation or self._client is not client
                )
            if cancelled:
                raise SSHSessionError("SSH 连接已取消")
            if initial_directory:
                if transport is None:
                    raise paramiko.SSHException("SSH 传输通道不可用")
                channel = transport.open_session(timeout=20)
                channel.get_pty(
                    term="xterm-256color",
                    width=self._pty_width,
                    height=self._pty_height,
                )
                startup_command = (
                    f"cd -- {shlex.quote(initial_directory)} && "
                    'exec "${SHELL:-/bin/bash}" -l'
                )
                channel.exec_command(startup_command)
            else:
                channel = client.invoke_shell(
                    term="xterm-256color",
                    width=self._pty_width,
                    height=self._pty_height,
                )
            channel.settimeout(1.0)
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
            send_queue: queue.Queue[bytes] = queue.Queue(maxsize=1024)
            self._send_queue = send_queue

        self._sender = threading.Thread(
            target=self._send_output,
            args=(generation, channel, send_queue),
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

    def send_line(self, command: str) -> None:
        self.send_raw(command.rstrip("\r\n") + "\n")

    def send_raw(self, value: str) -> None:
        payload = value.encode("utf-8")
        with self._lock:
            channel = self._channel
            send_queue = self._send_queue
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
        self.send_raw("\x03")

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

    def close(self, notify: bool = True) -> None:
        with self._lock:
            channel = self._channel
            client = self._client
            had_connection = channel is not None or client is not None
            self._generation += 1
            self._intentional_close = True
            self._stop.set()
            self._channel = None
            self._client = None
            self._reader = None
            self._sender = None

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
        if notify and had_connection:
            self._closed(None)

    def _send_output(
        self,
        generation: int,
        channel: Any,
        send_queue: queue.Queue[bytes],
    ) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    is_current = (
                        generation == self._generation
                        and self._channel is channel
                        and self._send_queue is send_queue
                    )
                if not is_current or channel.closed:
                    return
                try:
                    payload = send_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not self._send_payload(generation, channel, payload):
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
    ) -> bool:
        offset = 0
        inactivity_deadline = time.monotonic() + 30
        while offset < len(payload):
            with self._lock:
                is_current = (
                    generation == self._generation and self._channel is channel
                )
            if self._stop.is_set() or not is_current or channel.closed:
                return False
            try:
                sent = channel.send(payload[offset : offset + 32768])
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
                    data = channel.recv(65536)
                    if not data:
                        break
                    decoded = decoder.decode(data)
                    if decoded:
                        self._output(decoded)
                if not received:
                    self._stop.wait(0.01)
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
