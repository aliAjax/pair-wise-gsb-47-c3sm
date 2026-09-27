import unittest

from src.domain import Conflict, ValidationError
from src.rules import DomainRules


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


def awaiting_review_record(rules, **overrides):
    payload = rules.prepare_create(CREATE_DATA)
    payload.update({"decon_completed": True, "reserved_beds": 8, "reserved_ventilators": 2})
    payload.update(overrides)
    return {"id": 1, "reference": "DECON-1", "state": "awaiting_review", "payload": payload}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create_computes_slot_and_defaults(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["slot_end"], "2026-09-27T08:40")
        self.assertFalse(prepared["decon_completed"])
        self.assertEqual(prepared["reserved_beds"], 0)
        self.assertEqual(prepared["admitted_beds"], 0)

    def test_invalid_inputs(self):
        for key, value in [("contamination_level", "unknown"), ("triage", "blue"), ("slot_start", "2026/09/27 08:00")]:
            data = dict(CREATE_DATA, **{key: value})
            with self.assertRaises(ValidationError):
                self.rules.prepare_create(data)

    def test_required_cannot_exceed_available(self):
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(dict(CREATE_DATA, required_beds=30))
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(dict(CREATE_DATA, required_ventilators=10))

    def test_pool_slot_overlap(self):
        existing = [{"id": 1, "reference": "DECON-1", "state": "reserved", "payload": self.rules.prepare_create(CREATE_DATA)}]
        overlap = self.rules.prepare_create(dict(CREATE_DATA, slot_start="2026-09-27T08:30"))
        with self.assertRaises(Conflict):
            self.rules.check_pool_conflict(overlap, existing)
        # 紧邻但不重叠的时段可以登记
        adjacent = self.rules.prepare_create(dict(CREATE_DATA, slot_start="2026-09-27T08:40"))
        self.rules.check_pool_conflict(adjacent, existing)
        # 同时段不同池位可以登记
        other_pool = self.rules.prepare_create(dict(CREATE_DATA, pool_id="P2", slot_start="2026-09-27T08:30"))
        self.rules.check_pool_conflict(other_pool, existing)

    def test_pre_occupy_capacity_counts_reserved_and_admitted(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        reserving = dict(prepared, reserved_beds=20, reserved_ventilators=5)
        existing = [{"id": 1, "reference": "A", "state": "decontaminating", "payload": reserving}]
        with self.assertRaises(ValidationError):
            self.rules.check_pre_occupy_capacity(prepared, existing)
        admitted = dict(prepared, admitted_beds=20, admitted_ventilators=5)
        existing = [{"id": 1, "reference": "A", "state": "admitted", "payload": admitted}]
        with self.assertRaises(ValidationError):
            self.rules.check_pre_occupy_capacity(prepared, existing)
        # 已出院批次释放容量，可以预占
        closed = dict(prepared, admitted_beds=0, admitted_ventilators=0)
        existing = [{"id": 1, "reference": "A", "state": "closed", "payload": closed}]
        self.rules.check_pre_occupy_capacity(prepared, existing)

    def test_review_pass_converts_reservation_to_admission(self):
        record = awaiting_review_record(self.rules)
        state, payload, summary = self.rules.apply_action(record, "review", {"passed": True})
        self.assertEqual(state, "admitted")
        self.assertEqual(payload["admitted_beds"], 8)
        self.assertEqual(payload["admitted_ventilators"], 2)
        self.assertEqual(payload["reserved_beds"], 0)

    def test_review_failure_requires_gap_note_and_releases_hold(self):
        record = awaiting_review_record(self.rules)
        with self.assertRaises(ValidationError):
            self.rules.apply_action(record, "review", {"passed": False})
        state, payload, summary = self.rules.apply_action(record, "review", {"passed": False, "gap_note": "袖口残留超标，需重新洗消"})
        self.assertEqual(state, "pending_area")
        self.assertEqual(payload["pending_reason"], "decon_review_failed")
        self.assertEqual(payload["gap_note"], "袖口残留超标，需重新洗消")
        self.assertEqual(payload["reserved_beds"], 0)
        self.assertFalse(payload["decon_completed"])

    def test_review_blocked_without_decon(self):
        record = awaiting_review_record(self.rules, decon_completed=False)
        with self.assertRaises(Conflict):
            self.rules.apply_action(record, "review", {"passed": True})
