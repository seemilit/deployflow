@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto :check_files

where python.exe >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python 环境。
    echo 请先安装 Python，或在项目目录中创建 .venv 虚拟环境。
    goto :failed
)
set "PYTHON_EXE=python.exe"

:check_files
if not exist "%~dp0setup.py" (
    echo [错误] 未找到打包配置文件 setup.py。
    goto :failed
)
if not exist "%~dp0app\qt_main.pyw" (
    echo [错误] 未找到程序入口 app\qt_main.pyw。
    goto :failed
)

echo 正在检查打包环境...
"%PYTHON_EXE%" -c "import cx_Freeze, fabric, paramiko, pyte, PySide6" >nul 2>nul
if errorlevel 1 (
    echo [错误] 打包环境不完整，缺少 cx_Freeze 或项目依赖。
    echo 请执行："%PYTHON_EXE%" -m pip install -r requirements.txt
    goto :failed
)

echo 打包环境检查通过，开始打包...
set "PACKAGE_DIR=%~dp0release\DeployFlow"
set "STAGING_DIR=%~dp0release\.DeployFlow-build"
"%PYTHON_EXE%" setup.py build_exe --build-exe "%STAGING_DIR%"
if errorlevel 1 (
    echo [错误] 打包失败，请查看上方错误信息。
    goto :failed
)

if not exist "%STAGING_DIR%\DeployFlow.exe" (
    echo [错误] 打包命令已结束，但未找到 DeployFlow.exe。
    goto :failed
)

echo 正在更新程序文件并保留现有 conf 配置...
if not exist "%PACKAGE_DIR%" mkdir "%PACKAGE_DIR%"
robocopy "%STAGING_DIR%" "%PACKAGE_DIR%" /MIR /XD conf /R:2 /W:1 >nul
if errorlevel 8 (
    echo [错误] 更新打包目录失败，请确认程序没有正在运行。
    goto :failed
)
if not exist "%PACKAGE_DIR%\conf\tasks" mkdir "%PACKAGE_DIR%\conf\tasks"
if not exist "%PACKAGE_DIR%\conf\parameters" mkdir "%PACKAGE_DIR%\conf\parameters"
if not exist "%PACKAGE_DIR%\conf\scripts" mkdir "%PACKAGE_DIR%\conf\scripts"
rmdir /S /Q "%STAGING_DIR%" 2>nul

echo.
echo [成功] 打包完成：%PACKAGE_DIR%\DeployFlow.exe
goto :finish

:failed
echo.
echo 打包已停止。

:finish
popd
pause
endlocal
