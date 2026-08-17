@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
set "PYTHONPATH=%~dp0app;%PYTHONPATH%"

set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYTHONW%" goto :launch

where pythonw.exe >nul 2>nul
if errorlevel 1 (
    powershell.exe -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('未找到 Python。请创建 .venv，或把 pythonw.exe 加入 PATH。','启动失败')" >nul
    goto :finish
)
set "PYTHONW=pythonw.exe"

:launch
"%PYTHONW%" "%~dp0app\qt_main.pyw" "%~dp0conf\tasks" "%~dp0conf\parameters" "%~dp0conf\scripts" "%~dp0templates\server_parameters.template.txt" "%~dp0templates\remote_script.template.sh"

:finish
popd
endlocal
