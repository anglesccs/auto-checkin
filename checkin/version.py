"""软件版本与更新详情

版本号规则：Ver.YYYYMMDDHHMM（打包时的年月日时分，本地时间）。

发布流程：把 VERSION 改成新的时间戳，并在 CHANGELOG **顶部**插入一条同版本号的
记录（date 用可读时间），控制台「更新详情」按此顺序展示。
"""

VERSION = "Ver.202610121140"

CHANGELOG = [
    {
        "version": "Ver.202610121140",
        "date": "2026-10-12 11:40",
        "changes": [
            "控制台「添加账户」页新增「下载一键同步启动器（.bat）」：启动器内嵌当前服务地址（取自浏览器地址栏，任意 Docker 地址/端口组合自适应）与同步授权令牌，拷到客户端电脑双击即可完成同步，全程免输入",
            "启动器运行时自动定位 Python、按需安装依赖（requests / PyYAML / pycryptodome）、从服务端拉取最新提取脚本包后执行同步",
            "服务新增 /api/sync-tool/bat、/api/sync-tool/bundle、/api/sync-tool/ping 接口；同步令牌由 WEB_PASSWORD 经 HMAC-SHA256 派生（修改密码即自动作废旧令牌），仅授予账户导入与工具下载端点",
            "账户列表 WorkBuddy 平台区分国内版/国际版：凭据域名以 workbuddy.ai / codebuddy.ai 结尾的显示「WorkBuddy国际版」，国内版保持「WorkBuddy」",
        ],
    },
    {
        "version": "Ver.202610112318",
        "date": "2026-10-11 23:18",
        "changes": [
            "新增 WorkBuddy 国际版（WorkBuddy AI）支持：按凭据 domain 自动选择接口主机——国际版 www.workbuddy.ai、国内版 copilot.tencent.com，路径与鉴权完全一致",
            "修复 WorkBuddy 同账号多会话同步取到失效登录态导致 HTTP 401：同 uid 多份 .info 时改为按客户端最近刷新时间（lastRefreshTime）择新，不再只看 expires_at",
        ],
    },
    {
        "version": "Ver.202610112231",
        "date": "2026-10-11 22:31",
        "changes": [
            "修复 CodeArts Agent 账户无法导入：账户持久化层未登记 codearts 平台，导入时报「platform 无效：'codearts'」",
        ],
    },
    {
        "version": "Ver.202610111840",
        "date": "2026-10-11 18:40",
        "changes": [
            "新增 CodeArts Agent（华为云码道）平台支持：每日签到领取免费积分，STS 凭据自动续期",
            "新增 tools/extract_codearts_credentials.py：从本机 CodeArts Agent 客户端解密提取登录态",
            "「同步登录信息到NAS」工具：自动提取并同步 CodeArts Agent 凭据",
        ],
    },
    {
        "version": "Ver.202610020138",
        "date": "2026-10-02 01:38",
        "changes": [
            "首页「积分总额」卡片可视化：新增按客户端着色的积分占比条，小计改为分行展示（彩色圆点 + 数值右对齐），一眼看出各客户端积分多少与占比；状态查询失败的客户端标「(状态未知) --」",
        ],
    },
    {
        "version": "Ver.202610020127",
        "date": "2026-10-02 01:27",
        "changes": [
            "账户列表：移除「模式」列；新增「备注」列——点击铅笔图标即可行内编辑备注，保存后持久化，重新导入/同步凭据不会冲掉已填备注",
            "首页卡片：原「时区」卡片改为「积分总额」——显示所有客户端当前积分合计，并按客户端（TRAE / WorkBuddy / Qoder）小计；状态查询失败的客户端显示 --",
        ],
    },
    {
        "version": "Ver.202609300222",
        "date": "2026-09-30 02:22",
        "changes": [
            "控制台顶栏新增软件版本号（Ver.年月日时分），点击版本号可查看本页「更新详情」",
            "TRAE 签到：修复领取被拒却误报「当前参与用户太多，请稍后再试」——领取接口会校验请求头里的设备号必须是客户端真实签发的机器标识，导入时改为携带真实设备号，不再生成替代值（业务错误码 9074）",
            "TRAE 签到：领取失败时显示业务错误码，并对 9074（设备号不被服务端认可）与 9095（本设备今日已被其它账号使用）给出处理建议",
            "TRAE 签到：遇到短时风控自动退避重试（10 秒 / 30 秒各一次；领取接口幂等，不会重复发放）",
            "「同步登录信息到NAS」工具：同步客户端真实 device_id / machine_id",
            "启动日志打印版本号，便于确认线上运行的是哪一版",
        ],
    },
]
