"""化学事故分区接收台：登记、预占、洗消、复核与收治的状态规则。

核心约束：
- 每批登记染毒等级、人数、床位/呼吸机需求、洗消池与预计时长；
- 同一时段一个池位只接一批，时段重叠即冲突；
- 红色伤员也必须先洗消，任何分诊等级都不能跳过洗消直接收治；
- 复核通过前床位/呼吸机只是预占，复核通过后才转为正式收治；
- 池位故障或复核未过的批次留在待安排区，并必须写清缺口。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, ValidationError, choice, integer, optional_text, text


INITIAL_STATE = "registered"

TRIAGE_LEVELS = ["red", "yellow", "green", "black"]
CONTAMINATION_LEVELS = ["heavy", "moderate", "light"]

# 这些状态占用洗消池时段
POOL_HOLDING_STATES = {"registered", "reserved", "decontaminating", "awaiting_review"}
# 这些状态持有床位/呼吸机预占
RESERVING_STATES = {"reserved", "decontaminating", "awaiting_review"}
ADMITTED_STATE = "admitted"

CREATE_ROLES = {"reception_officer", "incident_commander"}
ACTION_ROLES = {
    "pre_occupy": {"reception_officer"},
    "start_decon": {"decon_officer"},
    "finish_decon": {"decon_officer"},
    "review": {"review_officer"},
    "report_pool_failure": {"decon_officer", "reception_officer"},
    "reschedule": {"reception_officer"},
    "discharge": {"hospital_liaison"},
    "cancel": {"incident_commander"},
}
TRANSITIONS = {
    "pre_occupy": {"registered": ["reserved"]},
    "start_decon": {"reserved": ["decontaminating"]},
    "finish_decon": {"decontaminating": ["awaiting_review"]},
    "review": {"awaiting_review": ["admitted", "pending_area"]},
    "report_pool_failure": {"registered": ["pending_area"], "reserved": ["pending_area"], "decontaminating": ["pending_area"]},
    "reschedule": {"pending_area": ["registered"]},
    "discharge": {"admitted": ["closed"]},
    "cancel": {"registered": ["cancelled"], "reserved": ["cancelled"], "pending_area": ["cancelled"]},
}

SLOT_FORMAT = "%Y-%m-%dT%H:%M"


def parse_slot(value: str) -> datetime:
    try:
        return datetime.strptime(value, SLOT_FORMAT)
    except ValueError as exc:
        raise ValidationError("slot_start必须是YYYY-MM-DDTHH:MM格式") from exc


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "hospital")
        text(p, "pool_id")
        choice(p, "triage", TRIAGE_LEVELS)
        choice(p, "contamination_level", CONTAMINATION_LEVELS)
        integer(p, "casualty_count", 1)
        integer(p, "required_beds", 0)
        integer(p, "required_ventilators", 0)
        integer(p, "available_beds", 0)
        integer(p, "available_ventilators", 0)
        integer(p, "estimated_minutes", 1)
        parse_slot(text(p, "slot_start"))
        if p["required_beds"] > p["available_beds"]:
            raise ValidationError("床位需求超过医院可用床位")
        if p["required_ventilators"] > p["available_ventilators"]:
            raise ValidationError("呼吸机需求超过医院可用呼吸机")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        start = parse_slot(p["slot_start"])
        p["slot_start"] = start.strftime(SLOT_FORMAT)
        p["slot_end"] = (start + timedelta(minutes=int(p["estimated_minutes"]))).strftime(SLOT_FORMAT)
        p["decon_completed"] = False
        p["reserved_beds"] = 0
        p["reserved_ventilators"] = 0
        p["admitted_beds"] = 0
        p["admitted_ventilators"] = 0
        p["pending_reason"] = ""
        p["gap_note"] = ""
        return p

    @staticmethod
    def _slots_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
        return bool(a_start) and bool(b_start) and a_start < b_end and b_start < a_end

    def check_pool_conflict(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]], exclude_id: int = None) -> None:
        for item in existing:
            if exclude_id is not None and item["id"] == exclude_id:
                continue
            if item["state"] not in POOL_HOLDING_STATES:
                continue
            other = item["payload"]
            if other.get("pool_id") != payload.get("pool_id"):
                continue
            if self._slots_overlap(payload.get("slot_start", ""), payload.get("slot_end", ""), other.get("slot_start", ""), other.get("slot_end", "")):
                raise Conflict(
                    "洗消池%s在%s至%s已被批次%s占用" % (other.get("pool_id"), other.get("slot_start"), other.get("slot_end"), item["reference"])
                )

    def check_pre_occupy_capacity(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]], exclude_id: int = None) -> None:
        used_beds = 0
        used_ventilators = 0
        for item in existing:
            if exclude_id is not None and item["id"] == exclude_id:
                continue
            other = item["payload"]
            if other.get("hospital") != payload.get("hospital"):
                continue
            if item["state"] in RESERVING_STATES:
                used_beds += int(other.get("reserved_beds", 0))
                used_ventilators += int(other.get("reserved_ventilators", 0))
            elif item["state"] == ADMITTED_STATE:
                used_beds += int(other.get("admitted_beds", 0))
                used_ventilators += int(other.get("admitted_ventilators", 0))
        free_beds = int(payload["available_beds"]) - used_beds
        free_ventilators = int(payload["available_ventilators"]) - used_ventilators
        if int(payload["required_beds"]) > free_beds:
            raise ValidationError("预占失败：床位缺口%s张" % (int(payload["required_beds"]) - free_beds))
        if int(payload["required_ventilators"]) > free_ventilators:
            raise ValidationError("预占失败：呼吸机缺口%s台" % (int(payload["required_ventilators"]) - free_ventilators))

    def require_transition(self, record: Dict[str, Any], action: str) -> List[str]:
        targets = TRANSITIONS.get(action, {}).get(record["state"])
        if not targets:
            raise Conflict("当前状态不允许执行%s" % action)
        return list(targets)

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        targets = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        new_state = ""
        summary = ""
        if action == "pre_occupy":
            new_state = "reserved"
            changes["reserved_beds"] = int(p["required_beds"])
            changes["reserved_ventilators"] = int(p["required_ventilators"])
            summary = "已预占床位%s张、呼吸机%s台，复核通过前不正式收治" % (changes["reserved_beds"], changes["reserved_ventilators"])
        elif action == "start_decon":
            new_state = "decontaminating"
            summary = "批次进入洗消池%s开始洗消" % p["pool_id"]
        elif action == "finish_decon":
            new_state = "awaiting_review"
            changes["decon_completed"] = True
            changes["decon_note"] = optional_text(data, "decon_note")
            summary = "洗消完成，等待去污复核"
        elif action == "review":
            passed = data.get("passed")
            if not isinstance(passed, bool):
                raise ValidationError("passed必须是布尔值")
            if not p.get("decon_completed"):
                # 红色伤员也必须先洗消，任何分诊等级都不能跳过
                raise Conflict("未完成洗消，不能复核收治")
            changes["review_note"] = optional_text(data, "review_note")
            if passed:
                new_state = "admitted"
                changes["admitted_beds"] = int(p.get("reserved_beds", 0))
                changes["admitted_ventilators"] = int(p.get("reserved_ventilators", 0))
                changes["reserved_beds"] = 0
                changes["reserved_ventilators"] = 0
                summary = "去污复核通过，预占床位%s张、呼吸机%s台转为正式收治" % (changes["admitted_beds"], changes["admitted_ventilators"])
            else:
                new_state = "pending_area"
                gap_note = text(data, "gap_note")
                changes.update(self._release_to_pending("decon_review_failed", gap_note))
                summary = self._pending_summary("去污复核未通过，批次留在待安排区", p)
        elif action == "report_pool_failure":
            new_state = "pending_area"
            gap_note = text(data, "gap_note")
            changes.update(self._release_to_pending("pool_failure", gap_note))
            summary = self._pending_summary("洗消池%s故障，批次留在待安排区" % p.get("pool_id", ""), p)
        elif action == "reschedule":
            new_state = "registered"
            start = parse_slot(text(data, "slot_start"))
            minutes = integer(data, "estimated_minutes", 1)
            pool_id = optional_text(data, "pool_id") or p["pool_id"]
            changes["pool_id"] = pool_id
            changes["slot_start"] = start.strftime(SLOT_FORMAT)
            changes["slot_end"] = (start + timedelta(minutes=minutes)).strftime(SLOT_FORMAT)
            changes["estimated_minutes"] = minutes
            changes["pending_reason"] = ""
            changes["gap_note"] = ""
            changes["decon_completed"] = False
            summary = "已重新安排洗消池%s，时段%s起%s分钟" % (pool_id, changes["slot_start"], minutes)
        elif action == "discharge":
            new_state = "closed"
            changes["outcome"] = choice(data, "outcome", ["treated", "transferred", "expired"])
            changes["admitted_beds"] = 0
            changes["admitted_ventilators"] = 0
            summary = "伤员离院，正式收治的床位与呼吸机释放"
        elif action == "cancel":
            new_state = "cancelled"
            changes["cancel_reason"] = text(data, "cancel_reason")
            changes["reserved_beds"] = 0
            changes["reserved_ventilators"] = 0
            summary = "批次取消，预占资源释放"
        if new_state not in targets:
            raise Conflict("当前状态不允许执行%s" % action)
        p.update(changes)
        return new_state, p, summary

    @staticmethod
    def _release_to_pending(reason: str, gap_note: str) -> Dict[str, Any]:
        return {
            "pending_reason": reason,
            "gap_note": gap_note,
            "reserved_beds": 0,
            "reserved_ventilators": 0,
            "decon_completed": False,
        }

    @staticmethod
    def _pending_summary(base: str, payload: Dict[str, Any]) -> str:
        beds = int(payload.get("reserved_beds", 0))
        ventilators = int(payload.get("reserved_ventilators", 0))
        if beds or ventilators:
            return "%s，释放预占床位%s张、呼吸机%s台" % (base, beds, ventilators)
        return base
