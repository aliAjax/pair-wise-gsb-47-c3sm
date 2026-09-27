import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {
    "hospital": "North Hospital",
    "pool_id": "P1",
    "triage": "yellow",
    "contamination_level": "moderate",
    "casualty_count": 6,
    "required_beds": 8,
    "required_ventilators": 2,
    "available_beds": 24,
    "available_ventilators": 6,
    "slot_start": "2026-09-27T08:00",
    "estimated_minutes": 40,
}
FLOW = [
    ("pre_occupy", "reception_officer", {}, "reserved"),
    ("start_decon", "decon_officer", {}, "decontaminating"),
    ("finish_decon", "decon_officer", {"decon_note": "两遍冲洗"}, "awaiting_review"),
    ("review", "review_officer", {"passed": True, "review_note": "检测达标"}, "admitted"),
    ("discharge", "hospital_liaison", {"outcome": "treated"}, "closed"),
]


def walk(service, record, steps=FLOW):
    for action, role, data, expected_state in steps:
        record = service.act(Actor("operator", role), record["id"], record["version"], action, data)
        assert record["state"] == expected_state, "执行%s后状态为%s，预期%s" % (action, record["state"], expected_state)
    return record


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, reference, **overrides):
        return self.service.create(Actor("desk", "reception_officer"), reference, dict(CREATE_DATA, **overrides))

    def act(self, record, role, action, data):
        return self.service.act(Actor("operator", role), record["id"], record["version"], action, data)

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "DECON-1", CREATE_DATA)
        self.create("DECON-1")
        with self.assertRaises(Conflict):
            self.create("DECON-1")

    def test_stale_version_is_rejected(self):
        record = self.create("DECON-1")
        record = self.act(record, "reception_officer", "pre_occupy", {})
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", "decon_officer"), record["id"], record["version"] - 1, "start_decon", {})

    def test_pool_slot_conflict_and_release_after_close(self):
        record = self.create("DECON-A", slot_start="2026-09-27T08:00", estimated_minutes=40)
        with self.assertRaises(Conflict):
            self.create("DECON-B", slot_start="2026-09-27T08:30", estimated_minutes=30)
        # A 出院后池位释放，同时段可以登记新批次
        walk(self.service, record)
        self.create("DECON-B", slot_start="2026-09-27T08:30", estimated_minutes=30)

    def test_pre_occupy_capacity_gap(self):
        record = self.create("DECON-A", required_beds=20)
        self.act(record, "reception_officer", "pre_occupy", {})
        other = self.create("DECON-B", slot_start="2026-09-27T09:00")
        with self.assertRaises(ValidationError) as ctx:
            self.act(other, "reception_officer", "pre_occupy", {})
        self.assertIn("床位缺口", str(ctx.exception))

    def test_pool_failure_sends_batch_to_pending_area_with_gap(self):
        record = self.create("DECON-A")
        record = self.act(record, "reception_officer", "pre_occupy", {})
        record = self.act(record, "decon_officer", "start_decon", {})
        # 必须写清缺口才能留在待安排区
        with self.assertRaises(ValidationError):
            self.act(record, "decon_officer", "report_pool_failure", {})
        record = self.act(record, "decon_officer", "report_pool_failure", {"gap_note": "P1水泵故障，需启用备用池P2"})
        self.assertEqual(record["state"], "pending_area")
        self.assertEqual(record["payload"]["pending_reason"], "pool_failure")
        self.assertEqual(record["payload"]["gap_note"], "P1水泵故障，需启用备用池P2")
        self.assertEqual(record["payload"]["reserved_beds"], 0)
        # 重新排池后走完整流程仍可收治
        record = self.act(record, "reception_officer", "reschedule", {"pool_id": "P2", "slot_start": "2026-09-27T10:00", "estimated_minutes": 30})
        self.assertEqual(record["state"], "registered")
        record = walk(self.service, record)
        self.assertEqual(record["state"], "closed")
        actions = [event["action"] for event in self.service.timeline(Actor("desk", "reception_officer"), record["id"])]
        self.assertIn("report_pool_failure", actions)
        self.assertIn("reschedule", actions)

    def test_review_failure_keeps_batch_pending_with_gap(self):
        record = self.create("DECON-A")
        record = walk(self.service, record, FLOW[:3])
        record = self.act(record, "review_officer", "review", {"passed": False, "gap_note": "去污复核未过：袖口残留超标，需重新洗消"})
        self.assertEqual(record["state"], "pending_area")
        self.assertEqual(record["payload"]["pending_reason"], "decon_review_failed")
        self.assertEqual(record["payload"]["gap_note"], "去污复核未过：袖口残留超标，需重新洗消")
        self.assertFalse(record["payload"]["decon_completed"])
        # 重新安排后再次洗消复核，通过后正式收治
        record = self.act(record, "reception_officer", "reschedule", {"slot_start": "2026-09-27T11:00", "estimated_minutes": 45})
        record = walk(self.service, record, FLOW[:4])
        self.assertEqual(record["state"], "admitted")
        dossier = self.service.batch_file(Actor("desk", "reception_officer"), "DECON-A")
        actions = [event["action"] for event in dossier["timeline"]]
        self.assertEqual(actions.count("review"), 2)
        self.assertIn("reschedule", actions)
