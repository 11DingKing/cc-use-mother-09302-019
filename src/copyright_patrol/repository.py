"""只追加记录的存储与 JSON 快照持久化。

内存中直接存放字典，所有集合型数据（许可版本、教案引用版本、场次事件）
只允许追加，不提供就地修改接口。持久化为整库 JSON 快照，时间字段按
ISO-8601 往返。
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# 快照中需要还原为 datetime 的字段
_DATETIME_KEYS = {
    "start",
    "end",
    "as_of",
    "deadline",
    "created_at",
    "recorded_at",
    "occurred_at",
    "published_at",
    "completed_at",
    "resolved_at",
    "detected_at",
    "reopened_at",
    "effective_at",
    "valid_from",
    "valid_until",
    "suspend_from",
    "suspend_until",
    "checked_at",
    "segment_from",
    "segment_to",
    "window_start",
    "window_end",
    "revoked_at",
}


class Repository:
    def __init__(self) -> None:
        self.holders: dict[str, dict] = {}
        self.materials: dict[str, dict] = {}
        self.licenses: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.risk_cases: dict[str, dict] = {}
        self.patrol_batches: dict[str, dict] = {}
        self.replace_commands: dict[str, dict] = {}
        self.sequence = 0

    # ---- 通用序号（案件、指令等对外编号） ----
    def next_serial(self, prefix: str) -> str:
        self.sequence += 1
        return f"{prefix}-{self.sequence:06d}"

    # ---- 快照持久化 ----
    def save(self, path: str | Path) -> None:
        payload = self.to_dict()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 唯一临时文件名 + 原子替换，避免并发写相互删除临时文件
        import os

        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=_encode)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def to_dict(self) -> dict:
        return {
            "holders": self.holders,
            "materials": self.materials,
            "licenses": self.licenses,
            "plans": self.plans,
            "sessions": self.sessions,
            "risk_cases": self.risk_cases,
            "patrol_batches": self.patrol_batches,
            "replace_commands": self.replace_commands,
            "sequence": self.sequence,
        }

    @classmethod
    def load(cls, path: str | Path) -> "Repository":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        repo = cls()
        repo.holders = _decode(raw["holders"])
        repo.materials = _decode(raw["materials"])
        repo.licenses = _decode(raw["licenses"])
        repo.plans = _decode(raw["plans"])
        repo.sessions = _decode(raw["sessions"])
        repo.risk_cases = _decode(raw["risk_cases"])
        repo.patrol_batches = _decode(raw["patrol_batches"])
        repo.replace_commands = _decode(raw["replace_commands"])
        repo.sequence = raw.get("sequence", 0)
        return repo


def _encode(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"不可持久化的类型：{type(value)!r}")


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        decoded: dict[str, Any] = {}
        for key, item in value.items():
            if key in _DATETIME_KEYS and isinstance(item, str):
                decoded[key] = datetime.fromisoformat(item)
            else:
                decoded[key] = _decode(item)
        return decoded
    return value
