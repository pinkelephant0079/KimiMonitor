# KimiMonitor 开发开局速查

> 每次新会话开场白：对 Kimi 说 **"启动 KimiMonitor"** 或 **"开工"**、**#dev**，即可自动加载全部项目上下文（行为契约、技术规范、开发日志）。
> 
> 如果 Kimi 没反应，直接 @ 它读 `AGENTS.md`。

---

## 项目是什么

macOS 菜单栏应用，实时监控 Kimi 额度，不用开网页就能看用量和重置倒计时。当前显示三个额度池：

| 额度池 | 数据源 | 说明 |
|--------|--------|------|
| Code 周配额（7天） | `GET https://api.kimi.com/coding/v1/usages` + OAuth | 返回 `remaining` 字段（不是 `used`！） |
| Code 5小时频限 | 同上 `limits[0]`，window=300分钟 | 曾经误标"5分钟"，v1.2.1 已修正 |
| **Work 订阅主额度** | `POST https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscription` + web token | `balances[SUBSCRIPTION].amountUsedRatio`，与桌面端"我的额度"页同口径 |

另有 Work 赠送池（`balances[GIFT]`），仅在菜单行展示。

## 凭证位置（关键！）

| 用途 | 文件 | 类型 |
|------|------|------|
| Code API | `~/.kimi/credentials/kimi-code.json` | OAuth，App 会自动刷新（熔断 3 次） |
| Work API | `~/Library/Application Support/kimi-desktop/bridge-store/token-store.json` → `tokens.access_token` | web token，**桌面端自行刷新该文件，App 只读不写** |
| ⚠️ 陷阱 | `agent-gw.kimi.com/coding/v1/usages` + `daimon-share/daimon/kimi-code-key.json` | 返回的 `totalQuota` 是**赠送池**，不是主额度！v1.1.0 踩过这个坑，v1.1.1 已纠正 |

菜单栏图标：月相 5 相渐盈（0-19 🌑 / 20-39 🌒 / 40-59 🌓 / 60-79 🌔 / 80-100 🌕，用满即满月）。双显模式只显示一个月相，按 Code + Work 百分比之和（封顶 100%），两者共用额度池。

---

## 目录结构

```
KimiMonitor/
├── app.py              # 主程序（单文件，全部逻辑）
├── app_debug.py        # 调试用启动入口
├── build_release.sh    # 打包脚本（PyInstaller → 签名 → dmg → sha256）
├── KimiMonitor.spec    # PyInstaller 配置
├── version.py          # 版本号（打包脚本从这里读）
├── setup.py            # 安装配置
├── AGENTS.md           # 行为契约（AI 必读）
├── CLAUDE.md           # 开发准则（AI 必读）
├── CHANGELOG.md        # 版本变更
├── README.md           # 用户文档
└── BOOTSTRAP.md        # ← 你正在看的这个
```

外部关联：
- `~/KimiBackups/devlogs/KimiMonitor_DevLog.md` — 详细开发历史
- `~/KimiBackups/devlogs/航海日志.md` — 系统配置变更记录
- 打包虚拟环境：`../kimi_monitor_env/`（build_release.sh 依赖）

---

## 常用命令

```bash
# 打包（版本号自动读 version.py）
./build_release.sh

# 部署 + 重启（打包后标准流程）
rm -rf /Applications/KimiMonitor.app
cp -R dist/KimiMonitor.app /Applications/
pkill -f "KimiMonitor.app/Contents/MacOS/KimiMonitor"
open /Applications/KimiMonitor.app

# 冒烟测试数据层（无 rumps 环境，stub GUI 依赖后 import app 测 API 解析）
# 参考 git 历史 v1.1.x 时期的测试写法

# 发版
git push origin main && git tag vX.X.X && git push origin vX.X.X
# gh 未登录时用钥匙串凭据：
GH_TOKEN=$(printf "protocol=https\nhost=github.com\n\n" | git credential fill | sed -n 's/^password=//p') gh release create vX.X.X dist/KimiMonitor-X.X.X.dmg ...
```

## 发版状态

- 本地：`v1.2.2`（已部署运行中）
- GitHub Release：`v1.2.0`，**落后本地 2 个版本**（v1.2.1、v1.2.2 攒着下次一起推）

---

## 技术栈

- Python 3.13
- rumps（macOS 菜单栏）
- PyObjC（系统事件监听）
- requests（API 调用）
- PyInstaller（打包 .app）

---

## 代码风格（对 AI 的要求）

- 类型注解必须
- 字符串用双引号
- 注释中文，代码英文
- UI 文本中文

---

## 开发红线

- 网络请求必须设 `timeout`
- API 错误记到 `self._last_error`（Work 侧记 `self._work_error`）
- Token 过期自动刷新（最多 3 次）
- 连续失败 3 次进静默模式
- Work 接口失败**静默降级**，不刷屏、不影响 Code 显示
- 接口数字字段是**字符串**（`"limit": "100"`），解析必须 `int()`
- **dist/ 和 build/ 不入 git**

---

## 待办改进（未实施，按价值排序）

1. 401 刷新重试分支与正常 200 路径代码重复，可合并
2. token 写文件（`_refresh_token` 覆写 kimi-code.json）无锁，建议临时文件+原子替换
3. `on_open_log` 路径硬编码 `~/KimiBackups/devlogs/`
4. 打包瘦身：dmg 15 MB，spec 排除未用 PyObjC 子模块可减半
5. rumps 私有属性 `_slider` / `_menuitem` 脆弱性（已在用，升级 rumps 时注意）

---

## 版本

当前：`v1.2.2`

查看详细变更 → `CHANGELOG.md`
