"""基于 SQLite 的幂等响应存储。

缓存键 = 幂等键 + 规范化计划/表快照指纹；命中后原样返回首次响应字节，
保证重复请求得到**字节一致**的 JSON。同一幂等键绑定不同请求内容时，
冲突信息（不同的指纹）一并持久化并返回 409。

SQLite 仅承担幂等账本；查询求值本身在受限内存引擎中完成。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotent_responses (
    idempotency_key TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    status_code     INTEGER NOT NULL,
    content_type    TEXT NOT NULL,
    body            BLOB NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (idempotency_key, fingerprint)
);
CREATE TABLE IF NOT EXISTS idempotency_index (
    idempotency_key TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL
);
"""


class IdempotencyStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def lookup(self, idempotency_key: str, fingerprint: str) -> dict[str, Any] | None:
        """命中返回 {status_code, content_type, body, replay: bool}。

        * 键 + 指纹均一致 => replay（含首次写入后再次提交的情况）；
        * 键一致、指纹不同 => 返回 {'conflict': True, 'other_fingerprint': ...}。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint FROM idempotency_index WHERE idempotency_key = ?",
                (idempotency_key,)).fetchone()
            if row is None:
                return None
            if row[0] != fingerprint:
                return {"conflict": True, "other_fingerprint": row[0]}
            saved = self._conn.execute(
                "SELECT status_code, content_type, body FROM idempotent_responses "
                "WHERE idempotency_key = ? AND fingerprint = ?",
                (idempotency_key, fingerprint)).fetchone()
            if saved is None:  # 理论上不可达：索引与响应同事务写入
                return None
            return {"status_code": saved[0], "content_type": saved[1],
                    "body": saved[2], "replay": True}

    def store(self, idempotency_key: str, fingerprint: str, status_code: int,
              content_type: str, body: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotency_index(idempotency_key, fingerprint) "
                "VALUES (?, ?)", (idempotency_key, fingerprint))
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotent_responses"
                "(idempotency_key, fingerprint, status_code, content_type, body) "
                "VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, fingerprint, status_code, content_type,
                 sqlite3.Binary(body)))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()
