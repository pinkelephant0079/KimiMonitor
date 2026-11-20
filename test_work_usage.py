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


def encode_varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def snappy_literal(data: bytes) -> bytes:
    """生成测试用的 Snappy 流，生产代码仍需支持字面量和回引。"""
    result = bytearray(encode_varint(len(data)))
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + 65536]
        offset += len(chunk)
        length = len(chunk)
        if length <= 60:
            result.append((length - 1) << 2)
            count = 0
        else:
            count = 1 if length <= 256 else 2
            result.append((59 + count) << 2)
            result.extend((length - 1).to_bytes(count, "little"))
        result.extend(chunk)
    return bytes(result)


def leveldb_entry(key: bytes, value: bytes) -> bytes:
    return encode_varint(0) + encode_varint(len(key)) + encode_varint(len(value)) + key + value


def leveldb_block(entries: list[tuple[bytes, bytes]]) -> bytes:
    content = b"".join(leveldb_entry(key, value) for key, value in entries)
    return content + (0).to_bytes(4, "little") + (1).to_bytes(4, "little")


def stored_snappy_block(content: bytes) -> bytes:
    return snappy_literal(content) + b"\x01" + b"\x00" * 4


def write_compressed_sstable(path: Path, content: bytes) -> None:
    data = leveldb_block([(b"access_token", content)])
    stored_data = stored_snappy_block(data)
    data_handle = encode_varint(0) + encode_varint(len(stored_data) - 5)
    index = leveldb_block([(b"data", data_handle)])
    stored_index = stored_snappy_block(index)
    footer = encode_varint(0) + encode_varint(0)
    footer += encode_varint(len(stored_data)) + encode_varint(len(stored_index) - 5)
    footer = footer.ljust(40, b"\x00") + app.LEVELDB_MAGIC
    path.write_bytes(stored_data + stored_index + footer)


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

    def test_leveldb_reader_reads_current_access_token_from_compressed_table(self) -> None:
        now = time.time()
        access = make_jwt(now - 10, now + 890)
        refresh = make_jwt(now - 10, now + 90 * 86400)
        expired = make_jwt(now - 1000, now - 100)
        with tempfile.TemporaryDirectory() as directory:
            leveldb = Path(directory)
            write_compressed_sstable(
                leveldb / "000002.ldb",
                b"\x00access_token\x00" + expired.encode("ascii")
                + b"\x00refresh_token\x00" + refresh.encode("ascii")
                + b"\x00access_token\x00" + access.encode("ascii") + b"\x00",
            )
            with patch.object(app, "WORK_LEVELDB_DIR", leveldb), patch.object(
                app, "WORK_COOKIE_DB", leveldb / "missing-cookies"
            ):
                self.assertEqual(app.KimiAPIClient._read_work_token(), access)

    def test_snappy_reader_handles_back_references(self) -> None:
        compressed = b"\x09\x08abc\x16\x03\x00"
        self.assertEqual(app._decompress_snappy(compressed), b"abcabcabc")


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
