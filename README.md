# 自动签到（TRAE / WorkBuddy / Qoder / CodeArts）

带 **Web 控制台** 的 TRAE（Trae CN / TRAE SOLO CN / 网页版）、WorkBuddy（国内版 / 国际版）、Qoder 与 CodeArts Agent 每日自动签到工具。

- Web 控制台：浏览器内导入账户、查看登录状态、查看积分与签到历史、手动签到、修改调度
- 多账户：任意数量的 TRAE / WorkBuddy / Qoder / CodeArts 账户，界面化管理
- Docker 常驻：容器内置每日定时调度，配置修改即时生效，自动重启
- 官方接口直连：不模拟点击、不开浏览器

## 导入账户：一键同步（推荐）

整个流程只需要两步：在 NAS 控制台点一个按钮，在客户端电脑双击一个文件。

**第一步**：浏览器打开 NAS 上的签到控制台 →「添加账户」页 → 点
**「⬇ 下载一键同步启动器（.bat）」**。

**第二步**：把下载到的 `.bat` 拷到装着 Trae / WorkBuddy / Qoder / CodeArts 客户端的那台电脑，
**双击它**。第一次运行时它会自己定位 Python、自动装好依赖、从 NAS 拉取最新的提取脚本，
然后把该电脑上**全部平台**的登录态（TRAE 客户端、TRAE 网页 Cookie 除外——见下）提取出来并
导入服务，最后立刻查询一次真实状态验证。全程无需输入任何内容，约 10 秒完成。

关键在于：**这个 .bat 是点下载的那一刻，由服务器针对你当前环境生成的**（见 `checkin/web.py`
的 `build_sync_tool_bat()`）：

- **服务地址已内嵌**：取你浏览器地址栏里的地址，任意 Docker 端口/主机名组合都自适应。
  换 NAS 地址或端口后，到新地址的控制台重新下载即可，启动器本身不需要改
- **同步授权令牌已内嵌**：由 `WEB_PASSWORD` 经 HMAC-SHA256 现算，不落盘缓存。
  设了密码的服务令牌 30 分钟有效；修改 `WEB_PASSWORD` 即自动作废旧令牌（启动器报 401 时
  重新下载）。令牌**只授予账户导入与工具下载端点**，不授予读控制台的权限
- **提取逻辑永远是最新的**：启动器运行时才从服务端下载脚本包（`checkin/sync_tool/` 随镜像
  发布），所以以后支持新平台、改进解密方式，都不需要重新下载启动器
- **它是一次性产物，不进版本库**：相当于「编译产物」，源码是 `checkin/web.py` 与
  `checkin/sync_tool/`。请勿把自己的启动器提交到公开仓库——里面含内网地址

覆盖的平台与登录态提取方式：TRAE CN / TRAE SOLO CN（解密本地存储 + 真实设备号）、
WorkBuddy 国内版/国际版（新版客户端 token 加密，需客户端正在运行）、Qoder（含机器指纹）、
CodeArts Agent。重复运行是安全的：同名账户自动刷新凭据（upsert）。

> **为什么网页不能直接读取登录态**：凭据只存在于客户端电脑的本地文件或进程内存里
> （WorkBuddy 新版客户端的解密密钥只在运行中的进程内存），浏览器沙箱既读不到本机文件，
> 也读不到其它进程内存。所以「网页点一下就上传」必然需要本机侧执行一次——这个启动器
> 就是把这一次执行变成「双击」而已。

### 备选：手动录入

以下方式适合控制台不可达（服务停机、令牌失效）时兜底，或只想录单个账户：

| 方式 | 操作 | 维护成本 |
|---|---|---|
| **TRAE 网页登录** | 浏览器登录 `work.trae.cn` → F12 复制 Cookie → 粘贴到 Web 控制台 | 每次签到自动用 Cookie 换取新 JWT；Cookie 失效后重新粘贴一次 |
| **交互式 BAT** | 在客户端电脑双击 `NAS运维工具\同步登录信息到NAS\同步登录信息到NAS.bat`：自动提取并上传全部平台登录态（交互式询问服务地址，答案记在 `sync.conf`；带 UAC 提权以读取 WorkBuddy 进程内存） | 与一键同步等效，但每次要手填地址 |
| **WorkBuddy 手工粘贴** | 粘贴客户端 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info` 文件内容 | 旧版客户端明文 token 可直接用；**新版客户端 token 已加密，无法手工粘贴**，只能用上面的自动同步方式 |
| **Qoder 手工粘贴** | 在客户端电脑运行 `python tools\extract_qoder_credentials.py`，把输出的 JSON 粘贴导入 | token 过期可用刷新令牌自动续期（免维护） |
| **CodeArts 手工粘贴** | 在客户端电脑运行 `python tools\extract_codearts_credentials.py`，把输出的 JSON 粘贴导入 | STS 凭据按客户端同机制自动续期 |

### 关于 TRAE 设备号

领取积分时 TRAE 要求请求头 `x-device-id` 是**客户端真实签发**的机器标识，并按设备限制每天
只能签到一个账号（后到的提示「当前设备今日已经签到」；生成/伪造号会被业务码 9074 拒收，文案
伪装成「当前参与用户太多，请稍后再试」）。一键同步与交互式 BAT 都会把客户端账户所在电脑的
**真实设备号**（`device_id` / `machine_id`，见 `checkin/traedevice.py`）随凭据一起导入，
**请勿手工编造设备号**。同一台电脑登录多个 TRAE 账户时共用一个真实设备号，同一天只有先领的
账户能拿到积分（平台限制）；分散到不同电脑即可各自领取。控制台账户表会显示设备号尾号。

> WorkBuddy 国内版（codebuddy.cn）与国际版（workbuddy.ai）按凭据域名自动区分，账户列表分别
> 显示「WorkBuddy」「WorkBuddy国际版」，无需手动选择。

## 快速开始（Docker）

```powershell
docker compose up -d --build
```

浏览器打开 `http://<主机IP>:8080`：

1. **添加账户** → 点「⬇ 下载一键同步启动器（.bat）」→ 拷到客户端电脑双击 → 全部平台登录态自动导入（详见下一节）
2. 在「账户」页确认登录态与积分正常
3. 「设置」页调整每天签到时间（默认 08:30，Asia/Shanghai）

之后每天自动签到，无需人工干预。

> 建议在 docker-compose.yml 中设置 `WEB_PASSWORD` 保护控制台（同时作为同步令牌的派生密钥）。

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
  - 「积分余额」下挂**最早到期批次**小字：TRAE / WorkBuddy 精确到每个积分包（官方接口带过期时间），Qoder 取「套餐周期结束」与「签到批次到期」中更早者（签到批次的到期时刻由领取响应原值留档，无该字段的旧记录按 +30 天近似并标注「估」）。7 天内到期标橙色提醒
- **添加账户**：TRAE 网页 Cookie 导入（自动验证并抓取用户名）/ TRAE 客户端凭据导入 / WorkBuddy .info 导入 / Qoder JSON 导入 / CodeArts JSON 导入；「下载一键同步启动器（.bat）」一次导入全部平台
- **签到历史**：按天查看每次签到结果与积分变动（保留 180 天）
- **软件版本**：顶栏显示版本号（`Ver.年月日时分`，取打包时刻），点击查看「更新详情」（每次发布的变更记录，来源 `checkin/version.py`）
- **设置**：签到时间、时区、启动即签；保存即时生效

## 目录结构

```
├── checkin/                     # 主程序（Python）
│   ├── __main__.py              # 入口：默认 Web+调度；--once 单次执行
│   ├── web.py                   # FastAPI 控制台 API
│   ├── store.py                 # 配置/账户/历史持久化（data/config.yaml + history.json）
│   ├── traeweb.py               # TRAE 网页版会话认证（Cookie → JWT）
│   ├── traedevice.py            # TRAE 客户端真实设备号提取
│   ├── version.py               # 软件版本号（Ver.年月日时分）+ 更新详情
│   ├── runner.py / scheduler.py # 签到执行与每日调度
│   ├── sync_tool/               # 随服务端分发的本机提取脚本包（启动器下载）
│   └── platforms/
│       ├── trae.py              # TRAE 签到（client / web 双模式，token 自动续期）
│       ├── workbuddy.py         # WorkBuddy 签到（国内/国际双主机，含今日礼包）
│       ├── qoder.py             # Qoder 签到（Campaign 每日活动，dt/drt 令牌自动续期）
│       └── codearts.py          # CodeArts Agent 签到（免费积分，STS 自动续期）
├── static/index.html            # Web 控制台前端（单文件，无构建）
├── tools/
│   ├── diagnose_workbuddy.py    # WorkBuddy 接口自检（排查 401 / 400 用）
│   ├── extract_qoder_credentials.py  # Qoder 登录态提取 + 机器指纹生成（Windows 本机运行）
│   ├── extract_codearts_credentials.py  # CodeArts 登录态提取（Windows 本机运行）
│   ├── ui_check.js              # 前端渲染自检（Node，无需浏览器）
│   └── aoci/                    # AOCI 代码认知层（AI 代理的仓库上下文，与签到功能无关）
│       ├── bin/aoci.exe         # 官方 Release 二进制（已校验 SHA256）
│       ├── aoci.ps1             # 包装脚本（自动准备 Git PATH 与可写 TEMP）
│       └── README.md            # 接入说明、校验记录、Managed Scope 规则
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
- **TRAE 提示「当前参与用户太多，请稍后再试」**：这是业务码 9074 的误导文案，实际是**设备号无效**（配置里的设备号是程序生成/伪造的，或换过电脑没重新同步）。在该账户登录的电脑上重新运行一次**一键同步启动器**即可（会带上真实设备号）。
- **TRAE 提示「当前设备今日已经签到」**：同一真实设备号当天已被另一个 TRAE 账户领过——平台的「一设备一天一次」限制。把账户分散到不同电脑，或改用网页版 Cookie 模式。
- **WorkBuddy 提示已过期 / 登录态失效**：打开一次 WorkBuddy 客户端（新版客户端要求正在运行才能取内存密钥），重新运行一键同步启动器刷新登录态。**新版客户端的 token 是加密的，无法手工粘贴 .info 导入**，只能用自动同步方式。
- **启动器报 401 / 下载失败**：说明内嵌令牌已失效——`WEB_PASSWORD` 被改过就会自动作废旧令牌。到控制台重新下载一份启动器即可；确认本机能访问 NAS 地址。
- **启动器提示未找到 Python**：安装 Python 3.9+ 并勾选「Add python.exe to PATH」。启动器会自动补齐 requests / PyYAML / pycryptodome 依赖；若在公司代理后面，先设置 `HTTPS_PROXY` 再运行。
- **Qoder 提示活动列表为空 / 风控拦截（503）**：中国版账户（qoder.com.cn）的签名接口要求机器指纹，指纹失效会被拦截。在装有 Qoder 客户端的电脑上重新运行 `python tools\extract_qoder_credentials.py`，把新 JSON 重新导入即可。国际版账户若出现该提示，请确认每日 10:00（UTC+8）活动已刷新。
- **Qoder 提示登录态已失效且刷新失败**：本机令牌已被客户端轮换作废。打开一次 Qoder 客户端（它会自动刷新登录态），再重新运行提取工具导入。
- **多台电脑同步同名账户会互相顶掉吗**：不会。导入时优先按「平台 + 登录身份（user_id/uid）」匹配，同一账号无论叫什么名都原地刷新凭据；若同名但**不是同一个账号**（user_id/uid 不同），会自动改名保存（如 `Trae CN-2`），绝不覆盖已有账户。重复同步同一台机器也不会产生重复账户。
- **改了设置不生效**：Web 保存即时生效；直接改文件的话 `docker compose restart`。
- **`领取积分失败：HTTP 400`**：官方对「今天已签到」返回 HTTP 400 + 业务码 10001，属正常业务码，程序已按「已签到」处理；若仍报错说明容器里是旧代码，重建镜像即可。
- **控制台没设密码**：`WEB_PASSWORD` 留空时控制台**无鉴权**，局域网内任何人都能打开并删除历史 / 账户。只在可信内网使用，或务必在 `docker-compose.yml` 里设上密码。

## AI 代码认知层（AOCI，可选）

本项目可选接入 [AOCI-CODE](https://github.com/aoci-spec/aoci-code)：一份随 Git 版本化的仓库认知索引，供 AI 编码代理在动手前先了解「每个文件干什么、依赖谁、改时不能踩什么坑」。**与签到功能完全无关**——不参与签到、不读账号凭据、不上传任何数据。

```powershell
.\tools\aoci\aoci.ps1 doctor     # 环境与接入自检
.\tools\aoci\aoci.ps1 verify     # 索引与漂移检查
.\tools\aoci\aoci.ps1 ui --detach --json   # 打开本地只读面板
```

- 接入详情、SHA256 校验记录、Managed Scope 规则见 `tools/aoci/README.md`。
- 需要本机有 **Git**（`scan` 的硬依赖，缺失会失败关闭）；包装脚本已自动处理 Git PATH 与 TEMP。
- `aoci` 二进制放在 `tools/aoci/bin/`（已校验）；源码改动后按实时 Guide 增量维护索引即可，不必整仓重生成。

## 安全说明

- 登录凭据（`token` / `refresh_token` / `cookie`）只保存在你自己的机器与 `data/config.yaml` 里，**不会上传到任何第三方**；同步脚本也只推送到你自己部署的服务。
- `data/`、`extracted_accounts.yaml`、`data/*.log`、运维脚本的 `deploy.conf` / `sync.conf` 已在 `.gitignore` 中排除——**提交前请确认没有把它们加进来**。
- AOCI 的认知索引（`aoci.txt` / `aoci.meta.txt` / `aoci.code.txt` / `AGENTS.md` / `.aoci/`）**有意纳入 Git**（索引需随代码版本化）；而机器绑定的宿主配置 `.mcp.json` 已自动忽略。AOCI 只读源码结构，不读业务数据，接入过程 `network_accessed: false`。
- 请只用于本人账号。

## 免责声明

本项目仅供学习与个人自动化使用，请遵守 TRAE / WorkBuddy 平台规则，仅用于本人账号。相关接口为逆向自官方客户端/网页的非公开实现，可能随版本变化失效。使用产生的任何后果由使用者自行承担。

## 许可证

[MIT](LICENSE)
