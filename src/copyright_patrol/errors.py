"""领域错误。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域错误的基类。"""

    status = 400
    code = "invalid_request"


class ValidationError(DomainError):
    """输入不满足领域约束。"""

    status = 400
    code = "validation_failed"


class NotFoundError(DomainError):
    """引用的对象不存在。"""

    status = 404
    code = "not_found"


class ConflictError(DomainError):
    """状态机冲突或重复提交。"""

    status = 409
    code = "conflict"


class ChainIntegrityError(DomainError):
    """版本链断链或摘要不匹配，说明历史被篡改。"""

    status = 409
    code = "chain_broken"


class PublicationBlocked(DomainError):
    """场次发布被许可校验拦截。"""

    status = 422
    code = "publication_blocked"

    def __init__(self, message: str, violations: list[dict]) -> None:
        super().__init__(message)
        self.violations = violations
