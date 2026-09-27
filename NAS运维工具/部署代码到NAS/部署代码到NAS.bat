@echo off
chcp 65001>nul
rem ============================================================
rem  把本机 auto-checkin 代码部署到飞牛 NAS 并重建 docker 镜像。
rem  双击运行。
rem
rem  不内置主机: 运行时输入 NAS 地址（会记住在 deploy.conf）。
rem
rem  交互项: NAS 地址、登录方式（密钥 / 密码）、SSH 用户、
rem  SSH 密码、远程目录、控制台端口。
rem  回答会记在 deploy.conf（密码不落盘）。
rem
rem  用法: 本脚本 [用户名@主机] [远程目录]
rem        参数只用于预填，回车即采用。
rem  [deploy launcher v4 : CRLF+UTF8]
rem ============================================================
title 部署 auto-checkin 到 NAS   [deploy launcher v4 : CRLF+UTF8]
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"

rem ---------- 默认值（依次被 deploy.conf 和交互输入覆盖） ----------
set "NAS_IP="
set "NAS_PORT=22"
set "NAS_USER="
set "AUTH_MODE=1"
set "RDIR=/vol2/1000/docker/auto-checkin"
set "WEB_PORT=8080"
set "SRC_OVERRIDE="
set "CFG=%HERE%deploy.conf"

if not exist "%CFG%" goto cfg_loaded
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%CFG%") do set "%%a=%%b"
:cfg_loaded

if "%~1"=="" goto no_args
for /f "tokens=1,* delims=@" %%a in ("%~1") do call :takeid "%%a" "%%b"
:no_args
if not "%~2"=="" set "RDIR=%~2"

cls
echo ============================================================
echo   部署 auto-checkin 到 NAS    [deploy launcher v4 : CRLF+UTF8]
echo ============================================================
echo.
echo   按提示回答下面的问题。直接回车 = 采用 [方括号] 里的默认值。
echo.
echo   预计耗时: 1 到 3 分钟（docker 重建镜像）。
echo   成功标志: 看到 [DONE]，且状态行显示 Up。
echo ------------------------------------------------------------
echo.

set "ANS="
set /p "ANS=1) NAS 的 IP 或主机名 [%NAS_IP%] : "
if not "%ANS%"=="" set "NAS_IP=%ANS%"

set "ANS="
set /p "ANS=2) SSH 端口 [%NAS_PORT%] : "
if not "%ANS%"=="" set "NAS_PORT=%ANS%"

echo.
echo   3) 登录方式:
echo        1 = SSH 密钥 / 免密登录   （默认）
echo        2 = SSH 密码
set "ANS="
set /p "ANS=      请选 1 或 2 [%AUTH_MODE%] : "
if not "%ANS%"=="" set "AUTH_MODE=%ANS%"

set "ANS="
set /p "ANS=4) SSH 用户名 [%NAS_USER%] : "
if not "%ANS%"=="" set "NAS_USER=%ANS%"

set "TARGET=%NAS_USER%@%NAS_IP%"
set "NAS_PASS="
set "USE_PLINK="

if not "%AUTH_MODE%"=="2" goto ask_paths
echo.
echo   5) SSH 密码 - 输入不回显，绝不写入磁盘。
where powershell >nul 2>&1
if errorlevel 1 goto ask_pass_plain
where plink >nul 2>&1
if errorlevel 1 goto ask_pass_openssh
set "USE_PLINK=1"
for /f "delims=" %%p in ('powershell -NoProfile -Command "$s=Read-Host -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"') do set "NAS_PASS=%%p"
if not "%NAS_PASS%"=="" goto ask_paths
set "USE_PLINK="
echo      没有取到密码 - 上传时 ssh 会自行再问一次。
goto ask_paths

:ask_pass_openssh
echo      未找到 plink.exe。OpenSSH 无法接受脚本喂入的密码，
echo      所以上传前 ssh 会自己提示一次 - 在那里输入即可。
echo      （安装 PuTTY 可实现全自动。）
goto ask_paths

:ask_pass_plain
set /p "NAS_PASS=      密码: "
if "%NAS_PASS%"=="" goto no_pass

:ask_paths
echo.
set "ANS="
set /p "ANS=6) 远程项目目录 [%RDIR%] : "
if not "%ANS%"=="" set "RDIR=%ANS%"

set "ANS="
set /p "ANS=7) 控制台端口 [%WEB_PORT%] : "
if not "%ANS%"=="" set "WEB_PORT=%ANS%"

if "%NAS_IP%"=="" goto no_host
if "%NAS_USER%"=="" goto no_host

rem ---------- 定位要部署的代码 ----------
rem  1) deploy.conf 里的 SRC_OVERRIDE
rem  2) 从本文件夹逐级向上（在本项目内时找到项目根）
rem  3) 本文件夹下的 src_snapshot（整包移植副本）
set "SRC="
set "CUR=%HERE%"
set /a DEPTH=0
if "%SRC_OVERRIDE%"=="" goto walk
call :probe "%SRC_OVERRIDE%"
if defined SRC goto src_ready

:walk
if %DEPTH% GEQ 6 goto walk_end
call :probe "%CUR%"
if defined SRC goto src_ready
for %%p in ("%CUR%\..") do set "PARENT=%%~fp"
if /i "%PARENT%"=="%CUR%" goto walk_end
set "CUR=%PARENT%\"
set /a DEPTH+=1
goto walk

:walk_end
call :probe "%HERE%src_snapshot"
if defined SRC goto src_ready

:no_src
echo.
echo [FAIL] 找不到要部署的代码。
echo        查找条件: 同时含 checkin\web.py 和 Dockerfile 的目录:
echo          - deploy.conf 里的 SRC_OVERRIDE
echo          - 本文件夹及其最多 6 级上级目录
echo          - %HERE%src_snapshot
echo.
echo        处理: 把本文件夹放回项目内；或把
echo        checkin static Dockerfile docker-compose.yml
echo        requirements.txt .dockerignore 拷进 src_snapshot\；
echo        或在 deploy.conf 里设置 SRC_OVERRIDE=（项目根目录）
goto halt

:src_ready
echo.
echo ------------------------------------------------------------
echo   代码来源 : %SRC%
echo   目标     : %TARGET%  端口 %NAS_PORT%
echo   远程目录 : %RDIR%
echo   控制台   : http://%NAS_IP%:%WEB_PORT%
if "%USE_PLINK%"=="1" echo   登录方式 : 密码（plink）
if "%USE_PLINK%"=="" if "%AUTH_MODE%"=="2" echo   登录方式 : 密码（OpenSSH 会再提示一次）
if not "%AUTH_MODE%"=="2" echo   登录方式 : SSH 密钥 / 免密
echo ------------------------------------------------------------
echo.

where ssh >nul 2>&1
if errorlevel 1 goto no_ssh
where tar >nul 2>&1
if errorlevel 1 goto no_tar

rem ---------- 记住回答（不含密码） ----------
>"%CFG%" echo # auto-checkin deploy config - auto-generated, safe to edit
>>"%CFG%" echo NAS_IP=%NAS_IP%
>>"%CFG%" echo NAS_PORT=%NAS_PORT%
>>"%CFG%" echo NAS_USER=%NAS_USER%
>>"%CFG%" echo AUTH_MODE=%AUTH_MODE%
>>"%CFG%" echo RDIR=%RDIR%
>>"%CFG%" echo WEB_PORT=%WEB_PORT%
>>"%CFG%" echo SRC_OVERRIDE=%SRC_OVERRIDE%

set "TGZ=%TEMP%\auto-checkin-src.tgz"
if exist "%TGZ%" del "%TGZ%"

echo [1/3] 正在打包 %SRC% 的代码 ...
cd /d "%SRC%"
if errorlevel 1 goto pack_fail
tar -czf "auto-checkin-src.tgz" --exclude=__pycache__ --exclude=*.pyc --exclude=.git checkin static Dockerfile docker-compose.yml requirements.txt .dockerignore
set "RC=%ERRORLEVEL%"
cd /d "%HERE%"
if not "%RC%"=="0" goto pack_fail
if not exist "%SRC%\auto-checkin-src.tgz" goto pack_fail
if exist "%TGZ%" del "%TGZ%"
move /y "%SRC%\auto-checkin-src.tgz" "%TGZ%" >nul
if not exist "%TGZ%" goto pack_fail
echo       ok.

echo.
echo [2/3] 正在上传并在 NAS 上重建（1 到 3 分钟）...
echo       --- 远端输出 ---
if defined USE_PLINK goto plink_run
ssh -p %NAS_PORT% -o StrictHostKeyChecking=accept-new "%TARGET%" "cd %RDIR% && tar xzf - && docker compose up -d --build && docker ps --filter name=auto-checkin --format '{{.Names}} {{.Status}}'" < "%TGZ%"
set "RC=%ERRORLEVEL%"
goto check_rc

:plink_run
plink -pw "%NAS_PASS%" -P %NAS_PORT% "%TARGET%" "cd %RDIR% && tar xzf - && docker compose up -d --build && docker ps --filter name=auto-checkin --format '{{.Names}} {{.Status}}'" < "%TGZ%"
set "RC=%ERRORLEVEL%"

:check_rc
echo       --- 远端输出结束 ---
if exist "%TGZ%" del "%TGZ%"
if not "%RC%"=="0" goto run_fail

echo.
echo [DONE] 部署完成。确认上面的状态行显示 Up，然后打开
echo        http://%NAS_IP%:%WEB_PORT% 检查账户页。
goto end

:no_ssh
echo [FAIL] 未找到 ssh.exe。
echo        设置 - 应用 - 可选功能 - 添加「OpenSSH 客户端」。
goto halt

:no_tar
echo [FAIL] 未找到 tar.exe。Windows 10 1803 以上自带。
echo        到 设置 - 应用 - 可选功能 里添加，或使用项目文档中的
echo        手工 tar 管道命令。
goto halt

:no_host
echo [FAIL] 必须填写 NAS 的 IP 和 SSH 用户名。
echo        请在提示处输入，或在 deploy.conf 里设置 NAS_IP / NAS_USER。
goto halt

:no_pass
echo [FAIL] 密码为空。没有部署任何东西。
goto halt

:pack_fail
echo [FAIL] 打包源码失败。没有上传任何东西。
echo        请查看上面的 tar 输出。源码目录: %SRC%
goto halt

:run_fail
echo [FAIL] 上传 / 重建失败，退出码 %RC%。请阅读上面的远端输出。
echo        常见原因:
echo          - SSH 密码、用户名或端口不对
echo          - 远程目录不存在: %RDIR%
echo          - NAS 上没有 docker compose
echo          - NAS 不可达
goto halt

:takeid
set "NAS_USER=%~1"
if not "%~2"=="" set "NAS_IP=%~2"
goto :eof

:probe
set "P=%~1"
if "%P%"=="" goto :eof
if "%P:~-1%"=="\" set "P=%P:~0,-1%"
if not exist "%P%\checkin\web.py" goto :eof
if not exist "%P%\Dockerfile" goto :eof
set "SRC=%P%"
goto :eof

:halt
if exist "%SRC%\auto-checkin-src.tgz" del "%SRC%\auto-checkin-src.tgz"
if exist "%TGZ%" del "%TGZ%"
echo.
pause
exit /b 1

:end
echo.
pause
endlocal
