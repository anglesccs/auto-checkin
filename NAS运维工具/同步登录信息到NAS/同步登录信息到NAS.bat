@echo off
chcp 65001>nul
rem ============================================================
rem  把本机的 Trae CN / TRAE SOLO CN / WorkBuddy 登录态
rem  同步到远端 auto-checkin 服务。双击运行。
rem
rem  不内置主机: 运行时输入 NAS 地址（会记住在 sync.conf）。
rem
rem  交互项: 服务地址、端口、可选的 Web 密码。
rem  回答会记在 sync.conf（Web 密码不落盘）。
rem
rem  依赖  : Python 3.9+ 及 requests / PyYAML / pycryptodome
rem          （先双击本文件夹里的 install-deps.bat 装一次）
rem          WorkBuddy 客户端必须正在运行，因为它的 token
rem          用只存在于客户端进程内存里的密钥加密。
rem  [sync launcher v4 : CRLF+UTF8]
rem ============================================================
title 同步登录信息到 NAS   [sync launcher v4 : CRLF+UTF8]
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"
set "PYTHONUTF8=1"

rem ---------- 默认值（依次被 sync.conf 和交互输入覆盖） ----------
set "NAS_IP="
set "WEB_PORT=8080"
set "WEB_PASS="
set "CFG=%HERE%sync.conf"

if not exist "%CFG%" goto cfg_loaded
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%CFG%") do set "%%a=%%b"
:cfg_loaded

if "%~1"=="" goto no_args
set "NAS_IP=%~1"
:no_args
if not "%~2"=="" set "WEB_PORT=%~2"
if not "%~3"=="" set "WEB_PASS=%~3"

cls
echo ============================================================
echo   同步登录信息到 NAS        [sync launcher v4 : CRLF+UTF8]
echo ============================================================
echo.
echo   直接回车 = 采用 [方括号] 里的默认值。
echo.
echo   运行前确认:
echo     - 本机的 Trae / WorkBuddy 客户端已登录
echo     - WorkBuddy 客户端正在运行（token 是加密的）
echo   预计耗时: 约 10 秒。成功标志: 每个账户都显示导入成功，
echo   结尾计数是 N/N。
echo ------------------------------------------------------------
echo.

set "ANS="
set /p "ANS=1) NAS 的 IP 或主机名 [%NAS_IP%] : "
if not "%ANS%"=="" set "NAS_IP=%ANS%"

set "ANS="
set /p "ANS=2) 控制台端口 [%WEB_PORT%] : "
if not "%ANS%"=="" set "WEB_PORT=%ANS%"

set "ANS="
set /p "ANS=3) Web 密码（没有则留空） [] : "
if not "%ANS%"=="" set "WEB_PASS=%ANS%"

set "SERVER=http://%NAS_IP%:%WEB_PORT%"

if "%NAS_IP%"=="" goto no_host

rem ---------- 定位 Python ----------
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
echo.
echo ------------------------------------------------------------
echo   Python  : %PY%
echo   服务    : %SERVER%
echo   脚本    : %HERE%sync_credentials.py
echo ------------------------------------------------------------
echo.

if not exist "%HERE%sync_credentials.py" goto no_script
if not exist "%HERE%extract_credentials.py" goto no_script

%PY% -c "import requests, yaml, Crypto" >nul 2>&1
if errorlevel 1 goto no_deps

rem ---------- 记住回答（不含密码） ----------
>"%CFG%" echo # auto-checkin credential sync config - safe to edit
>>"%CFG%" echo NAS_IP=%NAS_IP%
>>"%CFG%" echo WEB_PORT=%WEB_PORT%

if not "%WEB_PASS%"=="" set "CHECKIN_WEB_PASSWORD=%WEB_PASS%"

%PY% "%HERE%sync_credentials.py" "%SERVER%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto partial

echo.
echo [DONE] 登录态已同步。
echo        打开 %SERVER% 的账户页，确认「凭据有效期」不再是红色。
goto end

:partial
echo.
echo [WARN] 脚本退出码 %RC%，部分账户可能没有导入成功。
echo        请阅读上面的日志。常见原因:
echo          - WorkBuddy 客户端没在运行，加密 token 无法解密
echo          - 本机从未登录过 Trae / WorkBuddy
echo          - 服务地址不可达: %SERVER%
goto end

:no_py
echo [FAIL] 未找到 Python 3.9+。
echo        请到 https://www.python.org/downloads/ 安装
echo        （勾选 Add python.exe to PATH），然后运行 install-deps.bat。
goto halt

:no_host
echo [FAIL] 必须填写 NAS 的 IP。
echo        请在提示处输入，或在 sync.conf 里设置 NAS_IP。
goto halt

:no_script
echo [FAIL] 在 %HERE% 下找不到 sync_credentials.py / extract_credentials.py。
echo        请把 .bat 和这两个 .py 放在同一个文件夹。
goto halt

:no_deps
echo [FAIL] 缺少 Python 模块: requests / PyYAML / pycryptodome。
echo        请先双击本文件夹里的 install-deps.bat，再重新运行。
goto halt

:halt
echo.
pause
exit /b 1

:end
echo.
pause
endlocal
