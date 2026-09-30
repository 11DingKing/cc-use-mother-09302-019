"""许可覆盖与使用范围的合规判定。

判定逻辑只依赖版本链数据与显式给定的时间，不读取当前系统时钟，
因此巡检结果确定性可重放。

版本链按 ``recorded_at`` 回放：评估某个历史时刻（例如已完成场次的发布
时点）时传入较小的 ``cutoff``，事后追加的撤销、续期不会改变历史结论。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

# 版本类型
GRANT = "grant"
RENEWAL = "renewal"
REVOCATION = "revocation"
SUSPENSION = "suspension"
RESUMPTION = "resumption"

# 不合规原因
NO_GRANT = "no_valid_grant"
EXPIRED = "license_expired"
NOT_YET_VALID = "license_not_yet_valid"
REVOKED = "license_revoked"
SUSPENDED = "license_suspended"
SCOPE_MISMATCH = "scope_not_covered"
TERRITORY_MISMATCH = "territory_not_covered"


def prefix_at(versions: list[dict], cutoff: datetime | None) -> list[dict]:
    """返回 cutoff（含）之前已经登记的版本，保持链序。"""
    if cutoff is None:
        return list(versions)
    return [v for v in versions if v["recorded_at"] <= cutoff]


def active_grant(versions: Iterable[dict], moment: datetime) -> dict | None:
    """找到覆盖某一时刻的最新授权/续期版本。"""
    chosen = None
    for version in versions:
        if version["kind"] not in (GRANT, RENEWAL):
            continue
        if version["valid_from"] <= moment < version["valid_until"]:
            chosen = version  # 链序靠后的续期优先
    return chosen


def revocation_at(versions: Iterable[dict], moment: datetime) -> datetime | None:
    """截至该时刻是否已被撤销，返回生效时间。"""
    effective = None
    for version in versions:
        if version["kind"] != REVOCATION:
            continue
        at = version.get("effective_at", version["recorded_at"])
        if at <= moment:
            effective = at
    return effective


def suspension_intervals(versions: Iterable[dict]) -> list[tuple[datetime, datetime]]:
    """展开临时停用/恢复，得到实际停用区间列表。

    恢复会把当时仍在进行中的停用区间提前截断。
    """
    intervals: list[tuple[datetime, datetime]] = []
    for version in versions:
        kind = version["kind"]
        if kind == SUSPENSION:
            intervals.append((version["suspend_from"], version["suspend_until"]))
        elif kind == RESUMPTION:
            at = version.get("effective_at", version["recorded_at"])
            intervals = [
                (start, min(end, at)) if start <= at < end else (start, end)
                for start, end in intervals
            ]
    return [(s, e) for s, e in intervals if s < e]


def suspended_at(versions: Iterable[dict], moment: datetime) -> tuple[datetime, datetime] | None:
    for start, end in suspension_intervals(versions):
        if start <= moment < end:
            return start, end
    return None


def instant_status(
    versions: list[dict],
    moment: datetime,
    needed_rights: set[str],
    territory: str | None = None,
) -> dict:
    """评估某一瞬间的许可状态，返回 ``{compliant, reason, scope, ...}``。"""
    revoked = revocation_at(versions, moment)
    if revoked is not None:
        return {"compliant": False, "reason": REVOKED, "revoked_at": revoked}

    paused = suspended_at(versions, moment)
    if paused is not None:
        return {
            "compliant": False,
            "reason": SUSPENDED,
            "suspend_from": paused[0],
            "suspend_until": paused[1],
        }

    grant = active_grant(versions, moment)
    if grant is None:
        grants = [v for v in versions if v["kind"] in (GRANT, RENEWAL)]
        if not grants:
            return {"compliant": False, "reason": NO_GRANT}
        if all(v["valid_from"] > moment for v in grants):
            return {"compliant": False, "reason": NOT_YET_VALID}
        return {"compliant": False, "reason": EXPIRED}

    scope = set(grant["scope"])
    missing = needed_rights - scope
    if missing:
        return {
            "compliant": False,
            "reason": SCOPE_MISMATCH,
            "required": sorted(needed_rights),
            "granted": sorted(scope),
            "missing": sorted(missing),
        }

    licensed_territory = grant.get("territory")
    if licensed_territory and territory and licensed_territory != territory:
        return {
            "compliant": False,
            "reason": TERRITORY_MISMATCH,
            "required_territory": territory,
            "licensed_territory": licensed_territory,
        }

    return {
        "compliant": True,
        "reason": None,
        "scope": sorted(scope),
        "grant_version": grant["seq"],
        "license_hash": grant["hash"],
    }


def _boundaries(versions: list[dict], start: datetime, end: datetime) -> list[datetime]:
    points = {start, end}
    for version in versions:
        kind = version["kind"]
        if kind in (GRANT, RENEWAL):
            candidates = [version["valid_from"], version["valid_until"]]
        elif kind == REVOCATION:
            candidates = [version.get("effective_at", version["recorded_at"])]
        elif kind == SUSPENSION:
            candidates = [version["suspend_from"], version["suspend_until"]]
        elif kind == RESUMPTION:
            candidates = [version.get("effective_at", version["recorded_at"])]
        else:
            candidates = []
        for point in candidates:
            if start < point < end:
                points.add(point)
    return sorted(points)


def evaluate_window(
    versions: list[dict],
    start: datetime,
    end: datetime,
    needed_rights: set[str],
    territory: str | None = None,
    cutoff: datetime | None = None,
) -> list[dict]:
    """评估 ``[start, end)`` 整段是否被许可完整覆盖。

    状态在版本边界之间恒定，因此逐段取起点与中点检查即可精确覆盖全场次
    时段，而不是只查单点。返回每个不合规分段的违规明细。
    """
    if end <= start:
        raise ValueError("场次结束时间必须晚于开始时间")

    scoped = prefix_at(versions, cutoff)
    violations: list[dict] = []
    points = _boundaries(scoped, start, end)
    for left, right in zip(points, points[1:]):
        samples = [left, left + (right - left) / 2]
        for moment in samples:
            status = instant_status(scoped, moment, needed_rights, territory)
            if not status["compliant"]:
                violations.append(
                    {
                        "segment_from": left,
                        "segment_to": right,
                        "checked_at": moment,
                        **status,
                    }
                )
                break  # 该分段已不合规，无需再查中点
    return violations
