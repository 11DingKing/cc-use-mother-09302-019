"""HTTP 接口集成测试：在临时端口上走通完整业务流程。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copyright_patrol.api import build_server, create_app_service  # noqa: E402

UTC = timezone.utc


def iso(y, m, d, h=0, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=UTC).isoformat()


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = str(Path(self.tmp.name) / "store.json")
        self.service = create_app_service(self.store)
        self.server = build_server("127.0.0.1", 0, self.service, lambda: self.service.repo.save(self.store))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.drain(timeout=5)
        self.server.server_close()
        self.thread.join(timeout=2)

    def tearDown(self) -> None:
        self._stop_server()
        self.tmp.cleanup()

    def call(self, method: str, path: str, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())["data"]
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())["error"]

    def test_full_workflow_over_http(self) -> None:
        # 登记主体与素材
        self.assertEqual(self.call("POST", "/holders", {
            "holder_id": "H1", "name": "民乐版权组织",
        })[0], 201)
        self.assertEqual(self.call("POST", "/materials", {
            "material_id": "M2", "title": "公有领域录音", "kind": "audio",
            "holder_id": "H1", "license_free": True,
        })[0], 201)
        self.assertEqual(self.call("POST", "/materials", {
            "material_id": "M1", "title": "江南丝竹", "kind": "audio",
            "holder_id": "H1", "alternative_ids": ["M2"],
        })[0], 201)

        # 许可 9 月有效
        self.assertEqual(self.call("POST", "/licenses", {
            "license_id": "L1", "material_id": "M1",
            "scope": ["演出权"],
            "valid_from": iso(2026, 9, 1), "valid_until": iso(2026, 9, 30),
        })[0], 201)

        self.assertEqual(self.call("POST", "/plans", {
            "plan_id": "P1", "title": "音乐教案",
            "references": [{"material_id": "M1", "rights": ["演出权"]}],
        })[0], 201)

        # 九月场次可以发布
        self.assertEqual(self.call("POST", "/sessions", {
            "session_id": "S-OK", "title": "九月课", "plan_id": "P1",
            "start": iso(2026, 9, 10, 14), "end": iso(2026, 9, 10, 15),
        })[0], 201)
        self.assertEqual(self.call("POST", "/sessions/S-OK/publish")[0], 200)

        # 十月场次发布被拦截
        self.assertEqual(self.call("POST", "/sessions", {
            "session_id": "S-RISK", "title": "十月课", "plan_id": "P1",
            "start": iso(2026, 10, 10, 14), "end": iso(2026, 10, 10, 15),
        })[0], 201)
        status, error = self.call("POST", "/sessions/S-RISK/publish")
        self.assertEqual(status, 422)
        self.assertEqual(error["code"], "publication_blocked")
        self.assertEqual(error["violations"][0]["reason"], "license_expired")

        # 批量巡检生成风险案件，重跑幂等。
        # as_of 既是巡检窗口起点，也是回放知识截止点，必须晚于数据登记时刻。
        t1 = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        t2 = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        status, first = self.call("POST", "/patrols", {
            "batch_id": "B1", "as_of": t1, "horizon": iso(2026, 11, 1),
        })
        self.assertEqual(status, 201)
        self.assertEqual(len(first["opened"]), 1)
        status, replay = self.call("POST", "/patrols", {
            "batch_id": "B1", "as_of": t1, "horizon": iso(2026, 11, 1),
        })
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])

        # 批量替换为公有领域素材
        status, command = self.call("POST", "/replacements", {
            "mappings": [{"plan_id": "P1", "from_material": "M1", "to_material": "M2"}],
            "reason": "许可到期", "command_id": "RPL-1",
        })
        self.assertEqual(status, 201)

        # 影响查询：风险已清除
        status, impact = self.call("GET", "/replacements/RPL-1/impact")
        self.assertEqual(status, 200)
        self.assertEqual(impact["affected_arrangements"][0]["transition"], "risk_cleared")

        # 再次巡检：案件消解、列表为空，且发布放行
        status, second = self.call("POST", "/patrols", {
            "batch_id": "B2", "as_of": t2, "horizon": iso(2026, 11, 1),
        })
        self.assertEqual(status, 201)
        self.assertEqual(second["resolved"][0]["reason"], "material_replaced")
        status, cases = self.call("GET", "/cases")
        self.assertEqual(cases, [])
        self.assertEqual(self.call("POST", "/sessions/S-RISK/publish")[0], 200)

        # 版本链校验
        self.assertEqual(self.call("POST", "/chains/verify")[1]["ok"], True)

        # 快照已随每次写操作落盘：重启后数据与幂等键仍在
        self._stop_server()
        service2 = create_app_service(self.store)
        self.server = build_server("127.0.0.1", 0, service2, lambda: service2.repo.save(self.store))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        status, replayed = self.call("POST", "/patrols", {
            "batch_id": "B1", "as_of": iso(2026, 9, 1), "horizon": iso(2026, 11, 1),
        })
        self.assertTrue(replayed["idempotent_replay"])
        status, session = self.call("GET", "/sessions/S-RISK")
        self.assertEqual(session["status"], "published")


if __name__ == "__main__":
    unittest.main()
