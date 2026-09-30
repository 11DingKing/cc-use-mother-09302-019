"""端到端场景测试：覆盖登记、版本链、发布拦截、批量巡检幂等、
已完成活动依据冻结、批量替换与权利变化影响查询。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copyright_patrol import (  # noqa: E402
    CopyrightService,
    FixedClock,
    PublicationBlocked,
    Repository,
)
from copyright_patrol.errors import ChainIntegrityError, ConflictError, NotFoundError, ValidationError  # noqa: E402

UTC = timezone.utc


def dt(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


class ScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(dt(2026, 8, 1))
        self.svc = CopyrightService(Repository(), self.clock)
        self._seed()

    def _seed(self) -> None:
        s = self.svc
        s.register_holder("H-MUSIC", "民乐版权集体管理组织")
        # M1 音乐许可：2026-09-01 至 2026-09-30，含演出权与录制权
        s.register_material("M2", "公有领域丝竹录音", "audio", "H-MUSIC", license_free=True)
        s.register_material("M1", "江南丝竹录音", "audio", "H-MUSIC", alternative_ids=["M2"])
        s.register_license(
            "L1", "M1", ["演出权", "录制权"], dt(2026, 9, 1), dt(2026, 9, 30),
        )
        s.register_plan(
            "P1", "非遗音乐启蒙教案",
            [{"material_id": "M1", "rights": ["演出权"]}],
        )

    # ---- 发布闸门 ----

    def test_valid_session_can_publish(self) -> None:
        self.svc.schedule_session("S1", "九月公开课", "P1", dt(2026, 9, 10, 14), dt(2026, 9, 10, 15))
        self.svc.publish_session("S1")
        self.assertEqual(self.svc.get_session("S1")["status"], "published")

    def test_expired_license_blocks_publication(self) -> None:
        # 10 月场次：许可 9-30 到期
        self.svc.schedule_session("S2", "十月公开课", "P1", dt(2026, 10, 10, 14), dt(2026, 10, 10, 15))
        with self.assertRaises(PublicationBlocked) as ctx:
            self.svc.publish_session("S2")
        reason = ctx.exception.violations[0]["reason"]
        self.assertEqual(reason, "license_expired")

    def test_session_straddling_expiry_is_blocked(self) -> None:
        # 场次跨越到期点：覆盖全时段而非单点，必须拦截
        self.svc.schedule_session("S3", "跨月课", "P1", dt(2026, 9, 29, 23), dt(2026, 10, 1, 1))
        with self.assertRaises(PublicationBlocked):
            self.svc.publish_session("S3")

    def test_scope_gap_blocks_publication(self) -> None:
        self.svc.register_plan(
            "P2", "录制版教案",
            [{"material_id": "M1", "rights": ["信息网络传播权"]}],
        )
        self.svc.schedule_session("S4", "网络课", "P2", dt(2026, 9, 10), dt(2026, 9, 10, 2))
        with self.assertRaises(PublicationBlocked) as ctx:
            self.svc.publish_session("S4")
        self.assertEqual(ctx.exception.violations[0]["reason"], "scope_not_covered")

    def test_suspension_blocks_and_resumption_clears(self) -> None:
        self.svc.schedule_session("S5", "中秋课", "P1", dt(2026, 9, 15, 14), dt(2026, 9, 15, 15))
        self.svc.suspend_license("L1", dt(2026, 9, 14), dt(2026, 9, 16), "版权争议调查")
        with self.assertRaises(PublicationBlocked) as ctx:
            self.svc.publish_session("S5")
        self.assertEqual(ctx.exception.violations[0]["reason"], "license_suspended")
        # 提前恢复
        self.svc.resume_license("L1", "争议解除", effective_at=dt(2026, 9, 14, 12))
        self.svc.publish_session("S5")
        self.assertEqual(self.svc.get_session("S5")["status"], "published")

    def test_revocation_blocks_future_but_keeps_past_basis(self) -> None:
        self.svc.schedule_session("S6", "九月课", "P1", dt(2026, 9, 10, 14), dt(2026, 9, 10, 15))
        self.svc.publish_session("S6")
        self.clock.set(dt(2026, 9, 12))
        self.svc.complete_session("S6")
        basis = self.svc.get_session("S6")["basis"]
        self.assertTrue(basis["basis_hash"].startswith("sha256:"))
        self.assertEqual(basis["items"][0]["license_id"], "L1")

        # 事后撤销：新场次被拦截
        self.svc.revoke_license("L1", "合作终止", effective_at=dt(2026, 9, 13))
        self.svc.schedule_session("S7", "九月下旬课", "P1", dt(2026, 9, 20, 14), dt(2026, 9, 20, 15))
        with self.assertRaises(PublicationBlocked) as ctx:
            self.svc.publish_session("S7")
        self.assertEqual(ctx.exception.violations[0]["reason"], "license_revoked")
        # 已完成场次状态与依据不变
        done = self.svc.get_session("S6")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["basis"]["basis_hash"], basis["basis_hash"])

    # ---- 版本链 ----

    def test_renewal_extends_chain_and_unblocks(self) -> None:
        self.svc.schedule_session("S8", "十月课", "P1", dt(2026, 10, 10, 14), dt(2026, 10, 10, 15))
        with self.assertRaises(PublicationBlocked):
            self.svc.publish_session("S8")
        self.svc.renew_license("L1", dt(2026, 9, 30), dt(2026, 12, 31))
        # 旧版本原样保留，链上为第 2 版
        versions = self.svc.get_license("L1")["versions"]
        self.assertEqual([v["seq"] for v in versions], [1, 2])
        self.assertEqual(versions[1]["kind"], "renewal")
        self.svc.publish_session("S8")
        result = self.svc.verify_chains()
        self.assertTrue(result["ok"])

    def test_chain_detects_tampering(self) -> None:
        self.svc.renew_license("L1", dt(2026, 9, 30), dt(2026, 12, 31))
        # 模拟外部篡改历史版本
        self.svc.repo.licenses["L1"]["versions"][0]["scope"] = ["被篡改"]
        with self.assertRaises(ChainIntegrityError):
            self.svc.verify_chains()

    def test_double_revoke_rejected(self) -> None:
        self.svc.revoke_license("L1", "终止")
        with self.assertRaises(ConflictError):
            self.svc.revoke_license("L1", "再次终止")

    # ---- 巡检幂等 ----

    def test_patrol_is_idempotent_and_resolves_after_renewal(self) -> None:
        self.svc.schedule_session("S9", "十月课A", "P1", dt(2026, 10, 5, 14), dt(2026, 10, 5, 15))
        self.svc.schedule_session("S10", "十月课B", "P1", dt(2026, 10, 6, 14), dt(2026, 10, 6, 15))
        self.svc.schedule_session("S11", "九月课", "P1", dt(2026, 9, 5, 14), dt(2026, 9, 5, 15))

        first = self.svc.run_patrol("B-001", as_of=dt(2026, 9, 1), horizon=dt(2026, 11, 1))
        self.assertEqual(len(first["opened"]), 2)  # 只开十月两场
        # 九月合规场次同样在扫描范围内，但不会立案
        self.assertIn("S11", first["scanned_sessions"])
        open_sessions = {self.svc.get_case(c)["session_id"] for c in first["opened"]}
        self.assertNotIn("S11", open_sessions)

        # 同批次重跑：完全幂等
        replay = self.svc.run_patrol("B-001", as_of=dt(2026, 9, 1), horizon=dt(2026, 11, 1))
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["opened"], first["opened"])
        self.assertEqual(len(self.svc.list_open_cases()), 2)

        # 续期后用新批次重扫：旧案件消解，不新增重复案件
        self.svc.renew_license("L1", dt(2026, 9, 30), dt(2026, 12, 31))
        second = self.svc.run_patrol("B-002", as_of=dt(2026, 9, 2), horizon=dt(2026, 11, 1))
        self.assertEqual(second["opened"], [])
        self.assertEqual(len(second["resolved"]), 2)
        self.assertEqual(self.svc.list_open_cases(), [])
        # 同一(场次,素材)始终只有一个案件，且消解原因留痕
        for item in second["resolved"]:
            case = self.svc.get_case(item["case_id"])
            self.assertEqual(case["status"], "resolved")
            self.assertEqual([h["type"] for h in case["history"]], ["opened", "resolved"])

    def test_patrol_resolves_after_replacement(self) -> None:
        self.svc.schedule_session("S12", "十月课", "P1", dt(2026, 10, 5, 14), dt(2026, 10, 5, 15))
        self.svc.run_patrol("B-101", as_of=dt(2026, 9, 1), horizon=dt(2026, 11, 1))
        self.assertEqual(len(self.svc.list_open_cases()), 1)
        self.svc.replace_plan_materials(
            [{"plan_id": "P1", "from_material": "M1", "to_material": "M2"}],
            reason="许可到期，切换公有领域素材",
            command_id="RPL-1",
        )
        result = self.svc.run_patrol("B-102", as_of=dt(2026, 9, 2), horizon=dt(2026, 11, 1))
        self.assertEqual(result["resolved"][0]["reason"], "material_replaced")
        self.assertEqual(self.svc.list_open_cases(), [])

    def test_patrol_resolves_after_cancel_and_complete(self) -> None:
        self.svc.schedule_session("S13", "十月课", "P1", dt(2026, 10, 5, 14), dt(2026, 10, 5, 15))
        self.svc.run_patrol("B-201", as_of=dt(2026, 9, 1), horizon=dt(2026, 11, 1))
        self.svc.cancel_session("S13", "主讲请假")
        result = self.svc.run_patrol("B-202", as_of=dt(2026, 9, 2), horizon=dt(2026, 11, 1))
        self.assertEqual(result["resolved"][0]["reason"], "session_cancelled")

    # ---- 批量替换 ----

    def test_replacement_requires_registered_alternative(self) -> None:
        # 未登记为替代素材的目标不允许替换
        self.svc.register_material("M3", "其它录音", "audio", "H-MUSIC")
        with self.assertRaises(ValidationError):
            self.svc.replace_plan_materials(
                [{"plan_id": "P1", "from_material": "M1", "to_material": "M3"}],
                reason="试换",
            )

    def test_replacement_is_atomic_and_versioned(self) -> None:
        self.svc.register_plan(
            "P3", "另一份教案",
            [{"material_id": "M1", "rights": ["演出权"]}],
        )
        self.svc.register_material("M9", "未建立替代关系的素材", "audio", "H-MUSIC")
        with self.assertRaises(ValidationError):
            self.svc.replace_plan_materials(
                [
                    {"plan_id": "P1", "from_material": "M1", "to_material": "M2"},
                    {"plan_id": "P3", "from_material": "M1", "to_material": "M9"},
                ],
                reason="整批替换（含非法项）",
            )
        # 整批失败：P1 没有产生任何新版本
        self.assertEqual(len(self.svc.get_plan("P1")["versions"]), 1)

        command = self.svc.replace_plan_materials(
            [
                {"plan_id": "P1", "from_material": "M1", "to_material": "M2"},
                {"plan_id": "P3", "from_material": "M1", "to_material": "M2"},
            ],
            reason="到期批量替换",
        )
        self.assertEqual(len(command["items"]), 2)
        refs = self.svc.plan_references("P1")
        self.assertEqual(refs[0]["material_id"], "M2")
        # 版本链完整：create -> replace
        versions = self.svc.get_plan("P1")["versions"]
        self.assertEqual([v["kind"] for v in versions], ["create", "replace"])
        self.svc.verify_chains()

    # ---- 影响查询 ----

    def test_impact_lists_every_future_arrangement(self) -> None:
        self.svc.schedule_session("S20", "十月课", "P1", dt(2026, 10, 5, 14), dt(2026, 10, 5, 15))
        self.svc.schedule_session("S21", "十一月课", "P1", dt(2026, 11, 5, 14), dt(2026, 11, 5, 15))
        # 已完成的历史场次不应出现在影响列表中
        self.svc.schedule_session("S22", "九月课", "P1", dt(2026, 9, 5, 14), dt(2026, 9, 5, 15))
        self.svc.publish_session("S22")
        self.clock.set(dt(2026, 9, 6))
        self.svc.complete_session("S22")
        self.clock.set(dt(2026, 9, 7))

        impact = self.svc.impact_of_license_version("L1", 1)
        ids = {a["session_id"] for a in impact["affected_arrangements"]}
        self.assertEqual(ids, {"S20", "S21"})
        path = impact["affected_arrangements"][0]["reference_path"]
        self.assertEqual(path, ["M1", "P1", "S20"])

        # 续期前后对比：risk_cleared
        self.svc.renew_license("L1", dt(2026, 9, 30), dt(2026, 12, 31))
        impact2 = self.svc.impact_of_license_version("L1", 2)
        by_session = {a["session_id"]: a for a in impact2["affected_arrangements"]}
        self.assertEqual(by_session["S20"]["transition"], "risk_cleared")
        self.assertTrue(by_session["S20"]["after_compliant"])

    def test_revocation_impact_introduces_risk(self) -> None:
        self.svc.schedule_session("S23", "九月下旬课", "P1", dt(2026, 9, 20, 14), dt(2026, 9, 20, 15))
        self.svc.revoke_license("L1", "终止", effective_at=dt(2026, 9, 15))
        impact = self.svc.impact_of_license_version("L1", 2)
        self.assertEqual(impact["kind"], "revocation")
        self.assertEqual(impact["affected_arrangements"][0]["transition"], "risk_introduced")

    def test_replacement_impact(self) -> None:
        self.svc.schedule_session("S24", "十月课", "P1", dt(2026, 10, 5, 14), dt(2026, 10, 5, 15))
        self.svc.replace_plan_materials(
            [{"plan_id": "P1", "from_material": "M1", "to_material": "M2"}],
            reason="替换", command_id="RPL-9",
        )
        impact = self.svc.impact_of_replace_command("RPL-9")
        item = impact["affected_arrangements"][0]
        self.assertEqual(item["session_id"], "S24")
        self.assertEqual(item["transition"], "risk_cleared")
        self.assertEqual(item["from_material"], "M1")
        self.assertEqual(item["to_material"], "M2")

    # ---- 多份许可与撤销后重签 ----

    def test_new_license_after_revocation_restores_compliance(self) -> None:
        self.svc.schedule_session("S50", "九月课", "P1", dt(2026, 9, 20, 14), dt(2026, 9, 20, 15))
        self.svc.revoke_license("L1", "合作终止", effective_at=dt(2026, 9, 10))
        with self.assertRaises(PublicationBlocked):
            self.svc.publish_session("S50")
        # 已撤销许可不能再续期或停用
        with self.assertRaises(ConflictError):
            self.svc.renew_license("L1", dt(2026, 9, 10), dt(2026, 12, 31))
        with self.assertRaises(ConflictError):
            self.svc.suspend_license("L1", dt(2026, 9, 15), dt(2026, 9, 16), "争议")
        # 为同一素材登记新许可后恢复合规
        self.clock.set(dt(2026, 9, 11))
        self.svc.register_license("L2", "M1", ["演出权"], dt(2026, 9, 1), dt(2026, 12, 31))
        self.svc.publish_session("S50")
        self.assertEqual(self.svc.get_session("S50")["status"], "published")

    def test_territory_mismatch_blocks_publication(self) -> None:
        self.svc.register_material("M5", "受限地域录音", "audio", "H-MUSIC")
        self.svc.register_license(
            "L5", "M5", ["演出权"], dt(2026, 9, 1), dt(2026, 12, 31), territory="境内",
        )
        self.svc.register_plan(
            "P5", "境外教案", [{"material_id": "M5", "rights": ["演出权"]}],
        )
        self.svc.schedule_session(
            "S51", "境外课", "P5", dt(2026, 9, 20, 14), dt(2026, 9, 20, 15), territory="海外",
        )
        with self.assertRaises(PublicationBlocked) as ctx:
            self.svc.publish_session("S51")
        self.assertEqual(ctx.exception.violations[0]["reason"], "territory_not_covered")
        # 同地域场次可发布
        self.svc.schedule_session(
            "S52", "境内课", "P5", dt(2026, 9, 21, 14), dt(2026, 9, 21, 15), territory="境内",
        )
        self.svc.publish_session("S52")

    def test_resume_without_suspension_keeps_chain_valid(self) -> None:
        # 恢复版本本身允许追加（事后操作），链完整性保持
        self.svc.resume_license("L1", "行政误操作回滚")
        self.svc.verify_chains()

    # ---- 无许可素材 ----

    def test_unlicensed_material_blocks_and_patrols(self) -> None:
        self.svc.register_material("M4", "无许可素材", "audio", "H-MUSIC")
        self.svc.register_plan("P4", "风险教案", [{"material_id": "M4", "rights": ["演出权"]}])
        self.svc.schedule_session("S30", "风险课", "P4", dt(2026, 10, 5), dt(2026, 10, 5, 2))
        with self.assertRaises(PublicationBlocked) as ctx:
            self.svc.publish_session("S30")
        self.assertEqual(ctx.exception.violations[0]["reason"], "no_license_registered")
        batch = self.svc.run_patrol("B-301", as_of=dt(2026, 9, 1), horizon=dt(2026, 11, 1))
        self.assertEqual(len(batch["opened"]), 1)

    # ---- 持久化往返 ----

    def test_snapshot_roundtrip(self) -> None:
        import tempfile

        self.svc.schedule_session("S40", "十月课", "P1", dt(2026, 10, 5), dt(2026, 10, 5, 1))
        self.svc.run_patrol("B-401", as_of=dt(2026, 9, 1), horizon=dt(2026, 11, 1))
        self.svc.renew_license("L1", dt(2026, 9, 30), dt(2026, 12, 31))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            self.svc.repo.save(path)
            restored = CopyrightService(Repository.load(path), self.clock)
            self.assertEqual(restored.get_session("S40")["title"], "十月课")
            self.assertEqual(len(restored.get_license("L1")["versions"]), 2)
            self.assertEqual(len(restored.list_open_cases()), 1)
            restored.verify_chains()
            # 批次幂等键在重启后仍然生效
            replay = restored.run_patrol("B-401", as_of=dt(2026, 9, 1), horizon=dt(2026, 11, 1))
            self.assertTrue(replay["idempotent_replay"])


if __name__ == "__main__":
    unittest.main()
