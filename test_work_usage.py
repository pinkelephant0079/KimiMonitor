#!/usr/bin/env python3
"""Work 月度额度数据链路的回归测试。"""

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def make_jwt(issued_at: float, expires_at: float) -> str:
    def encode(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = encode({"alg": "none"})
    payload = encode({"aud": ["kimi.com"], "iat": issued_at, "exp": expires_at})
    return f"{header}.{payload}.c2lnbmF0dXJl"


class WorkUsageTests(unittest.TestCase):
    def test_cli_monthly_ratio_is_preferred(self) -> None:
        client = app.KimiAPIClient()
        client._token = "cli-token"
        response = FakeResponse(200, {
            "usages": {"limit_month_total": {"used_ratio": 0.426, "reset_time": "2026-12-01T00:00:00Z"}},
        })
        with patch.object(app.requests, "get", return_value=response), patch.object(
            app.KimiAPIClient, "_read_work_token", side_effect=AssertionError("不应读取网页会话")
        ):
            work, code_month = client.fetch_work()
        self.assertAlmostEqual(work.percent, 42.6)
        self.assertIsNone(code_month)
        self.assertIsNone(client._work_error)

    def test_web_stats_are_used_when_cli_has_only_booster_wallet(self) -> None:
        client = app.KimiAPIClient()
        client._token = "cli-token"
        cli_response = FakeResponse(200, {
            "booster_wallet": {"balance": {"amount": "30000000000", "amountLeft": "7702512700"}},
        })
        web_response = FakeResponse(200, {
            "subscriptionBalance": {"amountUsedRatio": 0.5282, "expireTime": "2026-11-20T00:00:00Z"},
        })
        with patch.object(app.requests, "get", return_value=cli_response), patch.object(
            app.requests, "post", return_value=web_response
        ) as post, patch.object(app.KimiAPIClient, "_read_work_token", return_value="web-token"):
            work, _ = client.fetch_work()
        self.assertAlmostEqual(work.percent, 52.82)
        self.assertNotAlmostEqual(work.percent, 74.36)
        self.assertEqual(post.call_args.kwargs["headers"]["Cookie"], "kimi-auth=web-token")

    def test_snake_case_web_stats_are_supported(self) -> None:
        client = app.KimiAPIClient()
        client._token = None
        response = FakeResponse(200, {
            "subscription_balance": {"amount_used_ratio": 0.25, "expire_time": "2026-12-01T00:00:00Z"},
        })
        with patch.object(app.KimiAPIClient, "_read_work_token", return_value="web-token"), patch.object(
            app.requests, "post", return_value=response
        ):
            work, _ = client.fetch_work()
        self.assertEqual(work.percent, 25.0)

    def test_leveldb_reader_ignores_refresh_and_expired_tokens(self) -> None:
        now = time.time()
        access = make_jwt(now - 10, now + 890)
        refresh = make_jwt(now - 10, now + 90 * 86400)
        expired = make_jwt(now - 1000, now - 100)
        with tempfile.TemporaryDirectory() as directory:
            leveldb = Path(directory)
            (leveldb / "000001.log").write_bytes(
                b"\x00access_token\x00" + expired.encode("ascii")
                + b"\x00refresh_token\x00" + refresh.encode("ascii")
                + b"\x00access_token\x00" + access.encode("ascii") + b"\x00"
            )
            with patch.object(app, "WORK_LEVELDB_DIR", leveldb), patch.object(
                app, "WORK_COOKIE_DB", leveldb / "missing-cookies"
            ):
                self.assertEqual(app.KimiAPIClient._read_work_token(), access)


class SingleInstanceTests(unittest.TestCase):
    def test_second_opener_cannot_acquire_lock_until_first_closes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "instance.lock"
            with patch.object(app, "INSTANCE_LOCK_PATH", lock_path):
                first = app.acquire_single_instance_lock()
                self.assertIsNotNone(first)
                self.assertIsNone(app.acquire_single_instance_lock())
                first.close()
                third = app.acquire_single_instance_lock()
                self.assertIsNotNone(third)
                third.close()


if __name__ == "__main__":
    unittest.main()
