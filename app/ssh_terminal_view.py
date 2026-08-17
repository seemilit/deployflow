"""Tkinter tab for one independent interactive SSH connection."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import pyte

from config import ServerParameters
from ssh_terminal import InteractiveSSHSession, SSHSessionError


StateCallback = Callable[["SSHTerminalTab"], None]
CloseCallback = Callable[["SSHTerminalTab"], None]
_TERMINAL_HISTORY_LINES = 2000
_MAX_OUTPUT_CHARACTERS_PER_POLL = 262144
_RENDER_INTERVAL_SECONDS = 1 / 20


@dataclass(eq=False)
class _HostKeyRequest:
    attempt: int
    session: InteractiveSSHSession
    hostname: str
    key_type: str
    fingerprint: str
    completed: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class SSHTerminalTab(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        parameters: ServerParameters,
        parameter_path: Path,
        default_open_path: str | None,
        state_changed: StateCallback,
        close_requested: CloseCallback,
    ) -> None:
        super().__init__(master)
        self.parameters = parameters
        self.parameter_path = parameter_path.resolve()
        self.default_open_path = default_open_path
        self._state_changed = state_changed
        self._close_requested = close_requested
        self._state = "disconnected"
        self._attempt = 0
        self._session: InteractiveSSHSession | None = None
        self._events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=512)
        self._terminal_columns = 160
        self._terminal_rows = 48
        self._terminal_screen = pyte.HistoryScreen(
            self._terminal_columns,
            self._terminal_rows,
            history=_TERMINAL_HISTORY_LINES,
        )
        self._terminal_stream = pyte.Stream(self._terminal_screen)
        self._history: list[str] = []
        self._history_index = 0
        self._poll_after_id: str | None = None
        self._resize_after_id: str | None = None
        self._render_after_id: str | None = None
        self._last_render_time = 0.0
        self._host_key_requests: set[_HostKeyRequest] = set()
        self._host_key_requests_lock = threading.Lock()
        self._history_cache_key: tuple[int, int, int, int] | None = None
        self._history_cache_lines: list[str] = []
        self._rendered_lines: list[str] = []
        self._closed = False
        self._create_widgets()
        self._set_state("disconnected")
        self._poll_after_id = self.after(20, self._poll_events)
        self.after_idle(self._apply_terminal_resize)

    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return bool(self._session is not None and self._session.connected)

    def _create_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(6, 5))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=self.parameters.target).grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        self.status_var = tk.StringVar()
        ttk.Label(header, textvariable=self.status_var).grid(
            row=0, column=1, sticky="w"
        )
        self.action_button = ttk.Button(header, command=self._handle_action)
        self.action_button.grid(row=0, column=2, padx=(6, 3))
        ttk.Button(
            header,
            text="关闭标签",
            command=lambda: self._close_requested(self),
        ).grid(row=0, column=3, padx=(3, 0))

        self.output_text = tk.Text(
            self,
            state=tk.DISABLED,
            wrap="none",
            font=("Cascadia Mono", 10),
            background="#111827",
            foreground="#e5e7eb",
            selectbackground="#374151",
            selectforeground="#ffffff",
            takefocus=True,
            relief=tk.FLAT,
            borderwidth=0,
            padx=8,
            pady=8,
        )
        self.output_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            self,
            orient=tk.VERTICAL,
            command=self.output_text.yview,
        )
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.output_text.configure(yscrollcommand=scrollbar.set)
        horizontal_scrollbar = ttk.Scrollbar(
            self,
            orient=tk.HORIZONTAL,
            command=self.output_text.xview,
        )
        horizontal_scrollbar.grid(row=2, column=0, sticky="ew")
        self.output_text.configure(xscrollcommand=horizontal_scrollbar.set)
        self.output_text.bind("<Button-1>", self._focus_terminal_output)
        self.output_text.bind("<KeyPress>", self._handle_terminal_key)
        self.output_text.bind("<Control-c>", self._copy_or_interrupt)
        self.output_text.bind("<Control-C>", self._copy_or_interrupt)
        self.output_text.bind("<Control-v>", self._paste_to_terminal)
        self.output_text.bind("<Control-V>", self._paste_to_terminal)
        self.output_text.bind("<Control-Shift-C>", self._copy_terminal_selection)
        self.output_text.bind("<Control-Shift-V>", self._paste_to_terminal)
        self.output_text.bind("<Button-3>", self._show_terminal_context_menu)
        self.output_text.bind("<Configure>", self._schedule_terminal_resize)

        self.terminal_context_menu = tk.Menu(self, tearoff=False)
        self.terminal_context_menu.add_command(
            label="复制",
            command=self._copy_terminal_selection,
        )
        self.terminal_context_menu.add_command(
            label="粘贴",
            command=self._paste_to_terminal,
        )

        input_frame = ttk.Frame(self, padding=(4, 5))
        input_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
        input_frame.columnconfigure(1, weight=1)
        ttk.Label(input_frame, text="整行命令：").grid(
            row=0, column=0, padx=(0, 5)
        )
        self.command_var = tk.StringVar()
        self.command_entry = ttk.Entry(input_frame, textvariable=self.command_var)
        self.command_entry.grid(row=0, column=1, sticky="ew")
        self.command_entry.bind("<Return>", lambda _event: self._send_command())
        self.command_entry.bind("<Up>", self._history_previous)
        self.command_entry.bind("<Down>", self._history_next)
        self.command_entry.bind("<Button-3>", self._show_command_context_menu)
        self.command_context_menu = tk.Menu(self, tearoff=False)
        self.command_context_menu.add_command(
            label="剪切",
            command=lambda: self.command_entry.event_generate("<<Cut>>"),
        )
        self.command_context_menu.add_command(
            label="复制",
            command=lambda: self.command_entry.event_generate("<<Copy>>"),
        )
        self.command_context_menu.add_command(
            label="粘贴",
            command=lambda: self.command_entry.event_generate("<<Paste>>"),
        )
        self.send_button = ttk.Button(
            input_frame,
            text="发送",
            command=self._send_command,
        )
        self.send_button.grid(row=0, column=2, padx=(6, 3))
        self.interrupt_button = ttk.Button(
            input_frame,
            text="中断",
            command=self._interrupt,
        )
        self.interrupt_button.grid(row=0, column=3, padx=3)
        ttk.Button(input_frame, text="清屏", command=self.clear).grid(
            row=0, column=4, padx=(3, 0)
        )

    def start_connection(self) -> None:
        if self._state == "connecting" or self.connected:
            return
        old_session = self._session
        if old_session is not None:
            self._close_session_async(old_session)

        self._attempt += 1
        attempt = self._attempt
        session: InteractiveSSHSession

        def emit_output(value: str) -> None:
            while (
                not self._closed
                and attempt == self._attempt
                and session is self._session
            ):
                try:
                    self._events.put(
                        ("output", (attempt, session, value)),
                        timeout=0.1,
                    )
                    return
                except queue.Full:
                    continue

        def emit_closed(error: str | None) -> None:
            self._queue_control_event(
                ("closed", (attempt, session, error))
            )

        def confirm_host_key(
            hostname: str,
            key_type: str,
            fingerprint: str,
        ) -> bool:
            return self._request_host_key_confirmation(
                attempt,
                session,
                hostname,
                key_type,
                fingerprint,
            )

        session = InteractiveSSHSession(
            output=emit_output,
            closed=emit_closed,
            confirm_host_key=confirm_host_key,
            known_hosts_path=self.parameter_path.parent.parent / "known_hosts",
        )
        session.resize_pty(self._terminal_columns, self._terminal_rows)
        self._session = session
        self._set_state("connecting")
        self._append(f"[SSH] 正在连接 {self.parameters.target}\n")
        threading.Thread(
            target=self._connect_worker,
            args=(session, attempt),
            name=f"ssh-connect-{self.parameters.name}",
            daemon=True,
        ).start()

    def _connect_worker(
        self,
        session: InteractiveSSHSession,
        attempt: int,
    ) -> None:
        try:
            session.connect(self.parameters, self.default_open_path)
        except SSHSessionError as exc:
            self._queue_control_event(
                ("connect_error", (attempt, session, str(exc)))
            )
        except Exception as exc:
            self._queue_control_event(
                ("connect_error", (attempt, session, f"SSH 连接失败：{exc}"))
            )
        else:
            self._queue_control_event(("connected", (attempt, session)))

    def _queue_control_event(self, event: tuple[str, object]) -> bool:
        while not self._closed:
            try:
                self._events.put(event, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def cancel_connection(self) -> None:
        if self._state != "connecting":
            return
        self._attempt += 1
        self._reject_host_key_requests()
        session = self._session
        self._session = None
        if session is not None:
            self._close_session_async(session)
        self._append("[SSH] 已取消连接\n")
        self._set_state("cancelled")

    def disconnect(self) -> None:
        self._attempt += 1
        self._reject_host_key_requests()
        session = self._session
        self._session = None
        if session is not None:
            self._close_session_async(session)
        self.command_var.set("")
        self._append("[SSH] 已断开连接\n")
        self._set_state("disconnected")

    def focus_terminal(self) -> None:
        if self.connected:
            self.output_text.focus_set()

    def clear(self) -> None:
        self._terminal_screen = pyte.HistoryScreen(
            self._terminal_columns,
            self._terminal_rows,
            history=_TERMINAL_HISTORY_LINES,
        )
        self._terminal_stream = pyte.Stream(self._terminal_screen)
        self._history_cache_key = None
        self._history_cache_lines = []
        self._rendered_lines = []
        self._render_terminal()
        session = self._session
        if session is not None and session.connected:
            try:
                session.send_raw("\x0c")
            except SSHSessionError:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._attempt += 1
        self._reject_host_key_requests()
        session = self._session
        self._session = None
        if session is not None:
            self._close_session_async(session)
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        if self._resize_after_id is not None:
            try:
                self.after_cancel(self._resize_after_id)
            except tk.TclError:
                pass
            self._resize_after_id = None
        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except tk.TclError:
                pass
            self._render_after_id = None

    def _close_session_async(self, session: InteractiveSSHSession) -> None:
        threading.Thread(
            target=session.close,
            kwargs={"notify": False},
            name=f"ssh-close-{self.parameters.name}",
            daemon=True,
        ).start()

    def _request_host_key_confirmation(
        self,
        attempt: int,
        session: InteractiveSSHSession,
        hostname: str,
        key_type: str,
        fingerprint: str,
    ) -> bool:
        request = _HostKeyRequest(
            attempt=attempt,
            session=session,
            hostname=hostname,
            key_type=key_type,
            fingerprint=fingerprint,
        )
        with self._host_key_requests_lock:
            if self._closed:
                return False
            self._host_key_requests.add(request)
        if not self._queue_control_event(("host_key", request)):
            request.completed.set()
            with self._host_key_requests_lock:
                self._host_key_requests.discard(request)
            return False
        if not request.completed.wait(timeout=120):
            request.approved = False
            request.completed.set()
        with self._host_key_requests_lock:
            self._host_key_requests.discard(request)
        return request.completed.is_set() and request.approved

    def _reject_host_key_requests(self) -> None:
        with self._host_key_requests_lock:
            requests = list(self._host_key_requests)
            self._host_key_requests.clear()
        for request in requests:
            request.approved = False
            request.completed.set()

    def _is_current_event(
        self,
        attempt: int,
        session: InteractiveSSHSession,
    ) -> bool:
        return attempt == self._attempt and session is self._session

    def _handle_action(self) -> None:
        if self._state == "connecting":
            self.cancel_connection()
        elif self.connected:
            self.disconnect()
        else:
            self.start_connection()

    def _set_state(self, state: str) -> None:
        self._state = state
        if state != "connected":
            self.command_var.set("")
        labels = {
            "connecting": ("正在连接…", "取消连接"),
            "connected": ("已连接", "断开"),
            "cancelled": ("已取消", "重新连接"),
            "error": ("连接失败", "重新连接"),
            "disconnected": ("未连接", "连接"),
        }
        status_text, action_text = labels[state]
        self.status_var.set(status_text)
        self.action_button.configure(text=action_text, state=tk.NORMAL)
        command_state = tk.NORMAL if state == "connected" else tk.DISABLED
        self.command_entry.configure(state=command_state)
        self.send_button.configure(state=command_state)
        self.interrupt_button.configure(state=command_state)
        self._state_changed(self)

    def _poll_events(self) -> None:
        started_at = time.monotonic()
        processed = 0
        terminal_characters = 0
        terminal_chunks: list[str] = []
        while (
            processed < 256
            and terminal_characters < _MAX_OUTPUT_CHARACTERS_PER_POLL
            and time.monotonic() - started_at < 0.012
        ):
            try:
                event_type, payload = self._events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if event_type == "output":
                attempt, session, value = payload
                if self._is_current_event(attempt, session):
                    output_value = str(value)
                    terminal_chunks.append(output_value)
                    terminal_characters += len(output_value)
            elif event_type == "connected":
                attempt, session = payload
                if not self._is_current_event(attempt, session):
                    continue
                if session.connected:
                    self._set_state("connected")
                    self.focus_terminal()
                else:
                    self._session = None
                    terminal_chunks.append(
                        "\r\n[SSH] 连接建立后立即关闭，请检查服务器状态\r\n"
                    )
                    self._set_state("error")
                    self._close_session_async(session)
            elif event_type == "connect_error":
                attempt, session, error = payload
                if self._is_current_event(attempt, session):
                    self._session = None
                    terminal_chunks.append(f"\r\n[SSH] {error}\r\n")
                    self._set_state("error")
            elif event_type == "closed":
                attempt, session, error = payload
                if self._is_current_event(attempt, session):
                    self._session = None
                    if error:
                        terminal_chunks.append(
                            f"\r\n[SSH] 连接已关闭：{error}\r\n"
                        )
                    else:
                        terminal_chunks.append("\r\n[SSH] 连接已关闭\r\n")
                    self._set_state("disconnected")
            elif event_type == "host_key":
                request = payload
                if not isinstance(request, _HostKeyRequest):
                    continue
                if request.completed.is_set():
                    continue
                approved = False
                if self._is_current_event(request.attempt, request.session):
                    approved = messagebox.askyesno(
                        "确认服务器主机密钥",
                        "这是第一次连接该服务器，尚未保存它的主机密钥。\n\n"
                        f"服务器：{request.hostname}\n"
                        f"密钥类型：{request.key_type}\n"
                        f"指纹：{request.fingerprint}\n\n"
                        "请确认该指纹与服务器管理员提供的一致。是否信任并保存？",
                        parent=self.winfo_toplevel(),
                    )
                request.approved = approved
                request.completed.set()

        if terminal_chunks:
            self._feed_terminal("".join(terminal_chunks))
        try:
            delay = 1 if not self._events.empty() else 20
            self._poll_after_id = self.after(delay, self._poll_events)
        except tk.TclError:
            self._poll_after_id = None

    def _send_command(self) -> None:
        session = self._session
        if session is None or not session.connected:
            return
        command = self.command_var.get()
        try:
            session.send_line(command)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")
            return
        if command.strip():
            if not self._history or self._history[-1] != command:
                self._history.append(command)
                self._history = self._history[-200:]
            self._history_index = len(self._history)
        self.command_var.set("")

    def _handle_terminal_key(self, event: tk.Event) -> str:
        if not self.connected:
            return "break"
        session = self._session
        if session is None:
            return "break"

        control_pressed = bool(event.state & 0x0004)
        if control_pressed and event.keysym.lower() == "v":
            return self._paste_to_terminal()
        if control_pressed and event.keysym.lower() == "c":
            return self._copy_or_interrupt()
        control_key = event.keysym.lower()
        if (
            control_pressed
            and len(control_key) == 1
            and "a" <= control_key <= "z"
        ):
            sequence = chr(ord(control_key) - ord("a") + 1)
        else:
            key_sequences = {
                "Tab": "\t",
                "Return": "\r",
                "BackSpace": "\x7f",
                "Delete": "\x1b[3~",
                "Left": "\x1b[D",
                "Right": "\x1b[C",
                "Up": "\x1b[A",
                "Down": "\x1b[B",
                "Home": "\x1b[H",
                "End": "\x1b[F",
                "Escape": "\x1b",
                "Prior": "\x1b[5~",
                "Next": "\x1b[6~",
                "F1": "\x1bOP",
                "F2": "\x1bOQ",
                "F3": "\x1bOR",
                "F4": "\x1bOS",
                "F5": "\x1b[15~",
                "F6": "\x1b[17~",
                "F7": "\x1b[18~",
                "F8": "\x1b[19~",
                "F9": "\x1b[20~",
                "F10": "\x1b[21~",
                "F11": "\x1b[23~",
                "F12": "\x1b[24~",
            }
            sequence = key_sequences.get(event.keysym)
            if sequence is None and event.char and ord(event.char) >= 32:
                sequence = event.char
        if not sequence:
            return "break"
        try:
            session.send_raw(sequence)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")
        return "break"

    def _focus_terminal_output(self, _event: object = None) -> None:
        self.output_text.focus_set()

    def _copy_terminal_selection(self, _event: object = None) -> str:
        try:
            selected_text = self.output_text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(selected_text)
        return "break"

    def _copy_or_interrupt(self, _event: object = None) -> str:
        try:
            self.output_text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            self._interrupt()
            return "break"
        return self._copy_terminal_selection()

    def _paste_to_terminal(self, _event: object = None) -> str:
        session = self._session
        if session is None or not session.connected:
            return "break"
        try:
            clipboard_text = self.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            session.send_raw(clipboard_text)
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")
        self.output_text.focus_set()
        return "break"

    def _show_terminal_context_menu(self, event: tk.Event) -> str:
        self.output_text.focus_set()
        try:
            self.terminal_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.terminal_context_menu.grab_release()
        return "break"

    def _show_command_context_menu(self, event: tk.Event) -> str:
        self.command_entry.focus_set()
        try:
            self.command_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.command_context_menu.grab_release()
        return "break"

    def _history_previous(self, _event: object = None) -> str:
        if not self._history:
            return "break"
        self._history_index = max(0, self._history_index - 1)
        self.command_var.set(self._history[self._history_index])
        self.command_entry.icursor(tk.END)
        return "break"

    def _history_next(self, _event: object = None) -> str:
        if not self._history:
            return "break"
        self._history_index = min(len(self._history), self._history_index + 1)
        value = (
            self._history[self._history_index]
            if self._history_index < len(self._history)
            else ""
        )
        self.command_var.set(value)
        self.command_entry.icursor(tk.END)
        return "break"

    def _interrupt(self) -> None:
        session = self._session
        if session is None or not session.connected:
            return
        try:
            session.interrupt()
            self.command_var.set("")
        except SSHSessionError as exc:
            self._append(f"[SSH] {exc}\n")

    def _append(self, value: str) -> None:
        if not value:
            return
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        self._feed_terminal(normalized.replace("\n", "\r\n"))

    def _feed_terminal(self, value: str) -> None:
        if not value:
            return
        self._terminal_stream.feed(value)
        elapsed = time.monotonic() - self._last_render_time
        if elapsed >= _RENDER_INTERVAL_SECONDS:
            self._render_terminal()
        elif self._render_after_id is None:
            delay_ms = max(
                1,
                int((_RENDER_INTERVAL_SECONDS - elapsed) * 1000),
            )
            try:
                self._render_after_id = self.after(
                    delay_ms,
                    self._render_terminal_scheduled,
                )
            except tk.TclError:
                self._render_after_id = None

    def _render_terminal_scheduled(self) -> None:
        self._render_after_id = None
        self._render_terminal()

    def _schedule_terminal_resize(self, _event: object = None) -> None:
        if self._closed:
            return
        if self._resize_after_id is not None:
            try:
                self.after_cancel(self._resize_after_id)
            except tk.TclError:
                pass
        try:
            self._resize_after_id = self.after(80, self._apply_terminal_resize)
        except tk.TclError:
            self._resize_after_id = None

    def _apply_terminal_resize(self) -> None:
        self._resize_after_id = None
        if self._closed:
            return
        session = self._session
        try:
            terminal_font = tkfont.Font(font=self.output_text.cget("font"))
            character_width = max(1, terminal_font.measure("0"))
            line_height = max(1, terminal_font.metrics("linespace"))
            columns = max(20, (self.output_text.winfo_width() - 16) // character_width)
            rows = max(4, (self.output_text.winfo_height() - 16) // line_height)
        except tk.TclError:
            return
        if columns == self._terminal_columns and rows == self._terminal_rows:
            return
        self._terminal_columns = columns
        self._terminal_rows = rows
        self._terminal_screen.resize(lines=rows, columns=columns)
        if session is not None:
            try:
                session.resize_pty(columns, rows)
            except SSHSessionError as exc:
                self._append(f"[SSH] {exc}\n")
                return
        self._render_terminal()

    def _history_line_text(self, line: object) -> str:
        getter = getattr(line, "get", None)
        if getter is None:
            return str(line).rstrip()
        characters: list[str] = []
        for column in range(self._terminal_columns):
            cell = getter(column)
            characters.append(getattr(cell, "data", " ") if cell is not None else " ")
        return "".join(characters).rstrip()

    def _render_terminal(self) -> None:
        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except tk.TclError:
                pass
            self._render_after_id = None
        self._last_render_time = time.monotonic()
        try:
            previous_yview = self.output_text.yview()
        except tk.TclError:
            previous_yview = (0.0, 1.0)
        follow_output = previous_yview[1] >= 0.995

        history = getattr(self._terminal_screen, "history", None)
        history_top = list(getattr(history, "top", ()))
        history_key = (
            self._terminal_columns,
            len(history_top),
            id(history_top[0]) if history_top else 0,
            id(history_top[-1]) if history_top else 0,
        )
        if history_key != self._history_cache_key:
            self._history_cache_lines = [
                self._history_line_text(line) for line in history_top
            ]
            self._history_cache_key = history_key
        history_lines = self._history_cache_lines
        display = list(self._terminal_screen.display)
        if not display:
            display = [""]
        cursor_y = min(max(0, self._terminal_screen.cursor.y), len(display) - 1)
        cursor_x = max(0, self._terminal_screen.cursor.x)
        last_content_line = max(
            (index for index, line in enumerate(display) if line.rstrip()),
            default=0,
        )
        last_line = max(cursor_y, last_content_line)
        screen_lines = [line.rstrip() for line in display[: last_line + 1]]
        if len(screen_lines[cursor_y]) <= cursor_x:
            screen_lines[cursor_y] += " " * (
                cursor_x - len(screen_lines[cursor_y]) + 1
            )
        lines = history_lines + screen_lines
        cursor_line = len(history_lines) + cursor_y

        self.output_text.configure(state=tk.NORMAL)
        first_changed = 0
        common_length = min(len(self._rendered_lines), len(lines))
        while (
            first_changed < common_length
            and self._rendered_lines[first_changed] == lines[first_changed]
        ):
            first_changed += 1
        if first_changed == 0:
            self.output_text.delete("1.0", tk.END)
            self.output_text.insert("1.0", "\n".join(lines))
        elif first_changed < len(self._rendered_lines):
            changed_index = f"{first_changed + 1}.0"
            self.output_text.delete(changed_index, "end-1c")
            if first_changed < len(lines):
                self.output_text.insert(
                    changed_index,
                    "\n".join(lines[first_changed:]),
                )
        elif first_changed < len(lines):
            self.output_text.insert(
                "end-1c",
                "\n" + "\n".join(lines[first_changed:]),
            )
        self._rendered_lines = lines
        self.output_text.tag_remove("terminal_cursor", "1.0", tk.END)
        self.output_text.tag_configure(
            "terminal_cursor",
            background="#e5e7eb",
            foreground="#111827",
        )
        cursor_index = f"{cursor_line + 1}.{cursor_x}"
        self.output_text.tag_add("terminal_cursor", cursor_index, f"{cursor_index}+1c")
        if follow_output:
            self.output_text.see(cursor_index)
        else:
            self.output_text.yview_moveto(previous_yview[0])
        self.output_text.configure(state=tk.DISABLED)

    def destroy(self) -> None:
        self.close()
        super().destroy()
