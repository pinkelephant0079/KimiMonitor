#!/usr/bin/env python3
"""KimiMonitor 调试版本 - 带详细日志"""

import json
import os
import queue
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import rumps

API_URL = "https://api.kimi.com/coding/v1/usages"
TOKEN_PATH = Path.home() / ".kimi" / "credentials" / "kimi-code.json"
DEVICE_ID_PATH = Path.home() / ".kimi" / "device_id"


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


class KimiAPIClient:
    def __init__(self):
        self._token = None
        self._device_id = None
        self._last_error = None
        self._last_data = None

    def _read_token(self):
        if self._token:
            return self._token
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._token = data.get("access_token")
                print(f"[DEBUG] Token read: {bool(self._token)}")
                return self._token
        except Exception as e:
            print(f"[DEBUG] Token read failed: {e}")
            self._last_error = f"读取 token 失败: {e}"
            return None

    def _read_device_id(self):
        if self._device_id:
            return self._device_id
        try:
            with open(DEVICE_ID_PATH, "r", encoding="utf-8") as f:
                self._device_id = f.read().strip()
                return self._device_id
        except Exception:
            return "unknown"

    def _parse_reset_time(self, reset_time_str):
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
            if hours > 0:
                if mins > 0:
                    return f"{hours}h{mins}m后", dt.timestamp()
                return f"{hours}h后", dt.timestamp()
            elif mins > 0:
                return f"{mins}m后", dt.timestamp()
            else:
                return "即将", dt.timestamp()
        except Exception as e:
            print(f"[DEBUG] Parse reset time error: {e}")
            return "未知", None

    def fetch(self):
        print("[DEBUG] fetch() started")
        token = self._read_token()
        if not token:
            print("[DEBUG] No token, returning None")
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Msh-Platform": "kimi_cli",
            "X-Msh-Device-Id": self._read_device_id(),
            "User-Agent": "KimiMonitor/1.0",
        }

        try:
            print("[DEBUG] Sending request...")
            start = time.time()
            resp = requests.get(API_URL, headers=headers, timeout=(3, 10))
            elapsed = time.time() - start
            print(f"[DEBUG] Request completed in {elapsed:.2f}s, status={resp.status_code}")
            
            if resp.status_code == 401:
                self._last_error = "Token 已过期"
                return None
            elif resp.status_code == 429:
                self._last_error = "请求太频繁"
                return None
            elif resp.status_code != 200:
                self._last_error = f"API 错误: HTTP {resp.status_code}"
                return None

            payload = resp.json()
            data = self._parse_payload(payload)
            self._last_data = data
            self._last_error = None
            print("[DEBUG] fetch() success")
            return data

        except requests.Timeout:
            print("[DEBUG] Request timeout")
            self._last_error = "请求超时"
        except requests.RequestException as e:
            print(f"[DEBUG] Request error: {e}")
            self._last_error = f"网络错误: {e}"
        except Exception as e:
            print(f"[DEBUG] Unexpected error: {e}")
            self._last_error = f"错误: {e}"
        
        return self._last_data

    def _parse_payload(self, payload):
        weekly = None
        rate_limit = None

        usage = payload.get("usage")
        if isinstance(usage, dict):
            limit = int(usage.get("limit", 0) or 0)
            used = int(usage.get("used", 0) or 0)
            reset_str = usage.get("resetTime", "")
            reset_text, reset_epoch = self._parse_reset_time(reset_str)
            percent = min(int(used * 100 / limit), 100) if limit > 0 else 0
            weekly = UsageInfo(
                label="本周用量", used=used, limit=limit,
                percent=percent, reset_text=reset_text, reset_epoch=reset_epoch,
            )

        limits = payload.get("limits", [])
        if limits and isinstance(limits[0], dict):
            detail = limits[0].get("detail", {})
            limit = int(detail.get("limit", 0) or 0)
            used = int(detail.get("used", 0) or 0)
            reset_str = detail.get("resetTime", "")
            reset_text, reset_epoch = self._parse_reset_time(reset_str)
            percent = min(int(used * 100 / limit), 100) if limit > 0 else 0
            rate_limit = UsageInfo(
                label="频限明细", used=used, limit=limit,
                percent=percent, reset_text=reset_text, reset_epoch=reset_epoch,
            )

        return KimiUsageData(weekly=weekly, rate_limit=rate_limit)

    def get_last_error(self):
        return self._last_error


class KimiMonitorApp(rumps.App):
    def __init__(self):
        print("[DEBUG] App init started")
        super().__init__(
            name="KimiMonitor",
            title="⏳ 启动中",
            icon=None,
            quit_button="退出",
        )
        
        self.api = KimiAPIClient()
        self._result_queue = queue.Queue()
        
        print("[DEBUG] Starting refresh thread...")
        self._refresh_thread = threading.Thread(target=self._refresh_worker, daemon=True)
        self._refresh_thread.start()
        
        print("[DEBUG] Starting UI timer...")
        self._ui_timer = rumps.Timer(self._on_ui_tick, 1)
        self._ui_timer.start()
        
        print("[DEBUG] Starting data timer...")
        self._data_timer = rumps.Timer(self._on_schedule_refresh, 60)
        self._data_timer.start()
        
        print("[DEBUG] Scheduling first refresh...")
        self._on_schedule_refresh(None)
        print("[DEBUG] App init completed")

    def _refresh_worker(self):
        print("[DEBUG] Refresh worker started")
        while True:
            try:
                msg = self._result_queue.get(timeout=1)
                print(f"[DEBUG] Worker got msg: {msg}")
                if msg == "refresh":
                    print("[DEBUG] Worker calling fetch()")
                    data = self.api.fetch()
                    print(f"[DEBUG] Worker fetch done, data={data is not None}")
                    self._result_queue.put(("result", data))
                    print("[DEBUG] Worker put result to queue")
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[DEBUG] Worker error: {e}")

    def _on_ui_tick(self, _):
        try:
            while True:
                msg = self._result_queue.get_nowait()
                print(f"[DEBUG] UI tick got msg: {type(msg)}")
                if isinstance(msg, tuple) and msg[0] == "result":
                    print(f"[DEBUG] UI tick updating UI, data={msg[1] is not None}")
                    self._update_ui(msg[1])
        except queue.Empty:
            pass

    def _on_schedule_refresh(self, _):
        print("[DEBUG] Schedule refresh triggered")
        try:
            self._result_queue.put_nowait("refresh")
            print("[DEBUG] Refresh scheduled")
        except queue.Full:
            print("[DEBUG] Queue full, refresh skipped")

    def _update_ui(self, data):
        print(f"[DEBUG] _update_ui called, data={data is not None}")
        if not data or not data.rate_limit:
            error = self.api.get_last_error()
            self.title = f"⚠️ {error[:8]}" if error else "⚪ --"
            print(f"[DEBUG] UI title set to error: {self.title}")
            return

        rl = data.rate_limit
        self.title = f"⚪ {rl.percent}% · {rl.reset_text}"
        print(f"[DEBUG] UI title set to: {self.title}")


if __name__ == "__main__":
    print("[DEBUG] Main started")
    app = KimiMonitorApp()
    print("[DEBUG] Running app...")
    app.run()
