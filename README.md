# 自动签到（TRAE / WorkBuddy）

带 **Web 控制台** 的 TRAE（Trae CN / TRAE SOLO CN / 网页版）与 WorkBuddy 每日自动签到工具。

- Web 控制台：浏览器内导入账户、查看登录状态、查看积分与签到历史、手动签到、修改调度
- 多账户：任意数量的 TRAE / WorkBuddy 账户，界面化管理
- Docker 常驻：容器内置每日定时调度，配置修改即时生效，自动重启
- 官方接口直连：不模拟点击、不开浏览器

## TRAE 账户的两种导入方式

| 方式 | 操作 | 维护成本 |
|---|---|---|
| **网页登录（推荐）** | 浏览器登录 `work.trae.cn` → F12 复制 Cookie → 粘贴到 Web 控制台 | 每次签到自动用 Cookie 换取新 JWT；Cookie 失效后重新粘贴一次 |
| **客户端凭据** | 桌面客户端电脑上运行 `NAS运维工具\同步登录信息到NAS\extract_credentials.py` → 把字段填入 Web 控制台 | refreshToken 自动续期，基本免维护（需一次本地操作） |

WorkBuddy：粘贴客户端 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info` 文件内容导入；token 过期后重开客户端再导入（官方无续期接口）。

## 快速开始（Docker）

```powershell
docker compose up -d --build
```

浏览器打开 `http://<主机IP>:8080`：

1. **添加账户** → 「TRAE 网页登录」→ 按页面指引复制 Cookie 粘贴 → 导入并验证
2. 在「账户」页确认登录态与积分正常
3. 「设置」页调整每天签到时间（默认 08:30，Asia/Shanghai）

之后每天自动签到，无需人工干预。

> 建议在 docker-compose.yml 中设置 `WEB_PASSWORD` 保护控制台。

## 一键同步本机凭据（BAT）

在装有 Trae / WorkBuddy 客户端的电脑上，**双击 `NAS运维工具\同步登录信息到NAS\同步登录信息到NAS.bat`**：

- 自动提取本机 Trae CN / TRAE SOLO CN / WorkBuddy 登录态（只读）
- 自动上传到 NAS 上的签到服务并验证（交互式询问服务地址，答案记在 `sync.conf`）
- 同账号多客户端自动去重（按 user_id，保留登录态最新的一份）
- 已导入的账户重复运行会自动刷新凭据

也可以手动执行：`python "NAS运维工具\同步登录信息到NAS\sync_credentials.py" [服务地址]`（脚本与两个工具同目录）。

## 更新代码到 NAS（BAT）

改完本机代码后，**双击 `NAS运维工具\部署代码到NAS\部署代码到NAS.bat`**：交互询问 NAS 地址 / 登录方式（密钥或密码）/ 用户名 / 远程目录，
自动打包上传并 `docker compose up -d --build` 重建容器，最后打印容器状态。答案记在 `deploy.conf`（**密码不落盘**）。

`docker compose up -d` 不带 `--build` **不会**拾取代码改动，这是最容易踩的坑。

## 本机直接运行（不用 Docker）

```powershell
pip install -r requirements.txt
python -m checkin          # Web 控制台 + 调度，默认 http://127.0.0.1:8080
python -m checkin --once   # 单次执行后退出（可配合系统计划任务）
```

## Web 控制台功能

- **账户页**：登录状态、当前积分、今日签到结果；单账户签到 / 状态刷新 / 删除
- **添加账户**：TRAE 网页 Cookie 导入（自动验证并抓取用户名）/ TRAE 客户端凭据导入 / WorkBuddy .info 导入
- **签到历史**：按天查看每次签到结果与积分变动（保留 180 天）
- **设置**：签到时间、时区、启动即签；保存即时生效

## 目录结构

```
├── checkin/                     # 主程序（Python）
│   ├── __main__.py              # 入口：默认 Web+调度；--once 单次执行
│   ├── web.py                   # FastAPI 控制台 API
│   ├── store.py                 # 配置/账户/历史持久化（data/config.yaml + history.json）
│   ├── traeweb.py               # TRAE 网页版会话认证（Cookie → JWT）
│   ├── runner.py / scheduler.py # 签到执行与每日调度
│   └── platforms/
│       ├── trae.py              # TRAE 签到（client / web 双模式，token 自动续期）
│       └── workbuddy.py         # WorkBuddy 签到（含今日礼包尽力领取）
├── static/index.html            # Web 控制台前端（单文件，无构建）
├── tools/
│   ├── extract_credentials.py   # 从桌面客户端提取登录态（可选，只读）
│   ├── sync_credentials.py      # 把本机登录态同步到远端服务
│   ├── diagnose_workbuddy.py    # WorkBuddy 接口自检（排查 401 / 400 用）
│   └── ui_check.js              # 前端渲染自检（Node，无需浏览器）
├── NAS运维工具/                  # 交互式运维脚本 + 使用说明
│   ├── 部署代码到NAS/            # 打包上传 + 重建容器
│   └── 同步登录信息到NAS/        # 提取并上传本机登录态
├── Dockerfile / docker-compose.yml
├── config.example.yaml          # data/config.yaml 的结构说明（无需手改）
├── 系统说明与部署.md             # 架构、接口细节、部署与排障手册
└── data/                        # 运行时数据（挂载卷持久化，含凭据，勿提交）
```

## 常见问题

- **Cookie 导入提示被风控拦截**：复制的是 DevTools → Network 里请求头的完整 Cookie（包含 ttwid 等），且网页版需处于登录状态。
- **TRAE 提示登录态失效**：网页模式 → 重新复制 Cookie 导入；客户端模式 → 重新登录客户端并提取。
- **WorkBuddy 提示已过期**：打开一次 WorkBuddy 客户端，重新导入 .info。
- **改了设置不生效**：Web 保存即时生效；直接改文件的话 `docker compose restart`。
- **`领取积分失败：HTTP 400`**：官方对「今天已签到」返回 HTTP 400 + 业务码 10001，属正常业务码，程序已按「已签到」处理；若仍报错说明容器里是旧代码，重建镜像即可。
- **控制台没设密码**：`WEB_PASSWORD` 留空时控制台**无鉴权**，局域网内任何人都能打开并删除历史 / 账户。只在可信内网使用，或务必在 `docker-compose.yml` 里设上密码。

## 安全说明

- 登录凭据（`token` / `refresh_token` / `cookie`）只保存在你自己的机器与 `data/config.yaml` 里，**不会上传到任何第三方**；同步脚本也只推送到你自己部署的服务。
- `data/`、`extracted_accounts.yaml`、`data/*.log`、运维脚本的 `deploy.conf` / `sync.conf` 已在 `.gitignore` 中排除——**提交前请确认没有把它们加进来**。
- 请只用于本人账号。

## 免责声明

本项目仅供学习与个人自动化使用，请遵守 TRAE / WorkBuddy 平台规则，仅用于本人账号。相关接口为逆向自官方客户端/网页的非公开实现，可能随版本变化失效。使用产生的任何后果由使用者自行承担。

## 许可证

[MIT](LICENSE)
