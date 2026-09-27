import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_chemical_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.http_api import create_server
from src.repository import Repository
from src.service import Service
from src.rules import DomainRules
from src.audit import AuditRecorder


def slot(hours_ahead: int = 1, minutes: int = 30) -> dict:
    start = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return {"scheduled_at": start.isoformat(), "estimated_minutes": minutes}


BATCH_DATA = {
    "station": "MAIN", "contamination_level": "heavy", "triage": "red",
    "casualty_count": 3, "required_beds": 3, "required_ventilators": 2,
}


class ChemicalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "chem.db")
        self.service = build_chemical_service(self.db_path)
        self.commander = Actor("cmd", "incident_commander")
        self.coordinator = Actor("coord", "transport_coordinator")
        self.decon = Actor("decon", "decon_officer")
        self.liaison = Actor("liaison", "hospital_liaison")

    def tearDown(self):
        self.temp.cleanup()

    def _pool(self, code="P1", actor=None):
        return self.service.register_pool(
            actor or self.commander,
            {"code": code, "station": "MAIN", "name": code + "号洗消池"},
        )

    def _batch(self, reference="CHEM-001", data=None, actor=None):
        return self.service.register_batch(actor or self.coordinator, reference, data or BATCH_DATA)

    def _arrange(self, batch, pool, data=None, actor=None):
        payload = {"pool_id": pool["id"], **slot(), **(data or {})}
        return self.service.act(actor or self.commander, batch["id"], batch["version"], "arrange", payload)

    def test_registration_only_enters_waiting_zone(self):
        batch = self._batch()
        self.assertEqual(batch["state"], "pending_arrangement")
        self.assertEqual(batch["payload"]["zone"], "待安排区")
        self.assertIsNone(batch["payload"]["assignment"])
        self.assertEqual(batch["payload"]["preoccupied"], {"beds": 0, "ventilators": 0})
        self.assertEqual(batch["payload"]["occupied"], {"beds": 0, "ventilators": 0})

    def test_red_casualties_must_decontaminate_before_admission(self):
        pool = self._pool()
        batch = self._batch()
        # 红色伤员不能绕过洗消直接复核收治
        with self.assertRaises(Conflict):
            self.service.act(self.decon, batch["id"], batch["version"], "review",
                             {"decontamination_passed": True})
        batch = self._arrange(batch, pool)
        self.assertEqual(batch["state"], "reserved")
        self.assertEqual(batch["payload"]["preoccupied"], {"beds": 3, "ventilators": 2})
        self.assertEqual(batch["payload"]["occupied"], {"beds": 0, "ventilators": 0})
        # 预占状态仍不能复核
        with self.assertRaises(Conflict):
            self.service.act(self.liaison, batch["id"], batch["version"], "review",
                             {"decontamination_passed": True})
        batch = self.service.act(self.decon, batch["id"], batch["version"], "start_decontamination", {})
        self.assertEqual(batch["state"], "decontaminating")
        batch = self.service.act(self.liaison, batch["id"], batch["version"], "review",
                                 {"decontamination_passed": True, "review_note": "去污合格"})
        self.assertEqual(batch["state"], "admitted")
        self.assertEqual(batch["payload"]["occupied"], {"beds": 3, "ventilators": 2})
        self.assertEqual(batch["payload"]["preoccupied"], {"beds": 0, "ventilators": 0})
        self.assertIsNone(batch["payload"]["assignment"])
        timeline = self.service.batch_timeline(self.commander, batch["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions, ["registered", "arrange", "start_decontamination", "admitted"])

    def test_same_pool_same_window_rejects_second_batch(self):
        pool = self._pool()
        first = self._batch("CHEM-001")
        window = slot()
        first = self.service.act(self.commander, first["id"], first["version"], "arrange",
                                 {"pool_id": pool["id"], **window})
        self.assertEqual(first["state"], "reserved")

        second = self._batch("CHEM-002", dict(BATCH_DATA, required_beds=1, required_ventilators=0))
        second = self.service.act(self.commander, second["id"], second["version"], "arrange",
                                  {"pool_id": pool["id"], **window})
        self.assertEqual(second["state"], "pending_arrangement")
        gap = second["payload"]["gaps"][-1]
        self.assertEqual(gap["reason"], "pool_conflict")
        self.assertEqual(gap["pool_code"], "P1")
        self.assertIsNone(second["payload"]["assignment"])

        # 错开时段（首尾相接不重叠）可以安排
        second = self.service.get_batch(self.commander, second["id"])
        start = datetime.fromisoformat(window["scheduled_at"]) + timedelta(minutes=window["estimated_minutes"])
        second = self.service.act(self.commander, second["id"], second["version"], "arrange",
                                  {"pool_id": pool["id"], "scheduled_at": start.isoformat(),
                                   "estimated_minutes": 20})
        self.assertEqual(second["state"], "reserved")

    def test_capacity_shortage_writes_gap(self):
        self.service.configure_station(self.commander,
                                       {"code": "MAIN", "name": "主分区", "beds_total": 4, "ventilators_total": 1})
        pool = self._pool()
        batch = self._batch("CHEM-010", dict(BATCH_DATA, required_beds=8, required_ventilators=3))
        batch = self._arrange(batch, pool)
        self.assertEqual(batch["state"], "pending_arrangement")
        gap = batch["payload"]["gaps"][-1]
        self.assertEqual(gap["reason"], "resource_shortage")
        self.assertEqual(gap["missing_beds"], 4)
        self.assertEqual(gap["missing_ventilators"], 2)

    def test_pool_fault_returns_batches_with_gap(self):
        pool = self._pool()
        reserved = self._arrange(self._batch("CHEM-020"), pool)
        washing = self._arrange(self._batch("CHEM-021", dict(BATCH_DATA, required_beds=1, required_ventilators=0)), pool,
                                {"scheduled_at": slot(2)["scheduled_at"]})
        washing = self.service.act(self.decon, washing["id"], washing["version"], "start_decontamination", {})
        done = self._arrange(self._batch("CHEM-022", dict(BATCH_DATA, required_beds=1, required_ventilators=0)), pool,
                             {"scheduled_at": slot(3)["scheduled_at"]})
        done = self.service.act(self.decon, done["id"], done["version"], "start_decontamination", {})
        done = self.service.act(self.liaison, done["id"], done["version"], "review",
                                {"decontamination_passed": True})

        result = self.service.report_pool_fault(self.decon, pool["id"], {"note": "循环泵损坏"})
        self.assertEqual(set(result["returned_batches"]), {reserved["id"], washing["id"]})
        for batch_id in result["returned_batches"]:
            record = self.service.get_batch(self.commander, batch_id)
            self.assertEqual(record["state"], "pending_arrangement")
            self.assertIsNone(record["payload"]["assignment"])
            self.assertEqual(record["payload"]["preoccupied"], {"beds": 0, "ventilators": 0})
            self.assertEqual(record["payload"]["gaps"][-1]["reason"], "pool_fault")
            self.assertIn("循环泵损坏", record["payload"]["gaps"][-1]["detail"])
        # 已收治批次不受影响
        self.assertEqual(self.service.get_batch(self.commander, done["id"])["state"], "admitted")
        # 故障池不能再安排
        again = self._batch("CHEM-023", dict(BATCH_DATA, required_beds=1, required_ventilators=0))
        again = self._arrange(again, pool)
        self.assertEqual(again["state"], "pending_arrangement")
        self.assertEqual(again["payload"]["gaps"][-1]["reason"], "pool_unavailable")
        # 修复后可重新安排
        self.service.repair_pool(self.decon, pool["id"], {"note": "已换泵"})
        again = self.service.get_batch(self.commander, again["id"])
        again = self._arrange(again, pool, {"scheduled_at": slot(5)["scheduled_at"]})
        self.assertEqual(again["state"], "reserved")

    def test_review_failure_returns_and_can_rearrange(self):
        pool = self._pool()
        batch = self._arrange(self._batch("CHEM-030"), pool)
        batch = self.service.act(self.decon, batch["id"], batch["version"], "start_decontamination", {})
        batch = self.service.act(self.liaison, batch["id"], batch["version"], "review",
                                 {"decontamination_passed": False, "review_note": "仍有残留"})
        self.assertEqual(batch["state"], "pending_arrangement")
        gap = batch["payload"]["gaps"][-1]
        self.assertEqual(gap["reason"], "review_failed")
        self.assertIn("仍有残留", gap["detail"])
        self.assertEqual(batch["payload"]["preoccupied"], {"beds": 0, "ventilators": 0})
        # 退回后可重新走一遍洗消复核
        batch = self._arrange(self.service.get_batch(self.commander, batch["id"]), pool,
                              {"scheduled_at": slot(4)["scheduled_at"]})
        batch = self.service.act(self.decon, batch["id"], batch["version"], "start_decontamination", {})
        batch = self.service.act(self.liaison, batch["id"], batch["version"], "review",
                                 {"decontamination_passed": True})
        self.assertEqual(batch["state"], "admitted")
        timeline = self.service.batch_timeline(self.commander, batch["id"])
        self.assertIn("returned", [event["action"] for event in timeline])

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_batch(Actor("x", "decon_officer"), "CHEM-040", BATCH_DATA)
        pool = self._pool()
        batch = self._batch("CHEM-040")
        with self.assertRaises(PermissionDenied):
            self.service.act(self.decon, batch["id"], batch["version"], "arrange", {"pool_id": pool["id"], **slot()})
        batch = self._arrange(batch, pool)
        with self.assertRaises(PermissionDenied):
            self.service.act(self.coordinator, batch["id"], batch["version"], "start_decontamination", {})

    def test_stale_version_rejected(self):
        pool = self._pool()
        batch = self._batch("CHEM-050")
        self._arrange(batch, pool)
        with self.assertRaises(Conflict):
            self._arrange(batch, pool, {"scheduled_at": slot(6)["scheduled_at"]})

    def test_next_day_query_and_persistence(self):
        pool = self._pool()
        batch = self._arrange(self._batch("CHEM-060"), pool)
        batch = self.service.act(self.decon, batch["id"], batch["version"], "start_decontamination", {})
        batch = self.service.act(self.liaison, batch["id"], batch["version"], "review",
                                 {"decontamination_passed": True})
        batch_id = batch["id"]
        today = datetime.now(timezone.utc).date().isoformat()

        # 第二天（新进程）仍可按日期与批次查到完整经过
        reopened = build_chemical_service(self.db_path)
        found = reopened.list_batches(self.commander, created_date=today)
        self.assertEqual([item["id"] for item in found], [batch_id])
        timeline = reopened.batch_timeline(self.liaison, batch_id)
        self.assertEqual([event["action"] for event in timeline],
                         ["registered", "arrange", "start_decontamination", "admitted"])
        detail = reopened.get_batch(self.commander, batch_id)
        self.assertEqual(detail["state"], "admitted")


class ChemicalHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp.name) / "http.db")
        service = Service(Repository(db_path), DomainRules(), AuditRecorder(Repository(db_path)))
        self.chemical = build_chemical_service(db_path)
        self.server = create_server("127.0.0.1", 0, service, Path("/workspace/static"), self.chemical)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _request(self, method, path, body=None, role="incident_commander"):
        data = json.dumps(body or {}).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-User-Id": "http-user", "X-Role": role},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        status, pool = self._request("POST", "/api/chem/pools",
                                     {"data": {"code": "P1", "station": "MAIN", "name": "1号池"}})
        self.assertEqual(status, 201)
        status, batch = self._request(
            "POST", "/api/chem/batches",
            {"reference": "CHEM-HTTP-1", "data": BATCH_DATA}, role="transport_coordinator")
        self.assertEqual(201, status)
        self.assertEqual(batch["state"], "pending_arrangement")

        window = slot()
        status, batch = self._request(
            "POST", "/api/chem/batches/%s/actions/arrange" % batch["id"],
            {"expected_version": batch["version"],
             "data": {"pool_id": pool["id"], **window}})
        self.assertEqual(status, 200)
        self.assertEqual(batch["state"], "reserved")
        status, batch = self._request(
            "POST", "/api/chem/batches/%s/actions/start_decontamination" % batch["id"],
            {"expected_version": batch["version"], "data": {}}, role="decon_officer")
        self.assertEqual(batch["state"], "decontaminating")
        status, batch = self._request(
            "POST", "/api/chem/batches/%s/actions/review" % batch["id"],
            {"expected_version": batch["version"],
             "data": {"decontamination_passed": True, "review_note": "合格"}},
            role="hospital_liaison")
        self.assertEqual(batch["state"], "admitted")

        status, timeline = self._request("GET", "/api/chem/batches/%s/audit" % batch["id"])
        self.assertEqual(len(timeline["items"]), 4)
        status, pools = self._request("GET", "/api/chem/pools")
        self.assertEqual(pools["items"][0]["code"], "P1")


if __name__ == "__main__":
    unittest.main()
