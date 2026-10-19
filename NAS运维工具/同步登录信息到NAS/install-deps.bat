@echo off
rem [v5 fix: relaunch self so cp65001 is active BEFORE any non-ASCII line is parsed.
rem  chcp mid-file makes cmd mis-track byte offsets at multibyte chars and execute
rem  random line fragments as commands. The stub lines below are pure ASCII.]
if "%~1"=="__cp" goto __cp_go
chcp 65001>nul
cmd /d /c ""%~f0" __cp %*"
exit /b %errorlevel%
:__cp_go
shift
set "PYTHONUTF8=1"
rem ============================================================
rem  安装登录态同步 bat 所需的 Python 模块
rem  （requests / PyYAML / pycryptodome）。双击运行一次即可。
rem  需要联网。   [deps launcher v4 : CRLF+UTF8]
rem ============================================================
title 安装同步依赖   [deps launcher v4 : CRLF+UTF8]
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"

echo ============================================================
echo   安装同步依赖     [deps launcher v4 : CRLF+UTF8]
echo ============================================================
echo.
echo   模块: requests, PyYAML, pycryptodome
echo   预计耗时: 10 到 60 秒。成功标志: 看到 [DONE]。
echo ------------------------------------------------------------
echo.

set "PY="
py -3 -V >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto py_ready
python -V >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto py_ready
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto py_ready
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto py_ready
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY goto py_ready
goto no_py

:py_ready
echo 使用的 Python: %PY%
echo.
%PY% -m pip install --upgrade requests PyYAML pycryptodome
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto fail

echo.
echo 正在验证 ...
%PY% -c "import requests, yaml, Crypto; print('all modules importable')"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto fail

echo.
echo [DONE] 依赖安装完成。现在可以双击本文件夹里的同步 bat 了。
goto end

:no_py
echo [FAIL] 未找到 Python 3.9+。
echo        请到 https://www.python.org/downloads/ 安装，
echo        并勾选 Add python.exe to PATH。
goto halt

:fail
echo [FAIL] pip 失败，退出码 %RC%。请阅读上面的输出。
echo        如果在代理后面，先设置代理，例如:
echo          set HTTPS_PROXY=http://127.0.0.1:7890
goto halt

:halt
echo.
pause
exit /b 1

:end
echo.
pause
endlocal
