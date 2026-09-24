import sys
from pathlib import Path

try:
    from cx_Freeze import Executable, setup
except ImportError as exc:
    raise SystemExit(
        "缺少 cx_Freeze。请先执行：.venv\\Scripts\\python.exe -m pip "
        "install -r requirements.txt"
    ) from exc


base = "Win32GUI" if sys.platform == "win32" else None
APPLICATION_DIRECTORY = Path(__file__).resolve().parent / "app"
if str(APPLICATION_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(APPLICATION_DIRECTORY))

setup(
    name="DeployFlow",
    version="0.3.0",
    description="Maven SSH deployment tool",
    install_requires=[
        "fabric>=3.2,<4",
        "paramiko>=3.4,<5",
        "pyte>=0.8.2,<1",
        "PySide6>=6.8,<7",
        "PySide6-Addons>=6.8,<7",
    ],
    options={
        "build_exe": {
            "packages": [
                "fabric",
                "paramiko",
                "pyte",
                "PySide6",
            ],
            "includes": [
                "application_qt",
                "builder",
                "config",
                "deployer",
                "git_integration",
                "password_protection",
                "ssh_terminal",
                "qt_ssh_terminal_view",
                "qt_xterm_terminal",
                "PySide6.QtWebChannel",
                "PySide6.QtWebEngineCore",
                "PySide6.QtWebEngineWidgets",
                "workflow",
                "workflow_executor",
            ],
            "include_files": [
                ("templates", "templates"),
                ("assets/app_icon.ico", "assets/app_icon.ico"),
                ("app/resources/xterm", "resources/xterm"),
                ("conf/tasks/.gitkeep", "conf/tasks/.gitkeep"),
                ("conf/parameters/.gitkeep", "conf/parameters/.gitkeep"),
                ("conf/scripts/.gitkeep", "conf/scripts/.gitkeep"),
            ],
        }
    },
    executables=[
        Executable(
            "app/qt_main.pyw",
            base=base,
            target_name="DeployFlow.exe",
            icon="assets/app_icon.ico",
        )
    ],
)
