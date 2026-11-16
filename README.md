# KimiMonitor

macOS 菜单栏上的 Kimi Code CLI 额度监控器

---

## 简介

KimiMonitor 是一款 macOS 菜单栏应用，实时监控你的 [Kimi Code CLI](https://kimi.com/coding) 额度使用情况。无需打开网页，在屏幕右上角即可掌握：

- **CLI 周调用配额** — 本周 API 调用次数使用进度
- **5小时频限** — 当前频限窗口内的调用次数
- **Work 月度额度** — 加油包代币总消耗比例（与 Kimi 网页端"总使用量"口径一致）

## 功能特性

- **菜单栏实时显示** — 在屏幕右上角随时查看额度百分比和重置倒计时
- **多种显示风格** — 百分比、百分比+倒计时、剩余额度、剩余+倒计时、进度条，一键切换
- **月相图标** — 5 相渐盈随百分比变化（🌑🌒🌓🌔🌕），额度用满即满月
- **多数据源切换** — 菜单栏可显示：仅 Code 频限 / 仅 Work 额度 / Code + Work 双显 / 频限+总额
- **智能刷新策略** — 屏幕亮起/解锁时立即刷新，黑屏/睡眠时暂停，低电量模式自动降频
- **Token 自动续期** — CLI Token 过期前自动刷新，无需手动操作
- **一键检查更新** — 自动检测 GitHub 最新版本并提示

## 系统要求

- macOS 11.0 (Big Sur) 或更高版本
- Apple Silicon 或 Intel Mac
- 已安装并登录 [Kimi Code CLI](https://kimi.com/coding)

## 安装

1. 从 [Releases](https://github.com/pinkelephant0079/KimiMonitor/releases) 下载最新版 `KimiMonitor.dmg`
2. 双击打开 `.dmg` 文件
3. 将 `KimiMonitor.app` 拖拽到 `Applications` 文件夹
4. 从启动台或 Applications 文件夹打开 KimiMonitor

> **首次打开提示**：macOS 可能会显示"无法验证开发者"，请前往「系统设置 → 隐私与安全性 → 安全性」，点击「仍要打开」。

## 使用

安装后，KimiMonitor 会在菜单栏显示为一个图标（默认显示 `⏳ 启动中`，随后显示额度数据）。

点击图标打开下拉菜单：

```
📊 CLI 周调用配额: 13/100 (13.00%) · 6天后
📊 5小时频限: 59/100 (59.00%) · 2h后
📊 Work 月度额度: 73.84% · 27天后
📊 Code 月度消耗: --
━━━━━━━━━━━━━━
🔄 立即刷新
⚙️ 显示风格      → 切换百分比/倒计时/进度条
🖥 菜单栏显示    → 切换 Code / Work / 双显 / 频限+总额
━━━━━━━━━━━━━━
📋 打开开发日志
ℹ️ 关于 KimiMonitor v1.3.1
⬆️ 检查更新
```

### 菜单栏显示模式

在「🖥 菜单栏显示」中可以切换标题显示内容：

| 模式 | 示例 |
|------|------|
| 仅 Code 频限 | `🌓 59% · 2h后` |
| 仅 Work 额度 | `🌔 73.84% · 27天后` |
| Code + Work 双显 | `🌓 59% · 2h后 · 73.84% · 27天后` |
| 频限+总额 | `🌓 59% · 2h后 · 73.84%` |

## 数据隐私

- Code OAuth Token 直接从 `~/.kimi/credentials/kimi-code.json` 读取；Work 会话只读桌面端 Local Storage（旧 Cookie 兜底），**不会上传到任何第三方服务器**
- Code 请求直接发送至 `api.kimi.com`，Work 月度统计请求直接发送至 `www.kimi.com`
- 配置文件保存在本地 `~/.kimi_monitor_app_config.json`

## 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)

## 技术栈

- Python 3.13
- [rumps](https://github.com/jaredks/rumps) — macOS 菜单栏应用框架
- [PyObjC](https://pyobjc.readthedocs.io/) — Python 与 macOS 原生 API 桥接
- PyInstaller — 打包为独立 .app

## License

MIT
