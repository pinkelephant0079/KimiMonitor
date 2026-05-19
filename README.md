# KimiMonitor

 macOS 菜单栏上的 Kimi Code CLI 频限监控器

---

## 简介

KimiMonitor 是一款 macOS 菜单栏应用，实时显示你的 [Kimi Code CLI](https://kimi.com/coding) 频限（Rate Limit）使用情况，让你无需打开网页就能掌握剩余额度和重置时间。

## 功能特性

- **菜单栏实时显示** — 在屏幕右上角随时查看频限百分比和重置倒计时
- **多种显示风格** — 百分比、百分比+倒计时、剩余额度、进度条，一键切换
- **丰富的图标主题** — 默认、爱心、冰火、钻石、交通灯、月相、电池 7 种主题
- **智能刷新策略** — 屏幕亮起/解锁时立即刷新，黑屏/睡眠时暂停，低电量模式自动降频
- **自定义阈值** — 滑块调整告急/警惕阈值，实时生效
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

安装后，KimiMonitor 会在菜单栏显示为一个图标（默认显示 `⏳ 启动中`，随后显示频限数据）。

点击图标打开下拉菜单：

```
📊 本周用量: 13/100 (13%) · 6天后
📊 频限明细: 59/100 (59%) · 2h后
━━━━━━━━━━━━━━
🔄 立即刷新
⚙️ 显示风格      → 切换百分比/倒计时/进度条
🎨 图标主题      → 切换 7 种主题
⚠️ 阈值设置      → 调整告急/警惕阈值
━━━━━━━━━━━━━━
📋 打开开发日志
ℹ️ 关于 KimiMonitor v1.0.0
⬆️ 检查更新
```

## 数据隐私

- 你的 OAuth Token 直接从 `~/.kimi/credentials/kimi-code.json` 读取，**不会上传到任何第三方服务器**
- 所有 API 请求直接发送至 `api.kimi.com`
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
