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

import base64
import fcntl
import json
import os
import queue
import random
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, TextIO

import requests
import rumps
from PyObjCTools.AppHelper import callAfter
from Foundation import NSObject

from version import VERSION, VERSION_NAME, GITHUB_RELEASES_API, GITHUB_RELEASES_URL

# ═══════════════════════════════════════════════════════════
# 配置常量
# ═══════════════════════════════════════════════════════════

API_URL = "https://api.kimi.com/coding/v1/usages"
WORK_API_URL = "https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats"
APP_NAME = "KimiMonitor"
TOKEN_PATH = Path.home() / ".kimi" / "credentials" / "kimi-code.json"
WORK_DESKTOP_DIR = Path.home() / "Library" / "Application Support" / "kimi-desktop"
WORK_LEVELDB_DIR = WORK_DESKTOP_DIR / "Local Storage" / "leveldb"
WORK_COOKIE_DB = WORK_DESKTOP_DIR / "Cookies"
WORK_JWT_PATTERN = re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
LEVELDB_MAGIC = b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"
DEVICE_ID_PATH = Path.home() / ".kimi" / "device_id"
CONFIG_PATH = Path.home() / ".kimi_monitor_app_config.json"
SIGNAL_PATH = Path.home() / ".kimi_monitor_refresh_signal"
INSTANCE_LOCK_PATH = Path.home() / ".kimi_monitor_app.lock"


def acquire_single_instance_lock() -> Optional[TextIO]:
    """持有跨路径的文件锁；返回的文件对象必须在应用生命周期内保持打开。"""
    lock_file = INSTANCE_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"{os.getpid()}\n")
    lock_file.flush()
    return lock_file


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """读取 LevelDB 使用的变长整数。"""
    result = 0
    shift = 0
    while True:
        if offset >= len(data) or shift >= 64:
            raise ValueError("LevelDB varint 无效")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, offset
        shift += 7


def _decompress_snappy(data: bytes) -> bytes:
    """解压 LevelDB 数据块使用的原始 Snappy 格式。"""
    expected, offset = _read_varint(data, 0)
    output = bytearray()
    while offset < len(data):
        tag = data[offset]
        offset += 1
        kind = tag & 3
        if kind == 0:
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                count = length - 59
                length = int.from_bytes(data[offset:offset + count], "little") + 1
                offset += count
            output.extend(data[offset:offset + length])
            offset += length
            continue
        if kind == 1:
            length = 4 + ((tag >> 2) & 7)
            distance = ((tag >> 5) << 8) | data[offset]
            offset += 1
        elif kind == 2:
            length = 1 + (tag >> 2)
            distance = int.from_bytes(data[offset:offset + 2], "little")
            offset += 2
        else:
            length = 1 + (tag >> 2)
            distance = int.from_bytes(data[offset:offset + 4], "little")
            offset += 4
        if distance <= 0 or distance > len(output):
            raise ValueError("Snappy 回引无效")
        for _ in range(length):
            output.append(output[-distance])
    if len(output) != expected:
        raise ValueError("Snappy 解压长度不匹配")
    return bytes(output)


def _leveldb_block(content: bytes, offset: int, size: int) -> bytes:
    """读取一个 LevelDB 块，并按需解压。"""
    if offset < 0 or size < 0 or offset + size + 5 > len(content):
        raise ValueError("LevelDB 块越界")
    raw = content[offset:offset + size]
    compression = content[offset + size]
    if compression == 0:
        return raw
    if compression == 1:
        return _decompress_snappy(raw)
    raise ValueError(f"未知 LevelDB 压缩类型: {compression}")


def _leveldb_handle(data: bytes, offset: int) -> tuple[int, int, int]:
    block_offset, offset = _read_varint(data, offset)
    block_size, offset = _read_varint(data, offset)
    return block_offset, block_size, offset


def _leveldb_entries(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    """遍历 LevelDB 索引块中的键值项。"""
    offset = 0
    previous = b""
    while offset < len(data):
        shared, offset = _read_varint(data, offset)
        unshared, offset = _read_varint(data, offset)
        value_length, offset = _read_varint(data, offset)
        if shared == unshared == value_length == 0:
            return
        key = previous[:shared] + data[offset:offset + unshared]
        offset += unshared
        value = data[offset:offset + value_length]
        offset += value_length
        yield key, value
        previous = key


def _read_sstable_blocks(path: Path) -> Iterator[bytes]:
    """只读遍历 SSTable 的数据块；CRC 不影响本机令牌元数据筛选。"""
    content = path.read_bytes()
    if len(content) < 48 or content[-8:] != LEVELDB_MAGIC:
        raise ValueError("不是有效的 LevelDB SSTable")
    footer = content[-48:-8]
    _, _, cursor = _leveldb_handle(footer, 0)
    index_offset, index_size, _ = _leveldb_handle(footer, cursor)
    index = _leveldb_block(content, index_offset, index_size)
    for _, value in _leveldb_entries(index):
        data_offset, data_size, _ = _leveldb_handle(value, 0)
        yield _leveldb_block(content, data_offset, data_size)


DEFAULT_CONFIG = {
    "style": "percent_countdown",
    "refresh_interval": 60,
    "idle_threshold": 300,  # 5分钟空闲认为屏幕无人
    "menubar_source": "code",  # 菜单栏显示: code / work / both / freq_and_total
    "colors": {
        "normal": "#FFFFFF",
        "warning": "#FF9500",
        "critical": "#FF3B30",
    },
}

# 月相图标：5 相渐盈铺满 0-100%，额度用满即满月（暗合月之暗面）
MOON_PHASES = ["🌑", "🌒", "🌓", "🌔", "🌕"]


# ═══════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════

@dataclass
class UsageInfo:
    label: str
    used: int
    limit: int
    percent: float
    reset_text: str
    reset_epoch: Optional[float]


@dataclass
class KimiUsageData:
    weekly: Optional[UsageInfo]
    rate_limit: Optional[UsageInfo]
    work: Optional[UsageInfo] = None        # Work 本月消耗（总量 - Code）
    code_month: Optional[UsageInfo] = None  # Code 本月消耗


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
        # Work 唤起熔断：限制自动唤起 Kimi Desktop 的频率
        self._work_relaunch_count = 0
        self._work_max_relaunch = 3
        self._work_last_relaunch_time = 0
        self._work_relaunch_cooldown = 600  # 10 分钟内最多唤起一次

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
            percent = min(used * 100 / limit, 100.0) if limit > 0 else 0.0
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
            percent = min(used * 100 / limit, 100.0) if limit > 0 else 0.0
            rate_limit = UsageInfo(
                label="5小时频限", used=used, limit=limit, percent=percent,
                reset_text=reset_text, reset_epoch=reset_epoch,
            )

        return KimiUsageData(weekly=weekly, rate_limit=rate_limit)

    @staticmethod
    def _jwt_claims(token: str) -> Optional[dict]:
        """只解码本机 JWT 的元数据，不验证签名；服务端仍会校验令牌。"""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload))
        except (ValueError, IndexError, json.JSONDecodeError):
            return None

    @classmethod
    def _read_work_token(cls) -> Optional[str]:
        """优先读新版桌面端 Local Storage 的短期访问令牌，旧 Cookie 仅作兜底。"""
        candidates = []
        if WORK_LEVELDB_DIR.exists():
            paths = sorted(
                (path for path in WORK_LEVELDB_DIR.glob("*") if path.suffix in (".log", ".ldb")),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in paths:
                try:
                    contents = (
                        [path.read_bytes()]
                        if path.suffix == ".log"
                        else list(_read_sstable_blocks(path))
                    )
                except (OSError, ValueError, IndexError) as e:
                    print(f"[Work] 读取 LevelDB 失败 {path.name}: {e}")
                    continue
                for content in contents:
                    for match in WORK_JWT_PATTERN.finditer(content):
                        token = match.group().decode("ascii")
                        claims = cls._jwt_claims(token)
                        if not claims:
                            continue
                        audience = claims.get("aud", [])
                        if isinstance(audience, str):
                            audience = [audience]
                        try:
                            expires_at = float(claims.get("exp", 0))
                            issued_at = float(claims.get("iat", 0))
                        except (TypeError, ValueError):
                            continue
                        lifetime = expires_at - issued_at
                        if (
                            "kimi.com" in audience
                            and expires_at > time.time() + 30
                            and 0 < lifetime <= 86400
                        ):
                            candidates.append((issued_at, token))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]

        if not WORK_COOKIE_DB.exists():
            return None
        import shutil
        import sqlite3
        import tempfile
        tmp = Path(tempfile.gettempdir()) / "kimi_monitor_cookies.db"
        try:
            shutil.copy2(WORK_COOKIE_DB, tmp)
            db = sqlite3.connect(str(tmp))
            try:
                row = db.execute(
                    "SELECT value FROM cookies WHERE name='kimi-auth' AND value != '' "
                    "ORDER BY expires_utc DESC LIMIT 1"
                ).fetchone()
            finally:
                db.close()
            if not row:
                return None
            token = row[0]
            claims = cls._jwt_claims(token)
            if claims and float(claims.get("exp", 0)) <= time.time():
                return None
            return token
        except Exception:
            return None
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _relaunch_kimi_desktop(self) -> bool:
        """唤起 Kimi Desktop，让它自行续期 kimi-auth。成功返回 True。"""
        now = time.time()
        # 冷却检查
        if now - self._work_last_relaunch_time < self._work_relaunch_cooldown:
            return False
        # 熔断检查
        if self._work_relaunch_count >= self._work_max_relaunch:
            return False
        try:
            print("[Work] 唤起 Kimi Desktop 续期...")
            subprocess.run(["open", "-a", "Kimi"], timeout=10, check=False)
            self._work_last_relaunch_time = now
            self._work_relaunch_count += 1
            return True
        except Exception as e:
            print(f"[Work] 唤起失败: {e}")
            return False

    def fetch_work(self) -> tuple[Optional[UsageInfo], Optional[UsageInfo]]:
        """读取月度共享额度；加油包钱包与月度额度是两套不同口径。"""
        cli_error = None
        token = self._read_token()
        if token:
            try:
                resp = requests.get(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Msh-Platform": "kimi_cli",
                        "X-Msh-Device-Id": self._read_device_id(),
                        "User-Agent": f"KimiMonitor/{VERSION}",
                    },
                    timeout=(3, 10),
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    monthly = payload.get("usages", {}).get("limit_month_total", {})
                    ratio = monthly.get("used_ratio")
                    if ratio is not None:
                        percent = min(max(float(ratio) * 100, 0.0), 100.0)
                        reset_text, reset_epoch = self._parse_reset_time(monthly.get("reset_time", ""))
                        self._work_error = None
                        return UsageInfo(
                            label="Work 月度额度", used=percent, limit=100, percent=percent,
                            reset_text=reset_text, reset_epoch=reset_epoch,
                        ), None
                else:
                    cli_error = f"HTTP {resp.status_code}"
            except requests.Timeout:
                cli_error = "请求超时"
            except Exception as e:
                cli_error = str(e)

        web_token = self._read_work_token()
        if not web_token:
            self._work_error = cli_error or "凭证缺失"
            return None, None
        try:
            resp = requests.post(
                WORK_API_URL,
                headers={
                    "Authorization": f"Bearer {web_token}",
                    "Cookie": f"kimi-auth={web_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connect-Protocol-Version": "1",
                    "Origin": "https://www.kimi.com",
                    "Referer": "https://www.kimi.com/code/console",
                    "X-Msh-Platform": "web",
                    "User-Agent": f"KimiMonitor/{VERSION}",
                },
                json={},
                timeout=(3, 10),
            )
            if resp.status_code in (401, 403):
                self._work_error = "授权失效"
                return None, None
            if resp.status_code != 200:
                self._work_error = f"HTTP {resp.status_code}"
                return None, None

            payload = resp.json()
            balance = payload.get("subscriptionBalance") or payload.get("subscription_balance")
            if not balance:
                self._work_error = "无订阅额度"
                return None, None
            ratio = balance.get("amountUsedRatio", balance.get("amount_used_ratio"))
            if ratio is None:
                self._work_error = "无订阅额度"
                return None, None
            percent = min(max(float(ratio) * 100, 0.0), 100.0)
            reset_text, reset_epoch = self._parse_reset_time(
                balance.get("expireTime") or balance.get("expire_time") or ""
            )
            self._work_error = None
            return UsageInfo(
                label="Work 月度额度", used=percent, limit=100, percent=percent,
                reset_text=reset_text, reset_epoch=reset_epoch,
            ), None
        except requests.Timeout:
            self._work_error = "请求超时"
        except Exception as e:
            self._work_error = str(e)
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

        # 立即触发第一次刷新
        self._trigger_refresh()

    def _build_menu(self):
        """构建下拉菜单"""
        self.menu.clear()
        self.menu.add(rumps.MenuItem("📊 CLI 周调用配额"))
        self.menu.add(rumps.MenuItem("📊 5小时频限"))
        self.menu.add(rumps.MenuItem("📊 Work 月度额度"))
        self.menu.add(rumps.MenuItem("📊 Code 月度消耗"))
        self.menu.add(None)
        self.menu.add(rumps.MenuItem("🔄 立即刷新", callback=self.on_refresh))
        self.menu.add(None)
        
        style_menu = rumps.MenuItem("⚙️ 显示风格")
        self._style_items["percent"] = rumps.MenuItem("百分比", callback=lambda _: self.set_style("percent"))
        self._style_items["percent_countdown"] = rumps.MenuItem("百分比+倒计时", callback=lambda _: self.set_style("percent_countdown"))
        self._style_items["remaining"] = rumps.MenuItem("剩余", callback=lambda _: self.set_style("remaining"))
        self._style_items["remaining_countdown"] = rumps.MenuItem("剩余+倒计时", callback=lambda _: self.set_style("remaining_countdown"))
        self._style_items["progress"] = rumps.MenuItem("进度条", callback=lambda _: self.set_style("progress"))
        for item in self._style_items.values():
            style_menu.add(item)
        self.menu.add(style_menu)

        # 菜单栏显示来源：仅 Code / 仅 Work / 双显 / 频限+总额
        menubar_menu = rumps.MenuItem("🖥 菜单栏显示")
        for key, label in [("code", "仅 Code 频限"), ("work", "仅 Work 额度"), ("both", "Code + Work 双显"), ("freq_and_total", "频限+总额")]:
            self._menubar_items[key] = rumps.MenuItem(label, callback=lambda _, k=key: self.set_menubar_source(k))
            menubar_menu.add(self._menubar_items[key])
        self.menu.add(menubar_menu)

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
                    work, code_month = self.api.fetch_work()
                    if data is None and (work or code_month):
                        data = KimiUsageData(weekly=None, rate_limit=None)
                    if data is not None:
                        data.work = work
                        data.code_month = code_month
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

    def set_menubar_source(self, source: str):
        self.config.set("menubar_source", source)
        self._update_ui(self.api._last_data)

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

    @staticmethod
    def _icon_for(percent: float) -> str:
        """月相图标：5 相渐盈，100% 为满月"""
        return MOON_PHASES[min(4, int(percent * 5 / 100))]

    def _styled_text(self, info: UsageInfo) -> str:
        """按当前显示风格格式化一条额度数据（不含图标）"""
        style = self.config.get("style", "percent_countdown")
        if style == "percent":
            return f"{info.percent:.2f}%"
        if style == "percent_countdown":
            if info.reset_text and info.reset_text != "已重置":
                return f"{info.percent:.2f}% · {info.reset_text}"
            return f"{info.percent:.2f}%"
        if style == "remaining":
            return f"剩{max(0, 100 - info.percent):.2f}%"
        if style == "remaining_countdown":
            remaining = max(0, 100 - info.percent)
            if info.reset_text and info.reset_text != "已重置":
                return f"剩{remaining:.2f}% · {info.reset_text}"
            return f"剩{remaining:.2f}%"
        if style == "progress":
            return self._progress_bar(info.percent)
        return f"{info.percent:.2f}%"

    def _update_title(self, data: Optional[KimiUsageData]):
        source = self.config.get("menubar_source", "code")

        # 熔断状态优先显示（Code 相关来源）
        if source in ("code", "freq_and_total"):
            if self.api._silent_mode and self.api._consecutive_failures >= 3:
                self._set_title("⚠️ 失去连接")
                return
            if self.api._token_refresh_failures >= 3:
                self._set_title("⚠️ Token 失效")
                return

        code_info = data.rate_limit if data else None
        code_month_info = data.code_month if data else None
        work_info = data.work if data else None

        if source == "work":
            if not work_info:
                err = self.api._work_error
                self._set_title(f"⚠️ {err[:8]}" if err else "⚠️ --")
                return
            title = f"{self._icon_for(work_info.percent)} {self._styled_text(work_info)}"
        elif source == "both":
            parts = []
            if code_month_info:
                parts.append(self._styled_text(code_month_info))
            if work_info:
                parts.append(self._styled_text(work_info))
            if not parts:
                error = self.api.get_last_error() or self.api._work_error
                self._set_title(f"⚠️ {error[:8]}" if error else "⚠️ --")
                return
            # 总量口径：Code 本月消耗 + Work 本月消耗，月相按两者之和（封顶 100%）
            combined = (code_month_info.percent if code_month_info else 0) + (work_info.percent if work_info else 0)
            title = f"{self._icon_for(min(100, combined))} " + " · ".join(parts)
        elif source == "freq_and_total":
            if not code_info:
                error = self.api.get_last_error()
                self._set_title(f"⚠️ {error[:8]}" if error else "⚠️ --")
                return
            # 总额度 = Code 月度 + Work 月度
            combined = (code_month_info.percent if code_month_info else 0) + (work_info.percent if work_info else 0)
            freq_text = self._styled_text(code_info)
            total_text = f"{combined:.2f}%"
            title = f"{self._icon_for(min(100, combined))} {freq_text} · {total_text}"
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
            self.menu["📊 CLI 周调用配额"].title = f"📊 CLI 周调用配额: {w.used}/{w.limit} ({w.percent:.2f}%) · {w.reset_text}"
        else:
            self.menu["📊 CLI 周调用配额"].title = "📊 CLI 周调用配额: --"

        if data and data.rate_limit:
            rl = data.rate_limit
            self.menu["📊 5小时频限"].title = f"📊 5小时频限: {rl.used}/{rl.limit} ({rl.percent:.2f}%) · {rl.reset_text}"
        else:
            error = self.api.get_last_error()
            self.menu["📊 5小时频限"].title = f"📊 5小时频限: {error or '--'}"

        # 未安装 Kimi 桌面端（无 Cookies 数据库）时整行隐藏
        try:
            hidden = not WORK_COOKIE_DB.exists()
            self.menu["📊 Work 月度额度"]._menuitem.setHidden_(hidden)
            self.menu["📊 Code 月度消耗"]._menuitem.setHidden_(hidden)
        except Exception:
            pass
        if data and data.work:
            w = data.work
            self.menu["📊 Work 月度额度"].title = f"📊 Work 月度额度: {w.percent:.2f}% · {w.reset_text}"
        else:
            self.menu["📊 Work 月度额度"].title = f"📊 Work 月度额度: {self.api._work_error or '--'}"
        if data and data.code_month:
            cm = data.code_month
            self.menu["📊 Code 月度消耗"].title = f"📊 Code 月度消耗: {cm.percent:.2f}% · {cm.reset_text}"
        else:
            self.menu["📊 Code 月度消耗"].title = f"📊 Code 月度消耗: {self.api._work_error or '--'}"

        source = self.config.get("menubar_source", "code")
        for key, item in self._menubar_items.items():
            item.state = 1 if key == source else 0

        style = self.config.get("style", "percent_countdown")
        for key, item in self._style_items.items():
            item.state = 1 if key == style else 0

    def _progress_bar(self, percent: float, width: int = 10) -> str:
        filled = percent * width // 100
        filled = min(filled, width)
        empty = width - filled
        return "█" * filled + "░" * empty


if __name__ == "__main__":
    _instance_lock = acquire_single_instance_lock()
    if _instance_lock is None:
        print("KimiMonitor 已在运行，本次启动退出")
        sys.exit(0)
    app = KimiMonitorApp()
    app.run()
