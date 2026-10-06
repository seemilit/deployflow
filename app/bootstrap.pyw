"""Small launcher that reports failures before the application starts."""

from __future__ import annotations

import ctypes
import sys
import traceback
from pathlib import Path
from datetime import datetime


def _report_startup_failure(error: BaseException) -> None:
    application_root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent
    )
    log_path: Path | None = application_root / "startup-error.log"
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        content = "".join(
            f"[{timestamp}] {line}" if line != "\n" else line
            for line in traceback.format_exc().splitlines(keepends=True)
        )
        log_path.write_text(content, encoding="utf-8")
    except OSError:
        log_path = None

    log_hint = (
        f"\n\n错误日志：{log_path}"
        if log_path is not None
        else "\n\n错误日志无法写入，请检查程序目录权限。"
    )
    message = f"程序启动失败：\n{error}{log_hint}"
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "程序启动失败", 0x10)
    except Exception:
        pass


def main() -> None:
    try:
        application_directory = str(Path(__file__).resolve().parent)
        if application_directory not in sys.path:
            sys.path.insert(0, application_directory)
        # A regular import lets cx_Freeze discover the application and its imports.
        from application_qt import main as run_application

        run_application()
    except SystemExit:
        # The application has already displayed and recorded its controlled error.
        return
    except KeyboardInterrupt:
        return
    except Exception as exc:
        _report_startup_failure(exc)


if __name__ == "__main__":
    main()
