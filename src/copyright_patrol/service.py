"""版权到期巡检领域服务。

所有写操作都向版本链追加新版本；读操作以显式 cutoff 回放版本链，
因此同一份数据在给定时间点的结论唯一且可重放。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import coverage
from .clock import Clock, SystemClock
from .errors import (
    ChainIntegrityError,
    ConflictError,
    NotFoundError,
    PublicationBlocked,
    ValidationError,
)
from .hashing import GENESIS, digest
from .repository import Repository

NO_LICENSE = "no_license_registered"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class CopyrightService:
    def __init__(self, repo: Repository | None = None, clock: Clock | None = None) -> None:
        self.repo = repo or Repository()
        self.clock = clock or SystemClock()

    # ============ 权利主体与素材 ============

    def register_holder(self, holder_id: str, name: str, contact: str = "") -> dict:
        if holder_id in self.repo.holders:
            raise ConflictError(f"权利主体已存在：{holder_id}")
        holder = {
            "holder_id": holder_id,
            "name": name,
            "contact": contact,
            "created_at": self.clock.now(),
        }
        self.repo.holders[holder_id] = holder
        return holder

    def register_material(
        self,
        material_id: str,
        title: str,
        kind: str,
        holder_id: str,
        alternative_ids: list[str] | None = None,
        license_free: bool = False,
    ) -> dict:
        if material_id in self.repo.materials:
            raise ConflictError(f"素材已存在：{material_id}")
        self._require_holder(holder_id)
        alternative_ids = alternative_ids or []
        for alt in alternative_ids:
            if alt == material_id:
                raise ValidationError("素材不能把自身设为替代素材")
            if alt not in self.repo.materials:
                raise NotFoundError(f"替代素材未登记：{alt}")
        material = {
            "material_id": material_id,
            "title": title,
            "kind": kind,
            "holder_id": holder_id,
            "alternative_ids": list(alternative_ids),
            "license_free": license_free,
            "created_at": self.clock.now(),
        }
        self.repo.materials[material_id] = material
        return material

    def link_alternative(self, material_id: str, alternative_id: str) -> dict:
        """补充登记替代关系（双方必须均已登记，不能自指）。"""
        material = self._require_material(material_id)
        self._require_material(alternative_id)
        if alternative_id == material_id:
            raise ValidationError("素材不能把自身设为替代素材")
        if alternative_id not in material["alternative_ids"]:
            material["alternative_ids"].append(alternative_id)
        return material

    # ============ 许可版本链 ============

    def register_license(
        self,
        license_id: str,
        material_id: str,
        scope: list[str],
        valid_from: datetime,
        valid_until: datetime,
        territory: str | None = None,
        note: str = "",
    ) -> dict:
        if license_id in self.repo.licenses:
            raise ConflictError(f"许可已存在：{license_id}")
        self._require_material(material_id)
        valid_from, valid_until = _aware(valid_from), _aware(valid_until)
        self._validate_window(valid_from, valid_until, scope)
        license_record = {
            "license_id": license_id,
            "material_id": material_id,
            "holder_id": self.repo.materials[material_id]["holder_id"],
            "created_at": self.clock.now(),
            "versions": [],
        }
        self.repo.licenses[license_id] = license_record
        version = self._append_license_version(
            license_record,
            coverage.GRANT,
            {
                "scope": sorted(set(scope)),
                "territory": territory,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "note": note,
            },
        )
        return version

    def renew_license(
        self,
        license_id: str,
        valid_from: datetime,
        valid_until: datetime,
        scope: list[str] | None = None,
        territory: str | None = None,
        note: str = "",
    ) -> dict:
        """续期：追加新版本，旧版本原样保留。"""
        license_record = self._require_license(license_id)
        self._require_not_revoked(license_record)
        valid_from, valid_until = _aware(valid_from), _aware(valid_until)
        latest = license_record["versions"][-1]
        base_scope = scope if scope is not None else latest["scope"]
        base_territory = territory if territory is not None else latest.get("territory")
        self._validate_window(valid_from, valid_until, base_scope)
        payload = {
            "scope": sorted(set(base_scope)),
            "territory": base_territory,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "note": note,
        }
        return self._append_license_version(license_record, coverage.RENEWAL, payload)

    def revoke_license(self, license_id: str, reason: str, effective_at: datetime | None = None) -> dict:
        """撤销：不可恢复地终止许可，只能再登记新许可/续期版本形成后续链。"""
        license_record = self._require_license(license_id)
        if any(v["kind"] == coverage.REVOCATION for v in license_record["versions"]):
            raise ConflictError("许可已撤销，撤销不能重复")
        return self._append_license_version(
            license_record,
            coverage.REVOCATION,
            {
                "effective_at": _aware(effective_at) if effective_at else self.clock.now(),
                "reason": reason,
            },
        )

    def suspend_license(
        self,
        license_id: str,
        suspend_from: datetime,
        suspend_until: datetime,
        reason: str,
    ) -> dict:
        """临时停用：在给定区间内许可不可用。"""
        license_record = self._require_license(license_id)
        self._require_not_revoked(license_record)
        suspend_from, suspend_until = _aware(suspend_from), _aware(suspend_until)
        if suspend_until <= suspend_from:
            raise ValidationError("停用结束时间必须晚于开始时间")
        if not reason:
            raise ValidationError("停用必须说明原因")
        return self._append_license_version(
            license_record,
            coverage.SUSPENSION,
            {
                "suspend_from": suspend_from,
                "suspend_until": suspend_until,
                "reason": reason,
            },
        )

    def resume_license(self, license_id: str, reason: str, effective_at: datetime | None = None) -> dict:
        """提前恢复：把进行中的停用区间截断到恢复时刻。"""
        license_record = self._require_license(license_id)
        return self._append_license_version(
            license_record,
            coverage.RESUMPTION,
            {
                "effective_at": _aware(effective_at) if effective_at else self.clock.now(),
                "reason": reason,
            },
        )

    def _append_license_version(self, license_record: dict, kind: str, fields: dict) -> dict:
        versions = license_record["versions"]
        seq = len(versions) + 1
        prev_hash = versions[-1]["hash"] if versions else GENESIS
        version = {
            "seq": seq,
            "kind": kind,
            "recorded_at": self.clock.now(),
            "prev_hash": prev_hash,
            **fields,
        }
        version["hash"] = digest(_hashable_version(version))
        versions.append(version)
        return version

    @staticmethod
    def _validate_window(valid_from: datetime, valid_until: datetime, scope: list[str]) -> None:
        if valid_until <= valid_from:
            raise ValidationError("许可结束时间必须晚于开始时间")
        if not scope:
            raise ValidationError("许可范围不能为空")

    @staticmethod
    def _require_not_revoked(license_record: dict) -> None:
        if any(v["kind"] == coverage.REVOCATION for v in license_record["versions"]):
            raise ConflictError(f"许可 {license_record['license_id']} 已撤销，请登记新许可")

    # ============ 教案与引用版本链 ============

    def register_plan(
        self,
        plan_id: str,
        title: str,
        references: list[dict],
    ) -> dict:
        """登记教案。

        references: [{"material_id": ..., "rights": ["演出权", ...]}]
        """
        if plan_id in self.repo.plans:
            raise ConflictError(f"教案已存在：{plan_id}")
        refs = self._normalize_references(references)
        plan = {
            "plan_id": plan_id,
            "title": title,
            "created_at": self.clock.now(),
            "versions": [],
        }
        self.repo.plans[plan_id] = plan
        return self._append_plan_version(plan, "create", refs, replaced=None, command_id=None)

    def replace_plan_materials(
        self,
        mappings: list[dict],
        reason: str,
        command_id: str | None = None,
    ) -> dict:
        """批量替换：一条指令可跨多份教案把素材替换为其登记的替代素材。

        mappings: [{"plan_id": ..., "from_material": ..., "to_material": ...}]
        全部校验通过后才统一落版本链；任一教案非法则整批不生效。
        """
        if not mappings:
            raise ValidationError("替换映射不能为空")
        if command_id and command_id in self.repo.replace_commands:
            raise ConflictError(f"替换指令编号已存在：{command_id}")
        if not reason:
            raise ValidationError("批量替换必须说明原因")

        command_id = command_id or self.repo.next_serial("RPL")
        # 一条指令可对同一教案连续替换多个素材；按教案聚合成一个引用版本。
        by_plan: dict[str, list[dict]] = {}
        for mapping in mappings:
            plan = self._require_plan(mapping["plan_id"])
            old = mapping["from_material"]
            new = mapping["to_material"]
            self._require_material(old)
            self._require_material(new)
            current = by_plan.get(plan["plan_id"])
            base_refs = current[0]["_refs"] if current else self.plan_references(plan["plan_id"])
            if not any(r["material_id"] == old for r in base_refs):
                raise ValidationError(f"教案 {plan['plan_id']} 未引用素材 {old}，无法替换")
            if new not in self.repo.materials[old]["alternative_ids"]:
                raise ValidationError(f"素材 {new} 不是 {old} 已登记的替代素材")
            refs = [
                {"material_id": new, "rights": list(r["rights"])}
                if r["material_id"] == old else dict(r)
                for r in base_refs
            ]
            refs = self._normalize_references(refs)
            by_plan.setdefault(plan["plan_id"], []).append(
                {"plan": plan, "old": old, "new": new, "_refs": refs}
            )

        recorded_at = self.clock.now()
        plan_versions = []
        for items in by_plan.values():
            plan = items[0]["plan"]
            replaced = [{"from_material": it["old"], "to_material": it["new"]} for it in items]
            version = self._append_plan_version(
                plan, "replace", items[-1]["_refs"], replaced=replaced, command_id=command_id
            )
            for it, swap in zip(items, replaced):
                plan_versions.append(
                    {
                        "plan_id": plan["plan_id"],
                        "from_material": swap["from_material"],
                        "to_material": swap["to_material"],
                        "plan_version": version["seq"],
                        "version_hash": version["hash"],
                    }
                )

        command = {
            "command_id": command_id,
            "reason": reason,
            "recorded_at": recorded_at,
            "items": plan_versions,
        }
        self.repo.replace_commands[command_id] = command
        return command

    def _append_plan_version(
        self,
        plan: dict,
        kind: str,
        refs: list[dict],
        replaced: dict | None,
        command_id: str | None,
    ) -> dict:
        versions = plan["versions"]
        seq = len(versions) + 1
        prev_hash = versions[-1]["hash"] if versions else GENESIS
        version = {
            "seq": seq,
            "kind": kind,
            "recorded_at": self.clock.now(),
            "command_id": command_id,
            "replaced": replaced,
            "references": refs,
            "prev_hash": prev_hash,
        }
        version["hash"] = digest(_hashable_version(version))
        versions.append(version)
        return version

    def _normalize_references(self, references: list[dict]) -> list[dict]:
        if not references:
            raise ValidationError("教案至少要引用一个素材")
        normalized = []
        seen = set()
        for ref in references:
            material_id = ref.get("material_id")
            rights = ref.get("rights") or []
            if not material_id:
                raise ValidationError("引用缺少 material_id")
            if material_id in seen:
                raise ValidationError(f"教案重复引用素材：{material_id}")
            if not rights:
                raise ValidationError(f"引用 {material_id} 必须声明所需使用方式")
            self._require_material(material_id)
            seen.add(material_id)
            normalized.append({"material_id": material_id, "rights": sorted(set(rights))})
        normalized.sort(key=lambda r: r["material_id"])
        return normalized

    def plan_version(self, plan_id: str, cutoff: datetime | None = None) -> dict:
        plan = self._require_plan(plan_id)
        versions = plan["versions"]
        if cutoff is not None:
            versions = [v for v in versions if v["recorded_at"] <= cutoff]
            if not versions:
                raise NotFoundError(f"教案在 {cutoff.isoformat()} 之前尚不存在：{plan_id}")
        return versions[-1]

    def plan_references(self, plan_id: str, cutoff: datetime | None = None) -> list[dict]:
        version = self.plan_version(plan_id, cutoff)
        return [dict(r) for r in version["references"]]

    # ============ 场次与发布拦截 ============

    def schedule_session(
        self,
        session_id: str,
        title: str,
        plan_id: str,
        start: datetime,
        end: datetime,
        territory: str | None = None,
    ) -> dict:
        if session_id in self.repo.sessions:
            raise ConflictError(f"场次已存在：{session_id}")
        self._require_plan(plan_id)
        start, end = _aware(start), _aware(end)
        if end <= start:
            raise ValidationError("场次结束时间必须晚于开始时间")
        now = self.clock.now()
        session = {
            "session_id": session_id,
            "title": title,
            "plan_id": plan_id,
            "start": start,
            "end": end,
            "territory": territory,
            "status": "scheduled",
            "created_at": now,
            "published_at": None,
            "completed_at": None,
            "basis": None,
            "events": [{"type": "scheduled", "occurred_at": now}],
        }
        self.repo.sessions[session_id] = session
        return session

    def cancel_session(self, session_id: str, reason: str) -> dict:
        session = self._require_session(session_id)
        if session["status"] in ("completed", "cancelled"):
            raise ConflictError(f"场次当前状态为 {session['status']}，不能撤销")
        now = self.clock.now()
        session["status"] = "cancelled"
        session["events"].append({"type": "cancelled", "occurred_at": now, "reason": reason})
        return session

    def _licenses_for_material(self, material_id: str) -> list[dict]:
        return [lic for lic in self.repo.licenses.values() if lic["material_id"] == material_id]

    def _license_for_material(self, material_id: str) -> dict | None:
        for license_record in self.repo.licenses.values():
            if license_record["material_id"] == material_id:
                return license_record
        return None

    def _evaluate_material(
        self,
        material_id: str,
        rights: set[str],
        session: dict,
        start: datetime,
        end: datetime,
        cutoff: datetime,
        overrides: dict[str, list[dict]] | None = None,
    ) -> dict:
        """跨素材的全部许可评估：任一许可完整覆盖即合规。

        overrides 可把指定许可替换成给定版本前缀，用于影响分析中
        对比某次权利变化前后的结论。
        """
        material = self.repo.materials.get(material_id)
        if material is not None and material.get("license_free"):
            return {
                "material_id": material_id,
                "license_free": True,
                "compliant": True,
                "license_id": None,
                "results": [],
                "violations": [],
            }
        results = []
        all_violations: list[dict] = []
        for license_record in self._licenses_for_material(material_id):
            license_id = license_record["license_id"]
            versions = overrides[license_id] if overrides and license_id in overrides else license_record["versions"]
            violations = coverage.evaluate_window(
                versions, start, end, rights,
                territory=session["territory"], cutoff=cutoff,
            )
            results.append(
                {"license_id": license_id, "compliant": not violations, "violations": violations}
            )
            for violation in violations:
                all_violations.append({"license_id": license_id, **violation})
        if not results:
            all_violations = [{"license_id": None, "reason": NO_LICENSE, "segment_from": start, "segment_to": end}]
            results.append({"license_id": None, "compliant": False, "violations": all_violations})
        compliant_license = next((r["license_id"] for r in results if r["compliant"]), None)
        return {
            "material_id": material_id,
            "license_free": False,
            "compliant": compliant_license is not None,
            "license_id": compliant_license or results[0]["license_id"],
            "results": results,
            "violations": all_violations,
        }

    def _material_violations(
        self,
        material_id: str,
        rights: set[str],
        session: dict,
        start: datetime,
        end: datetime,
        cutoff: datetime,
    ) -> list[dict]:
        """评估单素材在给定时段的违规分段；公有领域素材始终合规。"""
        return self._evaluate_material(material_id, rights, session, start, end, cutoff)["violations"]

    def _session_checks(self, session: dict, cutoff: datetime) -> list[dict]:
        """逐素材评估场次全时段的许可覆盖（一个素材有多份许可时任一覆盖即可）。"""
        refs = self.plan_references(session["plan_id"], cutoff=cutoff)
        checks = []
        for ref in refs:
            evaluation = self._evaluate_material(
                ref["material_id"], set(ref["rights"]), session,
                session["start"], session["end"], cutoff,
            )
            violations = evaluation["violations"]
            checks.append(
                {
                    "material_id": ref["material_id"],
                    "license_id": evaluation["license_id"],
                    "license_free": evaluation["license_free"],
                    "compliant": evaluation["compliant"],
                    "reason": violations[0]["reason"] if violations else None,
                    "violations": violations,
                }
            )
        return checks

    def publish_session(self, session_id: str) -> dict:
        """发布闸门：任一素材在任一时段不合规即整体拦截。"""
        session = self._require_session(session_id)
        if session["status"] != "scheduled":
            raise ConflictError(f"只有待发布场次可以发布，当前状态：{session['status']}")
        now = self.clock.now()
        checks = self._session_checks(session, cutoff=now)
        violations = [c for c in checks if not c["compliant"]]
        if violations:
            raise PublicationBlocked(
                f"场次 {session_id} 存在不合规素材引用，发布已拦截",
                violations,
            )
        session["status"] = "published"
        session["published_at"] = now
        session["events"].append({"type": "published", "occurred_at": now})
        return session

    def complete_session(self, session_id: str) -> dict:
        """完成场次：冻结当时实际依据，事后权利变化不再追溯。"""
        session = self._require_session(session_id)
        if session["status"] != "published":
            raise ConflictError(f"只有已发布场次可以标记完成，当前状态：{session['status']}")
        now = self.clock.now()
        checks = self._session_checks(session, cutoff=now)
        violations = [c for c in checks if not c["compliant"]]
        if violations:
            raise PublicationBlocked(
                f"场次 {session_id} 完成时仍存在不合规引用，不能冻结合规依据",
                violations,
            )
        basis = []
        refs_at_now = self.plan_references(session["plan_id"], cutoff=now)
        for check in checks:
            ref = next(r for r in refs_at_now if r["material_id"] == check["material_id"])
            if check.get("license_free"):
                basis.append(
                    {
                        "material_id": check["material_id"],
                        "license_id": None,
                        "rights": ref["rights"],
                        "basis": "license_free",
                    }
                )
                continue
            license_record = self.repo.licenses[check["license_id"]]
            status = coverage.instant_status(
                license_record["versions"],
                session["start"],
                set(ref["rights"]),
                territory=session["territory"],
            )
            grant = license_record["versions"][status["grant_version"] - 1]
            basis.append(
                {
                    "material_id": check["material_id"],
                    "license_id": check["license_id"],
                    "rights": ref["rights"],
                    "basis": "license_grant",
                    "grant_version": grant["seq"],
                    "license_hash": grant["hash"],
                    "valid_from": grant["valid_from"],
                    "valid_until": grant["valid_until"],
                }
            )
        frozen = {
            "frozen_at": now,
            "plan_version": self.plan_version(session["plan_id"], cutoff=now)["seq"],
            "items": basis,
        }
        frozen["basis_hash"] = digest(_hashable_version({k: v for k, v in frozen.items()}))
        session["status"] = "completed"
        session["completed_at"] = now
        session["basis"] = frozen
        session["events"].append(
            {"type": "completed", "occurred_at": now, "basis_hash": frozen["basis_hash"]}
        )
        return session

    # ============ 批量巡检（幂等可重跑） ============

    def run_patrol(
        self,
        batch_id: str,
        as_of: datetime | None = None,
        horizon: datetime | None = None,
    ) -> dict:
        """在 ``[as_of, horizon)`` 窗口内批量扫描未来场次，生成/消解风险案件。

        - batch_id 是幂等键：同一编号重复提交直接返回首次结果。
        - 同一（场次, 素材）风险未消除前永远只有一个开放案件。
        - 重新扫描发现已合规（续期/替换/取消/完成）则在该批次消解案件。
        """
        if batch_id in self.repo.patrol_batches:
            stored = self.repo.patrol_batches[batch_id]
            return {**stored, "idempotent_replay": True}

        as_of = _aware(as_of) if as_of else self.clock.now()
        horizon = _aware(horizon) if horizon else as_of + timedelta(days=30)
        if horizon <= as_of:
            raise ValidationError("巡检截止时间必须晚于起始时间")

        opened: list[str] = []
        reopened: list[str] = []
        resolved: list[dict] = []
        reconfirmed: list[str] = []
        scanned_pairs: set[tuple[str, str]] = set()

        for session in self._future_sessions(as_of, horizon):
            window_start = max(session["start"], as_of)
            window_end = min(session["end"], horizon)
            refs = self.plan_references(session["plan_id"], cutoff=as_of)
            for ref in refs:
                material_id = ref["material_id"]
                scanned_pairs.add((session["session_id"], material_id))
                violations = self._violations_in_window(
                    material_id, set(ref["rights"]), session, window_start, window_end, as_of
                )
                case = self._find_case(session["session_id"], material_id)
                if violations:
                    if case is None:
                        new_case = self._open_case(
                            batch_id, as_of, session, material_id, ref, window_start, window_end, violations
                        )
                        opened.append(new_case["case_id"])
                    elif case["status"] == "open":
                        reconfirmed.append(case["case_id"])
                    else:
                        case["status"] = "open"
                        case["history"].append(
                            {
                                "type": "reopened",
                                "batch_id": batch_id,
                                "at": as_of,
                                "violations": violations,
                            }
                        )
                        reopened.append(case["case_id"])
                elif case is not None and case["status"] == "open":
                    reason = self._resolution_reason(session, material_id, refs, as_of)
                    self._resolve_case(case, batch_id, as_of, reason, violations)
                    resolved.append({"case_id": case["case_id"], "reason": reason})

        # 场次已不在未来范围（完成/取消）或素材已被替换：消解遗留开放案件
        for case in list(self.repo.risk_cases.values()):
            if case["status"] != "open":
                continue
            key = (case["session_id"], case["material_id"])
            if key in scanned_pairs:
                continue
            session = self.repo.sessions.get(case["session_id"])
            reason = None
            if session is None:
                continue
            if session["status"] == "completed":
                reason = "session_completed"
            elif session["status"] == "cancelled":
                reason = "session_cancelled"
            elif as_of >= session["end"]:
                reason = "session_out_of_window"
            else:
                refs_now = self.plan_references(session["plan_id"], cutoff=as_of)
                if not any(r["material_id"] == case["material_id"] for r in refs_now):
                    reason = "material_replaced"
            if reason:
                self._resolve_case(case, batch_id, as_of, reason, [])
                resolved.append({"case_id": case["case_id"], "reason": reason})

        batch = {
            "batch_id": batch_id,
            "as_of": as_of,
            "horizon": horizon,
            "created_at": self.clock.now(),
            "scanned_sessions": [
                s["session_id"] for s in self._future_sessions(as_of, horizon)
            ],
            "opened": opened,
            "reopened": reopened,
            "reconfirmed": reconfirmed,
            "resolved": resolved,
            "open_case_ids": [
                c["case_id"]
                for c in self.repo.risk_cases.values()
                if c["status"] == "open"
            ],
            "idempotent_replay": False,
        }
        self.repo.patrol_batches[batch_id] = batch
        return batch

    def _violations_in_window(
        self,
        material_id: str,
        rights: set[str],
        session: dict,
        window_start: datetime,
        window_end: datetime,
        as_of: datetime,
    ) -> list[dict]:
        return self._material_violations(
            material_id, rights, session, window_start, window_end, as_of
        )

    def _open_case(self, batch_id, as_of, session, material_id, ref, window_start, window_end, violations):
        license_record = self._license_for_material(material_id)
        case = {
            "case_id": self.repo.next_serial("RISK"),
            "session_id": session["session_id"],
            "plan_id": session["plan_id"],
            "material_id": material_id,
            "license_id": license_record["license_id"] if license_record else None,
            "required_rights": ref["rights"],
            "status": "open",
            "history": [
                {
                    "type": "opened",
                    "batch_id": batch_id,
                    "at": as_of,
                    "window_start": window_start,
                    "window_end": window_end,
                    "violations": violations,
                }
            ],
        }
        self.repo.risk_cases[case["case_id"]] = case
        return case

    def _resolve_case(self, case: dict, batch_id: str, as_of: datetime, reason: str, violations: list) -> None:
        case["status"] = "resolved"
        case["history"].append(
            {
                "type": "resolved",
                "batch_id": batch_id,
                "at": as_of,
                "reason": reason,
            }
        )

    def _resolution_reason(self, session: dict, material_id: str, refs_now: list[dict], as_of: datetime) -> str:
        if session["status"] == "completed":
            return "session_completed"
        if not any(r["material_id"] == material_id for r in refs_now):
            return "material_replaced"
        license_record = self._license_for_material(material_id)
        if license_record and any(
            v["kind"] in (coverage.RENEWAL, coverage.GRANT) and v["recorded_at"] <= as_of
            for v in license_record["versions"]
        ):
            latest = license_record["versions"][-1]
            if latest["kind"] in (coverage.RENEWAL, coverage.GRANT):
                return "renewed"
        return "compliant_on_rerun"

    def _future_sessions(self, as_of: datetime, horizon: datetime) -> list[dict]:
        return [
            s
            for s in self.repo.sessions.values()
            if s["status"] in ("scheduled", "published")
            and s["end"] > as_of
            and s["start"] < horizon
        ]

    def _find_case(self, session_id: str, material_id: str) -> dict | None:
        for case in self.repo.risk_cases.values():
            if case["session_id"] == session_id and case["material_id"] == material_id:
                return case
        return None

    # ============ 权利变化影响分析 ============

    def impact_of_license_version(self, license_id: str, seq: int) -> dict:
        """列出某个许可版本（续期/撤销/停用/恢复/首登）影响的全部未来安排。"""
        license_record = self._require_license(license_id)
        if seq < 1 or seq > len(license_record["versions"]):
            raise NotFoundError(f"许可版本不存在：{license_id}#v{seq}")
        change = license_record["versions"][seq - 1]
        before = license_record["versions"][: seq - 1]
        after = license_record["versions"][:seq]
        now = self.clock.now()
        material_id = license_record["material_id"]

        arrangements = []
        for session in self.repo.sessions.values():
            if session["status"] not in ("scheduled", "published") or session["end"] <= now:
                continue
            refs = self.plan_references(session["plan_id"], cutoff=now)
            ref = next((r for r in refs if r["material_id"] == material_id), None)
            if ref is None:
                continue
            # 素材的其它许可在变化前后不变，仅替换本许可的版本前缀
            base = self._evaluate_material(
                material_id, set(ref["rights"]), session,
                session["start"], session["end"], now,
            )
            if before:
                before_eval = self._evaluate_material(
                    material_id, set(ref["rights"]), session,
                    session["start"], session["end"], now,
                    overrides={license_id: before},
                )
                before_compliant = before_eval["compliant"]
                before_reasons = _reason_set(before_eval)
            else:
                # 首登之前：把本许可视为不存在
                others = [r for r in base["results"] if r["license_id"] != license_id]
                before_compliant = any(r["compliant"] for r in others)
                before_reasons = (
                    set() if before_compliant
                    else {v["reason"] for r in others for v in r["violations"]}
                    or {coverage.NO_GRANT}
                )
            after_eval = self._evaluate_material(
                material_id, set(ref["rights"]), session,
                session["start"], session["end"], now,
                overrides={license_id: after},
            )
            after_compliant = after_eval["compliant"]
            after_reasons = _reason_set(after_eval)
            arrangements.append(
                {
                    "session_id": session["session_id"],
                    "title": session["title"],
                    "plan_id": session["plan_id"],
                    "start": session["start"],
                    "end": session["end"],
                    "status": session["status"],
                    "reference_path": [material_id, session["plan_id"], session["session_id"]],
                    "before_compliant": before_compliant,
                    "after_compliant": after_compliant,
                    "transition": _transition(not before_compliant, not after_compliant),
                    "before_reasons": sorted(before_reasons),
                    "after_reasons": sorted(after_reasons),
                }
            )
        arrangements.sort(key=lambda a: a["start"])
        return {
            "change_type": "license_version",
            "license_id": license_id,
            "material_id": material_id,
            "seq": seq,
            "kind": change["kind"],
            "recorded_at": change["recorded_at"],
            "version_hash": change["hash"],
            "affected_arrangements": arrangements,
        }

    def impact_of_replace_command(self, command_id: str) -> dict:
        """批量替换指令对未来场次的影响：素材去向与替换前后合规对比。"""
        command = self.repo.replace_commands.get(command_id)
        if command is None:
            raise NotFoundError(f"替换指令不存在：{command_id}")
        now = self.clock.now()
        touched_plans = {item["plan_id"] for item in command["items"]}

        arrangements = []
        for session in self.repo.sessions.values():
            if session["plan_id"] not in touched_plans:
                continue
            if session["status"] not in ("scheduled", "published") or session["end"] <= now:
                continue
            plan = self.repo.plans[session["plan_id"]]
            cmd_versions = [
                v for v in plan["versions"] if v.get("command_id") == command_id
            ]
            cmd_version = cmd_versions[0]
            before_refs = plan["versions"][cmd_version["seq"] - 2]["references"]
            after_refs = cmd_version["references"]
            before_bad = self._refs_violate(before_refs, session, now)
            after_bad = self._refs_violate(after_refs, session, now)
            for swap in cmd_version["replaced"]:
                arrangements.append(
                    {
                        "session_id": session["session_id"],
                        "title": session["title"],
                        "plan_id": session["plan_id"],
                        "start": session["start"],
                        "end": session["end"],
                        "status": session["status"],
                        "reference_path": [
                            swap["from_material"], session["plan_id"], session["session_id"],
                        ],
                        "from_material": swap["from_material"],
                        "to_material": swap["to_material"],
                        "before_compliant": not before_bad,
                        "after_compliant": not after_bad,
                        "transition": _transition(bool(before_bad), bool(after_bad)),
                        "before_reasons": before_bad,
                        "after_reasons": after_bad,
                    }
                )
        arrangements.sort(key=lambda a: a["start"])
        return {
            "change_type": "replace_command",
            "command_id": command_id,
            "recorded_at": command["recorded_at"],
            "items": command["items"],
            "affected_arrangements": arrangements,
        }

    def impact_of_material(self, material_id: str) -> dict:
        """列出素材当前关联到的全部未来场次（权利变化影响面的快捷查询）。"""
        self._require_material(material_id)
        now = self.clock.now()
        arrangements = []
        for session in self.repo.sessions.values():
            if session["status"] not in ("scheduled", "published") or session["end"] <= now:
                continue
            refs = self.plan_references(session["plan_id"], cutoff=now)
            ref = next((r for r in refs if r["material_id"] == material_id), None)
            if ref is None:
                continue
            bad = self._refs_violate([ref], session, now)
            arrangements.append(
                {
                    "session_id": session["session_id"],
                    "title": session["title"],
                    "plan_id": session["plan_id"],
                    "start": session["start"],
                    "end": session["end"],
                    "status": session["status"],
                    "reference_path": [material_id, session["plan_id"], session["session_id"]],
                    "compliant": not bad,
                    "reasons": bad,
                }
            )
        arrangements.sort(key=lambda a: a["start"])
        return {"material_id": material_id, "affected_arrangements": arrangements}

    def _refs_violate(self, refs: list[dict], session: dict, cutoff: datetime) -> list[str]:
        reasons = set()
        for ref in refs:
            violations = self._material_violations(
                ref["material_id"], set(ref["rights"]), session,
                session["start"], session["end"], cutoff,
            )
            reasons.update(v["reason"] for v in violations)
        return sorted(reasons)

    # ============ 版本链校验 ============

    def verify_chains(self) -> dict:
        """重算全部许可链与教案引用链的摘要，发现任何篡改即报错。"""
        broken = []
        for license_record in self.repo.licenses.values():
            err = _verify_chain(
                license_record["license_id"], license_record["versions"]
            )
            if err:
                broken.append(err)
        for plan in self.repo.plans.values():
            err = _verify_chain(plan["plan_id"], plan["versions"])
            if err:
                broken.append(err)
        if broken:
            raise ChainIntegrityError("版本链校验失败：" + "；".join(broken))
        return {
            "ok": True,
            "license_chains": len(self.repo.licenses),
            "plan_chains": len(self.repo.plans),
        }

    # ============ 查询接口 ============

    def list_open_cases(self) -> list[dict]:
        return [c for c in self.repo.risk_cases.values() if c["status"] == "open"]

    def get_session(self, session_id: str) -> dict:
        return self._require_session(session_id)

    def get_license(self, license_id: str) -> dict:
        return self._require_license(license_id)

    def get_plan(self, plan_id: str) -> dict:
        return self._require_plan(plan_id)

    def get_case(self, case_id: str) -> dict:
        case = self.repo.risk_cases.get(case_id)
        if case is None:
            raise NotFoundError(f"风险案件不存在：{case_id}")
        return case

    # ---- 内部查找 ----

    def _license_for_material(self, material_id: str) -> dict | None:
        for license_record in self.repo.licenses.values():
            if license_record["material_id"] == material_id:
                return license_record
        return None

    def _require_holder(self, holder_id: str) -> dict:
        holder = self.repo.holders.get(holder_id)
        if holder is None:
            raise NotFoundError(f"权利主体未登记：{holder_id}")
        return holder

    def _require_material(self, material_id: str) -> dict:
        material = self.repo.materials.get(material_id)
        if material is None:
            raise NotFoundError(f"素材未登记：{material_id}")
        return material

    def _require_license(self, license_id: str) -> dict:
        license_record = self.repo.licenses.get(license_id)
        if license_record is None:
            raise NotFoundError(f"许可未登记：{license_id}")
        return license_record

    def _require_plan(self, plan_id: str) -> dict:
        plan = self.repo.plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"教案未登记：{plan_id}")
        return plan

    def _require_session(self, session_id: str) -> dict:
        session = self.repo.sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"场次未登记：{session_id}")
        return session


def _reason_set(evaluation: dict) -> set[str]:
    return {v["reason"] for v in evaluation["violations"]}


def _transition(before_bad: bool, after_bad: bool) -> str:
    if before_bad and not after_bad:
        return "risk_cleared"
    if not before_bad and after_bad:
        return "risk_introduced"
    return "unchanged_compliant" if not after_bad else "unchanged_noncompliant"


def _hashable_version(version: dict) -> dict:
    """计算摘要时排除自身 hash 字段。"""
    return {k: v for k, v in version.items() if k != "hash"}


def _verify_chain(entity_id: str, versions: list[dict]) -> str | None:
    prev_hash = GENESIS
    for idx, version in enumerate(versions, start=1):
        if version["seq"] != idx:
            return f"{entity_id}#v{idx} 序号断裂"
        if version["prev_hash"] != prev_hash:
            return f"{entity_id}#v{idx} 前序摘要不匹配"
        expected = digest(_hashable_version(version))
        if version["hash"] != expected:
            return f"{entity_id}#v{idx} 内容摘要不匹配"
        prev_hash = version["hash"]
    return None
