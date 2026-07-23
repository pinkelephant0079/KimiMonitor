# Changelog

所有版本变更均按 [Keep a Changelog](https://keepachangelog.com/) 格式记录。

## [1.0.0] - 2026-05-19

### 新增
- 菜单栏实时显示 Kimi Code CLI 频限百分比和重置倒计时
- 4 种显示风格：百分比、百分比+倒计时、剩余+倒计时、进度条
- 7 种图标主题：默认、爱心、冰火、钻石、交通灯、月相、电池
- 可自定义告急/警惕阈值（滑块调节）
- 智能刷新：系统唤醒/屏幕解锁立即刷新，黑屏/睡眠暂停
- 低电量模式自动延长刷新间隔
- 屏幕锁定/解锁检测
- kimi 命令完成后自动刷新
- 下拉菜单显示本周用量和频限明细详情
- 一键检查更新（查询 GitHub Releases）
- 首次发布 `.dmg` 安装包

## [1.0.1] - 2026-05-19

### 修复
- 修复 Token 自动刷新端点错误（从 `api.kimi.com` 改为 `auth.kimi.com`）
- 添加 Token 刷新必要的请求头（`X-Msh-Platform`, `X-Msh-Device-Id`）
- 改进刷新错误信息，显示服务器返回的具体错误描述
- 修正 `expires_at` 计算方式（当前时间 + `expires_in`）

### 新增
- Token 刷新熔断机制（连续 3 次失败停止刷新）
- API 失联熔断机制（连续 3 次失败进入静默模式，5 分钟后再试）
- 手动刷新重置所有熔断计数器

## [1.1.2] - 2026-07-23

### 修复
- 双显模式下显示风格不生效：现在 Code 和 Work 都应用当前风格（百分比+倒计时等）
- Work 图标由 💼 改为随主题/阈值变化的状态图标（与 Code 一致）

## [1.1.1] - 2026-07-23

### 修复
- Work 额度数据源修正：改用桌面端官方接口 `GetSubscription`（web token 认证），显示订阅主额度实际使用百分比，与桌面端"我的额度"页口径一致；v1.1.0 误用的 `totalQuota` 实为赠送池
- 新增"🎁 Work 赠送额度"菜单行（存在时显示）

## [1.1.0] - 2026-07-23

### 新增
- 下拉菜单显示 Kimi Work 月度额度（读取桌面端 key，调用 agent-gw 接口）
- 菜单栏显示来源切换：仅 Code / 仅 Work / Code + Work 双显
- 重置倒计时超过 24 小时显示为"X天X小时后"

### 修复
- 修复 Code 接口只返回 `remaining` 字段导致百分比恒为 0% 的问题（兼容 used/remaining 两种字段）

## [Unreleased]

### 计划中
- Apple Developer ID 签名（消除 Gatekeeper 警告）
- 自动更新机制（无需手动下载）
- 开机自启设置
- 通知中心推送（频限即将耗尽时）

---

[1.0.1]: https://github.com/pinkelephant0079/KimiMonitor/releases/tag/v1.0.1
[1.0.0]: https://github.com/pinkelephant0079/KimiMonitor/releases/tag/v1.0.0
