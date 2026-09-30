"""版本链摘要工具。

许可版本与教案引用版本都以规范化 JSON 计算 SHA-256 摘要，并携带前一版本
摘要形成哈希链，任何历史改动都会让后续校验失败。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

GENESIS = "sha256:0000000000000000000000000000000000000000000000000000000000000000"


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    )


def digest(payload: Any) -> str:
    body = canonical_json(payload)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"不可序列化的类型：{type(value)!r}")
