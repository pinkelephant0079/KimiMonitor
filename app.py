#!/usr/bin/env python3
"""
KimiMonitor - macOS 菜单栏频限监控器
技术栈: Python 3.13 + rumps + requests + threading + PyObjC callAfter
"""

import sys
# Force line buffering so logs appear immediately in file
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1)

import json
import os
import queue
import random
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import rumps
from PyObjCTools.AppHelper import callAfter
from Foundation import NSObject

from version import VERSION, VERSION_NAME, GITHUB_RELEASES_API, GITHUB_RELEASES_URL

# ═══════════════════════════════════════════════════════════
# 配置常量
# ═══════════════════════════════════════════════════════════

API_URL = "https://api.kimi.com/coding/v1/usages"
WORK_API_URL = "https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscription"
APP_NAME = "KimiMonitor"
TOKEN_PATH = Path.home() / ".kimi" / "credentials" / "kimi-code.json"
WORK_KEY_PATH = Path.home() / "Library" / "Application Support" / "kimi-desktop" / "bridge-store" / "token-store.json"
DEVICE_ID_PATH = Path.home() / ".kimi" / "device_id"
CONFIG_PATH = Path.home() / ".kimi_monitor_app_config.json"
SIGNAL_PATH = Path.home() / ".kimi_monitor_refresh_signal"

DEFAULT_CONFIG = {
    "style": "percent_countdown",
    "icon_theme": "default",
    "warn_threshold": 50,      # 正常→告急 边界
    "critical_threshold": 80,  # 告急→警惕 边界
    "refresh_interval": 60,
    "idle_threshold": 300,  # 5分钟空闲认为屏幕无人
    "menubar_source": "code",  # 菜单栏显示: code / work / both
    "colors": {
        "normal": "#FFFFFF",
        "warning": "#FF9500",
        "critical": "#FF3B30",
    },
}

# 4 状态图标：normal / warning / critical / exhausted
ICON_THEMES = {
    "default":     {"normal": "⚪", "warning": "🟡", "critical": "🔴", "exhausted": "⚫"},
    "heart":       {"normal": "🤍", "warning": "💛", "critical": "❤️", "exhausted": "🖤"},
    "fire":        {"normal": "❄️", "warning": "💧", "critical": "🔥", "exhausted": "💀"},
    "diamond":     {"normal": "💎", "warning": "💠", "critical": "🔺", "exhausted": "⚫"},
    "traffic":     {"normal": "🟢", "warning": "🟡", "critical": "🔴", "exhausted": "⛔"},
    "moon":        {"normal": "🌑", "warning": "🌗", "critical": "🌕", "exhausted": "☀️"},
    "battery":     {"normal": "🔋", "warning": "⚡️", "critical": "🪫", "exhausted": "❌"},
}


# ═══════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════

@dataclass
class UsageInfo:
    label: str
    used: int
    limit: int
    percent: int
    reset_text: str
    reset_epoch: Optional[float]


@dataclass
class KimiUsageData:
    weekly: Optional[UsageInfo]
    rate_limit: Optional[UsageInfo]
    work: Optional[UsageInfo] = None
    work_gift: Optional[UsageInfo] = None


# ═══════════════════════════════════════════════════════════
# 配置管理
# ═══════════════════════════════════════════════════════════

class ConfigManager:
    def __init__(self):
        self._config = self._load()

    def _load(self) -> dict:
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return {**DEFAULT_CONFIG, **json.load(f)}
            except Exception:
                pass
        return DEFAULT_CONFIG.copy()

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)

    def get(self, key: str, default=None):
        return self._config.get(key, default)

    def set(self, key: str, value):
        self._config[key] = value
        self.save()


# ═══════════════════════════════════════════════════════════
# 屏幕状态检测
# ═══════════════════════════════════════════════════════════

class ScreenMonitor:
    """检测屏幕状态和系统空闲时间"""

    @staticmethod
    def is_display_asleep() -> bool:
        """检测显示器是否关闭（CGDisplayIsAsleep）"""
        try:
            import Quartz
            display = Quartz.CGMainDisplayID()
            return Quartz.CGDisplayIsAsleep(display) != 0
        except Exception:
            pass
        return False

    @staticmethod
    def get_idle_seconds() -> float:
        """获取系统空闲时间（秒）"""
        try:
            result = subprocess.run(
                ['ioreg', '-c', 'IOHIDSystem'],
                capture_output=True, text=True, timeout=2
            )
            for line in result.stdout.split('\n'):
                if 'HIDIdleTime' in line:
                    parts = line.split('=')
                    if len(parts) >= 2:
                        raw = parts[1].strip()
                        if raw.endswith('n'):
                            raw = raw[:-1]
                        nano = int(raw)
                        return nano / 1_000_000_000
        except Exception:
            pass
        return 0.0


class PowerMonitor:
    """检测电源状态和低电量模式"""

    @staticmethod
    def is_low_power_mode() -> bool:
        """检测是否实际开启低电量模式（pmset -g batt 输出含 lowPowerMode）"""
        try:
            result = subprocess.run(
                ['pmset', '-g', 'batt'],
                capture_output=True, text=True, timeout=2
            )
            return 'lowPowerMode' in result.stdout
        except Exception:
            return False

    @staticmethod
    def is_on_battery() -> bool:
        """检测是否使用电池供电"""
        try:
            result = subprocess.run(
                ['pmset', '-g', 'batt'],
                capture_output=True, text=True, timeout=2
            )
            return 'AC Power' not in result.stdout
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════
# API 客户端（线程安全）
# ═══════════════════════════════════════════════════════════

class KimiAPIClient:
    def __init__(self):
        self._token = None
        self._device_id = None
        self._last_error = None
        self._last_data: Optional[KimiUsageData] = None
        self._last_success_time = 0
        self._lock = threading.Lock()
        # Token 刷新熔断
        self._token_refresh_failures = 0
        self._max_refresh_failures = 3
        # API 失联熔断
        self._consecutive_failures = 0
        self._max_consecutive_failures = 3
        self._silent_mode = False
        # Work 额度错误状态（与 Code 独立容错）
        self._work_error = None

    def _read_token(self) -> Optional[str]:
        if self._token:
            return self._token
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._token = data.get("access_token")
                return self._token
        except Exception as e:
            self._last_error = f"读取 token 失败: {e}"
            return None

    def _refresh_token(self) -> bool:
        """用 refresh_token 换取新 token，成功返回 True"""
        if self._token_refresh_failures >= self._max_refresh_failures:
            self._last_error = "Token 失效，请重新登录 Kimi CLI"
            return False

        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                refresh_token = data.get("refresh_token")
                if not refresh_token:
                    self._last_error = "无 refresh_token，请重新登录"
                    self._token_refresh_failures = self._max_refresh_failures
                    return False

                device_id = self._read_device_id()
                resp = requests.post(
                    "https://auth.kimi.com/api/oauth/token",
                    data={
                        "client_id": "17e5f671-d194-4dfb-9706-5516cb48c098",
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    headers={
                        "User-Agent": f"KimiMonitor/{VERSION}",
                        "X-Msh-Platform": "kimi_cli",
                        "X-Msh-Device-Id": device_id,
                    },
                    timeout=10,
                )

                if resp.status_code == 200:
                    new_data = resp.json()
                    data["access_token"] = new_data.get("access_token", "")
                    data["refresh_token"] = new_data.get("refresh_token", data.get("refresh_token", ""))
                    # 计算 expires_at（当前时间 + expires_in）
                    expires_in = new_data.get("expires_in", 3600)
                    data["expires_at"] = time.time() + expires_in
                    data["expires_in"] = expires_in
                    with open(TOKEN_PATH, "w", encoding="utf-8") as f_out:
                        json.dump(data, f_out, indent=2)
                    self._token = data["access_token"]
                    self._token_refresh_failures = 0
                    self._consecutive_failures = 0
                    self._silent_mode = False
                    print(f"[Token] 自动刷新成功，新 token 有效期 {expires_in}s")
                    return True
                else:
                    self._token_refresh_failures += 1
                    try:
                        err_body = resp.json()
                        err_desc = err_body.get("error_description", f"HTTP {resp.status_code}")
                    except Exception:
                        err_desc = f"HTTP {resp.status_code}"
                    if self._token_refresh_failures >= self._max_refresh_failures:
                        self._last_error = "Token 失效，请重新登录 Kimi CLI"
                    else:
                        self._last_error = f"刷新失败 ({self._token_refresh_failures}/{self._max_refresh_failures}): {err_desc}"
                    return False

        except Exception as e:
            self._token_refresh_failures += 1
            if self._token_refresh_failures >= self._max_refresh_failures:
                self._last_error = "Token 失效，请重新登录 Kimi CLI"
            else:
                self._last_error = f"刷新失败: {e}"
            return False

    def _read_device_id(self) -> str:
        if self._device_id:
            return self._device_id
        try:
            with open(DEVICE_ID_PATH, "r", encoding="utf-8") as f:
                self._device_id = f.read().strip()
                return self._device_id
        except Exception:
            return "unknown"

    def _parse_reset_time(self, reset_time_str: str) -> tuple[str, Optional[float]]:
        try:
            if "." in reset_time_str and reset_time_str.endswith("Z"):
                base, frac = reset_time_str[:-1].split(".")
                frac = frac[:6]
                reset_time_str = f"{base}.{frac}Z"
            dt = datetime.fromisoformat(reset_time_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = dt - now
            seconds = int(delta.total_seconds())
            if seconds <= 0:
                return "已重置", None
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            if hours >= 24:
                days = hours // 24
                rem = hours % 24
                if rem > 0:
                    return f"{days}天{rem}小时后", dt.timestamp()
                return f"{days}天后", dt.timestamp()
            if hours > 0:
                if mins > 0:
                    return f"{hours}h{mins}m后", dt.timestamp()
                return f"{hours}h后", dt.timestamp()
            elif mins > 0:
                return f"{mins}m后", dt.timestamp()
            else:
                return "即将", dt.timestamp()
        except Exception:
            return "未知", None

    def fetch(self) -> Optional[KimiUsageData]:
        # 检查 token 是否快过期，提前刷新
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                expires_at = data.get("expires_at")
                if expires_at and time.time() >= expires_at - 300:
                    self._refresh_token()
        except Exception:
            pass

        token = self._read_token()
        if not token:
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Msh-Platform": "kimi_cli",
            "X-Msh-Device-Id": self._read_device_id(),
            "User-Agent": f"KimiMonitor/{VERSION}",
        }

        try:
            resp = requests.get(API_URL, headers=headers, timeout=(3, 10))
            if resp.status_code == 401:
                # Token 过期，尝试自动刷新一次
                if self._refresh_token():
                    token = self._read_token()
                    headers["Authorization"] = f"Bearer {token}"
                    resp = requests.get(API_URL, headers=headers, timeout=(3, 10))
                    if resp.status_code == 200:
                        payload = resp.json()
                        data = self._parse_payload(payload)
                        with self._lock:
                            self._last_data = data
                            self._last_success_time = time.time()
                            self._last_error = None
                            self._consecutive_failures = 0
                        return data
                # 刷新失败或重试仍失败
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_consecutive_failures:
                    self._silent_mode = True
                    self._last_error = "失去连接"
                return None
            elif resp.status_code == 429:
                self._consecutive_failures += 1
                self._last_error = "请求太频繁，被限流了"
                if self._consecutive_failures >= self._max_consecutive_failures:
                    self._silent_mode = True
                    self._last_error = "失去连接"
                return None
            elif resp.status_code != 200:
                self._consecutive_failures += 1
                self._last_error = f"API 错误: HTTP {resp.status_code}"
                if self._consecutive_failures >= self._max_consecutive_failures:
                    self._silent_mode = True
                    self._last_error = "失去连接"
                return None

            payload = resp.json()
            data = self._parse_payload(payload)
            with self._lock:
                self._last_data = data
                self._last_success_time = time.time()
                self._last_error = None
                self._consecutive_failures = 0
                self._silent_mode = False
            return data

        except requests.Timeout:
            self._consecutive_failures += 1
            self._last_error = "请求超时"
            if self._consecutive_failures >= self._max_consecutive_failures:
                self._silent_mode = True
                self._last_error = "失去连接"
        except requests.RequestException as e:
            self._consecutive_failures += 1
            self._last_error = f"网络错误: {e}"
            if self._consecutive_failures >= self._max_consecutive_failures:
                self._silent_mode = True
                self._last_error = "失去连接"
        except Exception as e:
            self._consecutive_failures += 1
            self._last_error = f"解析错误: {e}"
            if self._consecutive_failures >= self._max_consecutive_failures:
                self._silent_mode = True
                self._last_error = "失去连接"
        
        with self._lock:
            return self._last_data

    @staticmethod
    def _parse_used(detail: dict) -> tuple[int, int]:
        """兼容 used / remaining 两种返回字段（接口字段为字符串数字），返回 (used, limit)"""
        limit = int(detail.get("limit", 0) or 0)
        used_raw = detail.get("used")
        if used_raw is not None:
            used = int(used_raw or 0)
        else:
            used = max(0, limit - int(detail.get("remaining", 0) or 0))
        return used, limit

    def _parse_payload(self, payload: dict) -> KimiUsageData:
        weekly = None
        rate_limit = None

        usage = payload.get("usage")
        if isinstance(usage, dict):
            used, limit = self._parse_used(usage)
            reset_str = usage.get("resetTime", "")
            reset_text, reset_epoch = self._parse_reset_time(reset_str)
            percent = min(int(used * 100 / limit), 100) if limit > 0 else 0
            weekly = UsageInfo(
                label="CLI 周调用配额", used=used, limit=limit, percent=percent,
                reset_text=reset_text, reset_epoch=reset_epoch,
            )

        limits = payload.get("limits", [])
        if limits and isinstance(limits[0], dict):
            detail = limits[0].get("detail", {})
            used, limit = self._parse_used(detail)
            reset_str = detail.get("resetTime", "")
            reset_text, reset_epoch = self._parse_reset_time(reset_str)
            percent = min(int(used * 100 / limit), 100) if limit > 0 else 0
            rate_limit = UsageInfo(
                label="5分钟频限", used=used, limit=limit, percent=percent,
                reset_text=reset_text, reset_epoch=reset_epoch,
            )

        return KimiUsageData(weekly=weekly, rate_limit=rate_limit)

    def fetch_work(self) -> tuple[Optional[UsageInfo], Optional[UsageInfo]]:
        """拉取 Kimi Work 订阅主额度（GetSubscription），返回 (主额度, 赠送额度)；
        桌面端凭证文件不存在（未安装/未登录）时静默返回 (None, None)"""
        if not WORK_KEY_PATH.exists():
            return None, None
        try:
            with open(WORK_KEY_PATH, "r", encoding="utf-8") as f:
                token = json.load(f).get("tokens", {}).get("access_token")
            if not token:
                self._work_error = "凭证无效"
                return None, None
            resp = requests.post(
                WORK_API_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": f"KimiMonitor/{VERSION}",
                },
                json={},
                timeout=(3, 10),
            )
            if resp.status_code in (401, 403):
                # web token 失效（桌面端会自行刷新该文件，下一轮自动恢复）：静默降级
                self._work_error = "授权失效"
                return None, None
            if resp.status_code != 200:
                self._work_error = f"HTTP {resp.status_code}"
                return None, None

            main_info = None
            gift_info = None
            for bal in resp.json().get("balances", []):
                ratio = bal.get("amountUsedRatio")
                if ratio is None:
                    continue
                percent = min(int(float(ratio) * 100 + 0.5), 100)
                reset_text, reset_epoch = self._parse_reset_time(bal.get("expireTime", ""))
                bal_type = bal.get("type")
                label = "Work 月度额度" if bal_type == "SUBSCRIPTION" else "Work 赠送额度"
                info = UsageInfo(
                    label=label, used=percent, limit=100, percent=percent,
                    reset_text=reset_text, reset_epoch=reset_epoch,
                )
                if bal_type == "SUBSCRIPTION":
                    main_info = info
                elif bal_type == "GIFT":
                    gift_info = info
            self._work_error = None if main_info else "无订阅额度"
            return main_info, gift_info
        except requests.Timeout:
            self._work_error = "请求超时"
        except Exception as e:
            self._work_error = f"{e}"
        return None, None

    def get_last_error(self) -> Optional[str]:
        return self._last_error


# ═══════════════════════════════════════════════════════════
# 主应用
# ═══════════════════════════════════════════════════════════

class UIUpdater(NSObject):
    """用于在主线程执行 UI 更新的 NSObject 包装器"""
    def initWithApp_(self, app):
        self.app = app
        return self
    def updateUI_(self, data):
        self.app._update_ui(data)


class KimiMonitorApp(rumps.App):
    def run(self, **options):
        result = super().run(**options)
        return result
    def __init__(self):
        super().__init__(
            name="KimiMonitor",
            title="⏳ 启动中",
            icon=None,
            quit_button="退出",
        )
        self.config = ConfigManager()
        self.api = KimiAPIClient()
        self._result_queue = queue.Queue()
        self._ui_updater = UIUpdater.alloc().initWithApp_(self)
        self._style_items = {}
        self._theme_items = {}
        self._threshold_items = {}
        self._menubar_items = {}
        self._running = True
        
        # 智能刷新状态
        self._sleeping = False
        self._screen_active = True
        self._screen_locked_by_notification = False
        self._last_signal_mtime = 0
        self._app_start_time = time.time()
        
        self._build_menu()
        
        # 注册系统事件
        self._register_system_events()
        
        # 启动后台工作线程
        self._refresh_thread = threading.Thread(target=self._refresh_worker, daemon=True)
        self._refresh_thread.start()
        
        self._schedule_thread = threading.Thread(target=self._schedule_worker, daemon=True)
        self._schedule_thread.start()
        
        self._screen_thread = threading.Thread(target=self._screen_monitor_worker, daemon=True)
        self._screen_thread.start()
        
        # 修正旧配置：警惕阈值不应超过 90（100% 是耗尽状态）
        if self.config.get("critical_threshold", 80) > 90:
            self.config.set("critical_threshold", 90)
        
        # 立即触发第一次刷新
        self._trigger_refresh()

    def _build_menu(self):
        """构建下拉菜单"""
        self.menu.clear()
        self.menu.add(rumps.MenuItem("📊 CLI 周调用配额"))
        self.menu.add(rumps.MenuItem("📊 5分钟频限"))
        self.menu.add(rumps.MenuItem("📊 Work 月度额度"))
        self.menu.add(rumps.MenuItem("🎁 Work 赠送额度"))
        self.menu.add(None)
        self.menu.add(rumps.MenuItem("🔄 立即刷新", callback=self.on_refresh))
        self.menu.add(None)
        
        style_menu = rumps.MenuItem("⚙️ 显示风格")
        self._style_items["percent"] = rumps.MenuItem("百分比", callback=lambda _: self.set_style("percent"))
        self._style_items["percent_countdown"] = rumps.MenuItem("百分比+倒计时", callback=lambda _: self.set_style("percent_countdown"))
        self._style_items["remaining_countdown"] = rumps.MenuItem("剩余+倒计时", callback=lambda _: self.set_style("remaining_countdown"))
        self._style_items["progress"] = rumps.MenuItem("进度条", callback=lambda _: self.set_style("progress"))
        for item in self._style_items.values():
            style_menu.add(item)
        self.menu.add(style_menu)
        
        theme_menu = rumps.MenuItem("🎨 图标主题")
        warn_th = self.config.get("warn_threshold", 50)
        crit_th = self.config.get("critical_threshold", 80)
        theme_labels = {
            "default":  f"默认    ⚪(<{warn_th}%) 🟡(≥{warn_th}%) 🔴(≥{crit_th}%) ⚫(100%)",
            "heart":    f"爱心    🤍(<{warn_th}%) 💛(≥{warn_th}%) ❤️(≥{crit_th}%) 🖤(100%)",
            "fire":     f"冰火    ❄️(<{warn_th}%) 💧(≥{warn_th}%) 🔥(≥{crit_th}%) 💀(100%)",
            "diamond":  f"钻石    💎(<{warn_th}%) 💠(≥{warn_th}%) 🔺(≥{crit_th}%) ⚫(100%)",
            "traffic":  f"交通    🟢(<{warn_th}%) 🟡(≥{warn_th}%) 🔴(≥{crit_th}%) ⛔(100%)",
            "moon":     f"月相    🌑(<{warn_th}%) 🌗(≥{warn_th}%) 🌕(≥{crit_th}%) ☀️(100%)",
            "battery":  f"电池    🔋(<{warn_th}%) ⚡️(≥{warn_th}%) 🪫(≥{crit_th}%) ❌(100%)",
        }
        for key, label in theme_labels.items():
            self._theme_items[key] = rumps.MenuItem(label, callback=lambda _, k=key: self.set_icon_theme(k))
            theme_menu.add(self._theme_items[key])
        self.menu.add(theme_menu)

        # 菜单栏显示来源：仅 Code / 仅 Work / 双显
        menubar_menu = rumps.MenuItem("🖥 菜单栏显示")
        for key, label in [("code", "仅 Code 频限"), ("work", "仅 Work 额度"), ("both", "Code + Work 双显")]:
            self._menubar_items[key] = rumps.MenuItem(label, callback=lambda _, k=key: self.set_menubar_source(k))
            menubar_menu.add(self._menubar_items[key])
        self.menu.add(menubar_menu)
        
        # 阈值设置（滑块 + 刻度 + 轨道色）
        threshold_menu = rumps.MenuItem("⚠️ 阈值设置")
        warn_th = self.config.get("warn_threshold", 50)
        crit_th = self.config.get("critical_threshold", 80)
        self._threshold_items["warn_label"] = rumps.MenuItem("告急阈值", callback=lambda _: None)
        self._threshold_items["warn_label"].title = f"告急阈值: {warn_th}%"
        threshold_menu.add(self._threshold_items["warn_label"])
        self._warn_slider = rumps.SliderMenuItem(
            value=warn_th, min_value=10, max_value=90,
            callback=self._on_warn_slider_change,
            dimensions=(180, 22)
        )
        # 9 个刻度（10% 步进），只允许停在刻度上
        self._warn_slider._slider.setNumberOfTickMarks_(9)
        self._warn_slider._slider.setAllowsTickMarkValuesOnly_(True)
        # 轨道颜色：橙色（与告急状态呼应）
        from AppKit import NSColor
        self._warn_slider._slider.setTrackFillColor_(NSColor.systemOrangeColor())
        threshold_menu.add(self._warn_slider)
        threshold_menu.add(None)
        self._threshold_items["crit_label"] = rumps.MenuItem("警惕阈值", callback=lambda _: None)
        self._threshold_items["crit_label"].title = f"警惕阈值: {crit_th}%"
        threshold_menu.add(self._threshold_items["crit_label"])
        self._crit_slider = rumps.SliderMenuItem(
            value=min(crit_th, 90), min_value=20, max_value=90,
            callback=self._on_crit_slider_change,
            dimensions=(180, 22)
        )
        self._crit_slider._slider.setNumberOfTickMarks_(9)
        self._crit_slider._slider.setAllowsTickMarkValuesOnly_(True)
        # 轨道颜色：红色（与警惕状态呼应）
        self._crit_slider._slider.setTrackFillColor_(NSColor.systemRedColor())
        threshold_menu.add(self._crit_slider)
        self.menu.add(threshold_menu)
        
        self.menu.add(None)
        self.menu.add(rumps.MenuItem("📋 打开开发日志", callback=self.on_open_log))
        self.menu.add(None)
        self.menu.add(rumps.MenuItem(f"ℹ️ 关于 KimiMonitor v{VERSION}", callback=self.on_about))
        self.menu.add(rumps.MenuItem("⬆️ 检查更新", callback=self.on_check_update))

    def _register_system_events(self):
        """注册系统睡眠/唤醒事件，以及屏幕锁定/解锁通知"""
        @rumps.events.on_sleep
        def on_sleep():
            self._sleeping = True
            print("[System] 系统睡眠，暂停刷新")
        
        @rumps.events.on_wake
        def on_wake():
            self._sleeping = False
            print("[System] 系统唤醒，立即刷新")
            self._trigger_refresh()
        
        # 注册 macOS 屏幕锁定/解锁分布式通知
        self._register_screen_lock_notifications()

    def _trigger_refresh(self):
        """向队列放入刷新请求"""
        try:
            self._result_queue.put_nowait("refresh")
        except queue.Full:
            pass

    def _refresh_worker(self):
        """后台线程：监听刷新请求，执行网络请求，通过 performSelectorOnMainThread 更新 UI"""
        while self._running:
            try:
                msg = self._result_queue.get(timeout=1)
                if msg == "refresh":
                    data = self.api.fetch()
                    work, work_gift = self.api.fetch_work()
                    if data is None and (work or work_gift):
                        data = KimiUsageData(weekly=None, rate_limit=None)
                    if data is not None:
                        data.work = work
                        data.work_gift = work_gift
                    # 等待 rumps.App.run() 初始化 _nsapp 并进入事件循环
                    while self._running and (not hasattr(self, '_nsapp') or self._nsapp is None):
                        time.sleep(0.1)
                    # 再稍等片刻确保主线程事件循环已启动
                    time.sleep(0.3)
                    self._ui_updater.performSelectorOnMainThread_withObject_waitUntilDone_(
                        'updateUI:', data, True)
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[Worker Error] {e}")

    def _schedule_worker(self):
        """后台线程：动态间隔自动刷新，低电量/静默模式时延长间隔"""
        while self._running:
            if self.api._silent_mode:
                interval = 300  # 静默模式 5 分钟
                if not getattr(self, '_last_silent_logged', False):
                    print(f"[Silent] 进入静默模式，刷新间隔延长至 {interval}s")
                    self._last_silent_logged = True
            elif PowerMonitor.is_low_power_mode():
                interval = 180 + random.randint(0, 60)
                if not getattr(self, '_last_low_power_logged', False):
                    print(f"[Power] 低电量模式，刷新间隔延长至 {interval}s")
                    self._last_low_power_logged = True
                self._last_silent_logged = False
            else:
                interval = 60 + random.randint(0, 10)
                if getattr(self, '_last_low_power_logged', False):
                    print(f"[Power] 恢复正常模式，刷新间隔 {interval}s")
                    self._last_low_power_logged = False
                self._last_silent_logged = False
            time.sleep(interval)
            if not self._sleeping and self._screen_active:
                self._trigger_refresh()

    def _register_screen_lock_notifications(self):
        """注册 macOS 屏幕锁定/解锁通知"""
        try:
            from Foundation import NSObject, NSDistributedNotificationCenter
            
            class ScreenLockObserver(NSObject):
                def initWithApp_(self, app):
                    self.app = app
                    return self
                
                def screenLocked_(self, notification):
                    if time.time() - self.app._app_start_time < 3:
                        return
                    if self.app._screen_locked_by_notification:
                        return
                    print("[Screen] 屏幕锁定")
                    self.app._screen_locked_by_notification = True
                    self.app._screen_active = False
                
                def screenUnlocked_(self, notification):
                    if time.time() - self.app._app_start_time < 3:
                        return
                    if not self.app._screen_locked_by_notification:
                        return
                    print("[Screen] 屏幕解锁，立即刷新")
                    self.app._screen_locked_by_notification = False
                    self.app._screen_active = True
                    self.app._trigger_refresh()
            
            self._screen_observer = ScreenLockObserver.alloc().initWithApp_(self)
            nc = NSDistributedNotificationCenter.defaultCenter()
            nc.addObserver_selector_name_object_(
                self._screen_observer, 'screenLocked:', 'com.apple.screenIsLocked', None
            )
            nc.addObserver_selector_name_object_(
                self._screen_observer, 'screenUnlocked:', 'com.apple.screenIsUnlocked', None
            )
            print("[Screen] 屏幕锁定通知已注册")
        except Exception as e:
            print(f"[Screen] 注册屏幕通知失败: {e}")

    def _screen_monitor_worker(self):
        """后台线程：每 10 秒检测显示器状态和 kimi 命令信号"""
        while self._running:
            time.sleep(10)
            
            # 检测显示器关闭状态（作为屏幕锁定的补充）
            was_active = self._screen_active
            display_asleep = ScreenMonitor.is_display_asleep()
            idle = ScreenMonitor.get_idle_seconds()
            idle_threshold = self.config.get("idle_threshold", 300)
            
            # 如果屏幕被通知锁定，不要通过空闲检测恢复
            if self._screen_locked_by_notification:
                if self._screen_active:
                    print("[Screen] 屏幕已锁定，暂停刷新")
                self._screen_active = False
            elif display_asleep or idle > idle_threshold:
                if self._screen_active:
                    print("[Screen] 显示器关闭或长时间无操作，暂停刷新")
                self._screen_active = False
            else:
                if not self._screen_active:
                    print("[Screen] 显示器恢复，立即刷新")
                    self._trigger_refresh()
                self._screen_active = True
            
            # 检测 kimi 命令完成信号
            self._check_command_signal()

    def _check_command_signal(self):
        """检查是否有 kimi 命令完成的信号"""
        try:
            if SIGNAL_PATH.exists():
                mtime = SIGNAL_PATH.stat().st_mtime
                if mtime > self._last_signal_mtime:
                    self._last_signal_mtime = mtime
                    # 信号在 5 秒内才响应，避免处理旧信号
                    if time.time() - mtime < 5:
                        print("[Signal] 检测到 kimi 命令完成，立即刷新")
                        self._trigger_refresh()
        except Exception as e:
            print(f"[Signal Error] {e}")

    def on_refresh(self, _):
        """手动刷新"""
        # 重置所有熔断计数器
        self.api._consecutive_failures = 0
        self.api._token_refresh_failures = 0
        self.api._silent_mode = False
        self._set_title("⏳ 刷新中...")
        self._trigger_refresh()

    def set_style(self, style: str):
        self.config.set("style", style)
        self._update_ui(self.api._last_data)

    def set_icon_theme(self, theme: str):
        self.config.set("icon_theme", theme)
        self._update_ui(self.api._last_data)

    def set_menubar_source(self, source: str):
        self.config.set("menubar_source", source)
        self._update_ui(self.api._last_data)

    def _on_warn_slider_change(self, slider):
        """告急阈值滑块变化回调"""
        new_val = int(slider.value)
        crit = self.config.get("critical_threshold", 80)
        if new_val >= crit:
            new_val = max(10, crit - 10)
            slider.value = new_val
        self.config.set("warn_threshold", new_val)
        self._threshold_items["warn_label"].title = f"告急阈值: {new_val}%"
        self._update_title(self.api._last_data)
    
    def _on_crit_slider_change(self, slider):
        """警惕阈值滑块变化回调"""
        new_val = int(slider.value)
        warn = self.config.get("warn_threshold", 50)
        if new_val <= warn:
            new_val = min(90, warn + 10)
            slider.value = new_val
        self.config.set("critical_threshold", new_val)
        self._threshold_items["crit_label"].title = f"警惕阈值: {new_val}%"
        self._update_title(self.api._last_data)

    def on_open_log(self, _):
        log_path = Path.home() / "KimiBackups" / "devlogs" / "KimiMonitor_DevLog.md"
        os.system(f'open "{log_path}"')

    def on_about(self, _):
        """显示关于对话框"""
        rumps.alert(
            title=f"关于 {APP_NAME}",
            message=f"版本: {VERSION} ({VERSION_NAME})\n\n"
                    f"KimiMonitor 是一款 macOS 菜单栏应用，\n"
                    f"用于实时监控 Kimi Code CLI 的频限使用情况。\n\n"
                    f"技术栈: Python + rumps + PyObjC",
            ok="确定"
        )

    def on_check_update(self, _):
        """检查 GitHub 最新版本"""
        import threading
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        """后台检查更新"""
        try:
            resp = requests.get(
                GITHUB_RELEASES_API,
                headers={"User-Agent": f"KimiMonitor/{VERSION}"},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                latest = data.get("tag_name", "")
                if latest.startswith("v"):
                    latest = latest[1:]
                if self._version_newer(latest, VERSION):
                    self._set_title("⬆️ 有更新")
                    rumps.notification(
                        title="KimiMonitor 有更新",
                        subtitle=f"最新版本: v{latest}",
                        message=f"当前版本: v{VERSION}，点击前往下载",
                        sound=False,
                    )
                    # 用户点击通知后打开 releases 页面
                    os.system(f'open "{GITHUB_RELEASES_URL}"')
                else:
                    rumps.notification(
                        title="KimiMonitor",
                        subtitle="已是最新版本",
                        message=f"当前版本 v{VERSION}",
                        sound=False,
                    )
            else:
                rumps.notification(
                    title="KimiMonitor",
                    subtitle="检查更新失败",
                    message="无法连接到 GitHub API",
                    sound=False,
                )
        except Exception as e:
            rumps.notification(
                title="KimiMonitor",
                subtitle="检查更新失败",
                message=str(e),
                sound=False,
            )

    @staticmethod
    def _version_newer(latest: str, current: str) -> bool:
        """比较版本号，latest > current 返回 True"""
        try:
            l = [int(x) for x in latest.split(".")]
            c = [int(x) for x in current.split(".")]
            for i in range(max(len(l), len(c))):
                lv = l[i] if i < len(l) else 0
                cv = c[i] if i < len(c) else 0
                if lv > cv:
                    return True
                if lv < cv:
                    return False
            return False
        except Exception:
            return False

    def _update_ui(self, data: Optional[KimiUsageData]):
        """更新菜单栏标题和下拉菜单（在主线程中被 performSelectorOnMainThread 调用）"""
        self._update_title(data)
        self._update_menu(data)

    def _set_title(self, text: str, color: Optional[str] = None):
        """设置菜单栏标题，支持颜色。低电量模式使用黄色。"""
        self._title = text
        if not hasattr(self, '_nsapp') or self._nsapp is None:
            return
        nsitem = self._nsapp.nsstatusitem
        btn = nsitem.button() if hasattr(nsitem, 'button') else None
        
        if color == "yellow":
            from AppKit import NSAttributedString, NSColor
            from Foundation import NSDictionary
            yellow = NSColor.systemYellowColor()
            attrs = NSDictionary.dictionaryWithObject_forKey_(yellow, "NSColor")
            attr_str = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            if btn:
                btn.setAttributedTitle_(attr_str)
            else:
                nsitem.setAttributedTitle_(attr_str)
        else:
            # 使用 rumps 原生 title setter + button 直接设置双保险
            self.title = text
            if btn:
                btn.setTitle_(text)
                btn.setNeedsDisplay_(True)
            else:
                nsitem.setTitle_(text)
                nsitem.setNeedsDisplay_(True)
        # macOS 15: 强制刷新 status item 显示
        if hasattr(nsitem, 'setVisible_'):
            nsitem.setVisible_(True)

    def _icon_for(self, percent: int) -> str:
        """按当前主题和阈值返回状态图标"""
        theme_key = self.config.get("icon_theme", "default")
        theme = ICON_THEMES.get(theme_key, ICON_THEMES["default"])
        warn_th = self.config.get("warn_threshold", 50)
        crit_th = self.config.get("critical_threshold", 80)
        if theme_key == "moon":
            # 彩蛋：8 月相盈亏周期，暗合月之暗面
            moon_phases = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
            return moon_phases[min(7, int(percent * 8 / 100))]
        if percent >= 100:
            return theme["exhausted"]
        if percent >= crit_th:
            return theme["critical"]
        if percent >= warn_th:
            return theme["warning"]
        return theme["normal"]

    def _styled_text(self, info: UsageInfo) -> str:
        """按当前显示风格格式化一条额度数据（不含图标）"""
        style = self.config.get("style", "percent_countdown")
        if style == "percent":
            return f"{info.percent}%"
        if style == "percent_countdown":
            if info.reset_text and info.reset_text != "已重置":
                return f"{info.percent}% · {info.reset_text}"
            return f"{info.percent}%"
        if style == "remaining_countdown":
            remaining = max(0, 100 - info.percent)
            if info.reset_text and info.reset_text != "已重置":
                return f"剩{remaining}% · {info.reset_text}"
            return f"剩{remaining}%"
        if style == "progress":
            return self._progress_bar(info.percent)
        return f"{info.percent}%"

    def _update_title(self, data: Optional[KimiUsageData]):
        source = self.config.get("menubar_source", "code")

        # 熔断状态优先显示（仅 Code 来源时）
        if source == "code":
            if self.api._silent_mode and self.api._consecutive_failures >= 3:
                self._set_title("⚠️ 失去连接")
                return
            if self.api._token_refresh_failures >= 3:
                self._set_title("⚠️ Token 失效")
                return

        code_info = data.rate_limit if data else None
        work_info = data.work if data else None

        if source == "work":
            if not work_info:
                err = self.api._work_error
                self._set_title(f"⚠️ {err[:8]}" if err else "⚠️ --")
                return
            title = f"{self._icon_for(work_info.percent)} {self._styled_text(work_info)}"
        elif source == "both":
            parts = []
            if code_info:
                parts.append(f"{self._icon_for(code_info.percent)} {self._styled_text(code_info)}")
            if work_info:
                parts.append(f"{self._icon_for(work_info.percent)} {self._styled_text(work_info)}")
            if not parts:
                error = self.api.get_last_error() or self.api._work_error
                self._set_title(f"⚠️ {error[:8]}" if error else "⚠️ --")
                return
            title = " · ".join(parts)
        else:
            if not code_info:
                error = self.api.get_last_error()
                self._set_title(f"⚠️ {error[:8]}" if error else "⚠️ --")
                return
            title = f"{self._icon_for(code_info.percent)} {self._styled_text(code_info)}"

        # 低电量模式：标题变为黄色
        color = "yellow" if PowerMonitor.is_low_power_mode() else None
        self._set_title(title, color)

    def _update_menu(self, data: Optional[KimiUsageData]):
        if data and data.weekly:
            w = data.weekly
            self.menu["📊 CLI 周调用配额"].title = f"📊 CLI 周调用配额: {w.used}/{w.limit} ({w.percent}%) · {w.reset_text}"
        else:
            self.menu["📊 CLI 周调用配额"].title = "📊 CLI 周调用配额: --"

        if data and data.rate_limit:
            rl = data.rate_limit
            self.menu["📊 5分钟频限"].title = f"📊 5分钟频限: {rl.used}/{rl.limit} ({rl.percent}%) · {rl.reset_text}"
        else:
            error = self.api.get_last_error()
            self.menu["📊 5分钟频限"].title = f"📊 5分钟频限: {error or '--'}"

        # 未安装 Kimi Work（无凭证文件）时整行隐藏
        try:
            hidden = not WORK_KEY_PATH.exists()
            self.menu["📊 Work 月度额度"]._menuitem.setHidden_(hidden)
            self.menu["🎁 Work 赠送额度"]._menuitem.setHidden_(hidden or not (data and data.work_gift))
        except Exception:
            pass
        if data and data.work:
            w = data.work
            self.menu["📊 Work 月度额度"].title = f"📊 Work 月度额度: {w.percent}% · {w.reset_text}"
        else:
            self.menu["📊 Work 月度额度"].title = f"📊 Work 月度额度: {self.api._work_error or '--'}"
        if data and data.work_gift:
            g = data.work_gift
            self.menu["🎁 Work 赠送额度"].title = f"🎁 Work 赠送额度: {g.percent}% · {g.reset_text}"

        source = self.config.get("menubar_source", "code")
        for key, item in self._menubar_items.items():
            item.state = 1 if key == source else 0

        style = self.config.get("style", "percent_countdown")
        for key, item in self._style_items.items():
            item.state = 1 if key == style else 0
        
        theme = self.config.get("icon_theme", "default")
        for key, item in self._theme_items.items():
            item.state = 1 if key == theme else 0
        
        warn_th = self.config.get("warn_threshold", 50)
        crit_th = self.config.get("critical_threshold", 80)
        self._threshold_items["warn_label"].title = f"告急阈值: {warn_th}%"
        self._threshold_items["crit_label"].title = f"警惕阈值: {crit_th}%"

    def _progress_bar(self, percent: int, width: int = 10) -> str:
        filled = percent * width // 100
        filled = min(filled, width)
        empty = width - filled
        return "█" * filled + "░" * empty


if __name__ == "__main__":
    app = KimiMonitorApp()
    app.run()
