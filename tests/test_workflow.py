import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


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


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_full_reception_flow_and_audit(self):
        record = self.service.create(Actor("desk", "reception_officer"), "DECON-0001", CREATE_DATA)
        self.assertEqual(record["state"], "registered")
        self.assertEqual(record["payload"]["slot_end"], "2026-09-27T08:40")
        record = walk(self.service, record)
        self.assertEqual(record["state"], "closed")
        timeline = self.service.timeline(Actor("desk", "reception_officer"), record["id"])
        self.assertEqual([event["action"] for event in timeline], ["created"] + [step[0] for step in FLOW])

    def test_reserved_first_then_formal_admission(self):
        record = self.service.create(Actor("desk", "reception_officer"), "DECON-0002", CREATE_DATA)
        record = self.service.act(Actor("desk", "reception_officer"), record["id"], record["version"], "pre_occupy", {})
        self.assertEqual(record["payload"]["reserved_beds"], 8)
        self.assertEqual(record["payload"]["reserved_ventilators"], 2)
        self.assertEqual(record["payload"]["admitted_beds"], 0)
        # 洗消复核前不能正式收治
        with self.assertRaises(Conflict):
            self.service.act(Actor("reviewer", "review_officer"), record["id"], record["version"], "review", {"passed": True})
        record = walk(self.service, record, FLOW[1:4])
        self.assertEqual(record["state"], "admitted")
        self.assertEqual(record["payload"]["admitted_beds"], 8)
        self.assertEqual(record["payload"]["admitted_ventilators"], 2)
        self.assertEqual(record["payload"]["reserved_beds"], 0)

    def test_red_casualties_must_decon_first(self):
        red = dict(CREATE_DATA, triage="red", contamination_level="heavy")
        record = self.service.create(Actor("desk", "reception_officer"), "DECON-RED", red)
        # 红色伤员同样不能跳过洗消直接收治
        with self.assertRaises(Conflict):
            self.service.act(Actor("reviewer", "review_officer"), record["id"], record["version"], "review", {"passed": True})
        record = walk(self.service, record)
        self.assertEqual(record["state"], "closed")

    def test_batch_file_available_next_day(self):
        record = self.service.create(Actor("desk", "reception_officer"), "DECON-0003", CREATE_DATA)
        walk(self.service, record)
        # 模拟第二天重启服务后按批次号查询预占、收治经过
        reopened = build_service(self.db_path)
        dossier = reopened.batch_file(Actor("desk", "reception_officer"), "DECON-0003")
        self.assertEqual(dossier["record"]["state"], "closed")
        actions = [event["action"] for event in dossier["timeline"]]
        self.assertEqual(actions, ["created", "pre_occupy", "start_decon", "finish_decon", "review", "discharge"])

    def test_timeline_date_filter(self):
        record = self.service.create(Actor("desk", "reception_officer"), "DECON-0004", CREATE_DATA)
        actor = Actor("desk", "reception_officer")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertEqual(len(self.service.timeline(actor, record["id"], date=today)), 1)
        self.assertEqual(self.service.timeline(actor, record["id"], date="1999-01-01"), [])
