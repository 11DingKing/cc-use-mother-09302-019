"""时间来源抽象。

领域内所有判断都通过时钟取当前时间，测试与批量巡检可以注入固定时间，
保证同一输入永远得到同一结果（巡检安全可重跑）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区信息的当前时间。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """固定时钟，可通过 :meth:`advance` 推进，用于场景回放。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = _aware(moment)

    def now(self) -> datetime:
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = _aware(moment)

    def advance(self, **delta) -> datetime:
        from datetime import timedelta

        self._moment = self._moment + timedelta(**delta)
        return self._moment


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
