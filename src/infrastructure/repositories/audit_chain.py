"""哈希链审计存储（防篡改JSONL）。

每条记录：seq + prev_hash + entry，record_hash=SHA256(prev_hash+canonical(record))；
链头哈希变动即可检测任何篡改/删改，满足SECURITY_COMPLIANCE审计红线。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_GENESIS = "0" * 64


def canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _record_hash(seq: int, prev_hash: str, entry: dict[str, Any], ts: str) -> str:
    payload = canonical({"seq": seq, "prev_hash": prev_hash, "entry": entry, "ts": ts})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditChainWriter:
    """追加写哈希链（线程安全，跨进程按文件末尾续链）。"""

    def __init__(self, chain_path: str = "data/audit/audit_chain.jsonl") -> None:
        self._path = Path(chain_path)
        self._lock = threading.Lock()
        self._seq = 0
        self._head = _GENESIS
        self._resumed = False

    def _resume(self) -> None:
        if self._resumed:
            return
        if self._path.exists():
            last = ChainVerifier(self._path).last_record()
            if last is not None:
                self._seq = last["seq"]
                self._head = last["record_hash"]
        self._resumed = True

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        """落一条审计记录，返回含seq/record_hash的完整链记录。"""
        with self._lock:
            self._resume()
            ts = datetime.now(timezone.utc).isoformat()
            seq = self._seq + 1
            record = {
                "seq": seq,
                "ts": ts,
                "prev_hash": self._head,
                "entry": entry,
                "record_hash": _record_hash(seq, self._head, entry, ts),
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._seq = seq
            self._head = record["record_hash"]
            return record

    @property
    def head(self) -> str:
        with self._lock:
            self._resume()
            return self._head


class ChainVerifier:
    """全链校验：任何记录被篡改/删除/插入都会导致断链。"""

    def __init__(self, chain_path: str | Path) -> None:
        self._path = Path(chain_path)

    def records(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def last_record(self) -> dict[str, Any] | None:
        records = self.records()
        return records[-1] if records else None

    def verify(self) -> dict[str, Any]:
        """返回 {valid, count, head, broken_at}。"""
        records = self.records()
        prev = _GENESIS
        for rec in records:
            expect = _record_hash(rec["seq"], prev, rec["entry"], rec["ts"])
            if rec["prev_hash"] != prev or rec["record_hash"] != expect:
                return {
                    "valid": False, "count": len(records),
                    "head": records[-1]["record_hash"] if records else None,
                    "broken_at": rec["seq"],
                }
            prev = rec["record_hash"]
        return {
            "valid": True, "count": len(records),
            "head": records[-1]["record_hash"] if records else None,
            "broken_at": None,
        }
