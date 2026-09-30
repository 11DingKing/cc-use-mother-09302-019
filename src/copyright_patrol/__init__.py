"""版权到期巡检服务端。

模块划分：

- ``clock``：可替换的时间来源，保证巡检与发布判断可控、可重放。
- ``errors``：领域错误及对应的接口状态码。
- ``hashing``：版本链摘要算法。
- ``coverage``：许可时段、停用区间与使用范围的合规判定。
- ``repository``：只追加记录的存储，含 JSON 快照持久化。
- ``service``：领域服务，登记、版本链、发布拦截、巡检与影响查询。
- ``api`` / ``serve``：标准库实现的 HTTP 接口与启动入口。
"""
from .clock import Clock, FixedClock, SystemClock
from .errors import (
    ChainIntegrityError,
    ConflictError,
    DomainError,
    NotFoundError,
    PublicationBlocked,
    ValidationError,
)
from .repository import Repository
from .service import CopyrightService

__all__ = [
    "Clock",
    "FixedClock",
    "SystemClock",
    "Repository",
    "CopyrightService",
    "DomainError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
    "PublicationBlocked",
    "ChainIntegrityError",
]
