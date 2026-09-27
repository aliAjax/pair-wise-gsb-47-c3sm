"""化学事故分区接收台领域规则。

分区与状态：

- pending_arrangement 待安排区：登记后驻留，不占床位、呼吸机、池位；安排失败、池位故障、
  复核未过也退回这里，并在 gaps 中写清缺口。
- reserved 洗消等候区：池位时段锁定，床位与呼吸机仅“预占”，不得正式收治。
- decontaminating 洗消区：正在洗消，红色伤员同样必须进入，无绕过路径。
- admitted 收治区：去污复核通过后才转入，预占转正式占用，池位释放。
- cancelled 已取消。
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, ValidationError, choice, integer, optional_text, text


CONTAMINATION_LEVELS = ["heavy", "moderate", "light"]
TRIAGE_LEVELS = ["red", "yellow", "green", "black"]

STATE_PENDING = "pending_arrangement"
STATE_RESERVED = "reserved"
STATE_DECONTAMINATING = "decontaminating"
STATE_ADMITTED = "admitted"
STATE_CANCELLED = "cancelled"

INITIAL_STATE = STATE_PENDING

ZONE_LABELS = {
    STATE_PENDING: "待安排区",
    STATE_RESERVED: "洗消等候区（资源预占）",
    STATE_DECONTAMINATING: "洗消区",
    STATE_ADMITTED: "收治区",
    STATE_CANCELLED: "已取消",
}

POOL_AVAILABLE = "available"
POOL_FAULTY = "faulty"

# 床位/呼吸机被占用（含预占）的状态；池位只在洗消前后两段被占。
RESOURCE_HOLD_STATES = (STATE_RESERVED, STATE_DECONTAMINATING, STATE_ADMITTED)
POOL_HOLD_STATES = (STATE_RESERVED, STATE_DECONTAMINATING)

ACTION_FROM_STATES = {
    "arrange": {STATE_PENDING},
    "start_decontamination": {STATE_RESERVED},
    "review": {STATE_DECONTAMINATING},
    "cancel": {STATE_PENDING, STATE_RESERVED},
}

REGISTER_ROLES = {"incident_commander", "transport_coordinator"}
ARRANGE_ROLES = {"incident_commander"}
DECONTAMINATION_ROLES = {"decon_officer"}
REVIEW_ROLES = {"decon_officer", "hospital_liaison"}
CANCEL_ROLES = {"incident_commander"}
STATION_ROLES = {"incident_commander"}
POOL_ROLES = {"incident_commander"}

_ACTION_ROLES = {
    "arrange": ARRANGE_ROLES,
    "start_decontamination": DECONTAMINATION_ROLES,
    "review": REVIEW_ROLES,
    "cancel": CANCEL_ROLES,
}


def parse_window(scheduled_at: str, estimated_minutes: int) -> Tuple[datetime, datetime]:
    """把 ISO8601 开始时间解析为 UTC 感知时间，并算出结束时间。"""
    raw = text({"scheduled_at": scheduled_at}, "scheduled_at")
    try:
        start = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("scheduled_at必须是ISO8601时间") from exc
    minutes = integer({"estimated_minutes": estimated_minutes}, "estimated_minutes", 1)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    return start, start + timedelta(minutes=minutes)


def _parse_stored(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ChemicalRules:
    INITIAL_STATE = INITIAL_STATE
    ZONE_LABELS = ZONE_LABELS

    def role_can(self, role: str, action: str) -> bool:
        return role == "admin" or role in _ACTION_ROLES.get(action, set())

    @staticmethod
    def known_roles() -> set:
        roles = set(REGISTER_ROLES) | set(STATION_ROLES) | set(POOL_ROLES)
        for items in _ACTION_ROLES.values():
            roles |= set(items)
        return roles

    def validate_station(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        code = text(p, "code")
        name = optional_text(p, "name", code)
        beds = integer(p, "beds_total", 0)
        ventilators = integer(p, "ventilators_total", 0)
        return {"code": code, "name": name, "beds_total": beds, "ventilators_total": ventilators}

    def validate_pool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        code = text(p, "code")
        station = optional_text(p, "station", "MAIN")
        name = optional_text(p, "name", code)
        return {"code": code, "station": station, "name": name}

    def validate_registration(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        station = optional_text(p, "station", "MAIN")
        contamination = choice(p, "contamination_level", CONTAMINATION_LEVELS)
        triage = choice(p, "triage", TRIAGE_LEVELS)
        count = integer(p, "casualty_count", 1)
        beds = integer(p, "required_beds", 0)
        ventilators = integer(p, "required_ventilators", 0)
        return {
            "station": station,
            "contamination_level": contamination,
            "triage": triage,
            "casualty_count": count,
            "required_beds": beds,
            "required_ventilators": ventilators,
        }

    def initial_payload(self, cleaned: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **cleaned,
            "zone": ZONE_LABELS[STATE_PENDING],
            "assignment": None,
            "preoccupied": {"beds": 0, "ventilators": 0},
            "occupied": {"beds": 0, "ventilators": 0},
            "decontamination": None,
            "decon_attempts": [],
            "admission": None,
            "gaps": [],
        }

    def require_state(self, record: Dict[str, Any], action: str) -> None:
        if record["state"] not in ACTION_FROM_STATES.get(action, set()):
            raise Conflict("当前状态不允许执行%s" % action)

    def evaluate_arrangement(
        self,
        record: Dict[str, Any],
        pool: Dict[str, Any],
        station: Dict[str, Any],
        active_batches: List[Dict[str, Any]],
        start: datetime,
        end: datetime,
        now: str,
    ) -> List[Dict[str, Any]]:
        """返回缺口列表；为空表示池位与资源都满足，可以预占。"""
        payload = record["payload"]
        window = {"scheduled_at": start.isoformat(), "estimated_minutes": int((end - start).total_seconds() // 60)}
        gaps: List[Dict[str, Any]] = []

        if pool["status"] != POOL_AVAILABLE:
            note = str(pool.get("status_note") or "")
            detail = "池位%s故障不可用" % pool["code"]
            if note:
                detail += "：%s" % note
            gaps.append({
                "at": now, "reason": "pool_unavailable", "pool_code": pool["code"],
                "detail": detail, "missing_beds": 0, "missing_ventilators": 0, **window,
            })
        else:
            for other in active_batches:
                if other["id"] == record["id"] or other["state"] not in POOL_HOLD_STATES:
                    continue
                assignment = other["payload"].get("assignment") or {}
                if assignment.get("pool_id") != pool["id"]:
                    continue
                other_start = _parse_stored(assignment["scheduled_at"])
                other_end = _parse_stored(assignment["ends_at"])
                if start < other_end and end > other_start:
                    gaps.append({
                        "at": now, "reason": "pool_conflict", "pool_code": pool["code"],
                        "detail": "池位%s在%s-%s已安排批次%s，同一时段只接一批" % (
                            pool["code"], assignment["scheduled_at"], assignment["ends_at"], other["reference"]),
                        "missing_beds": 0, "missing_ventilators": 0, **window,
                    })
                    break

        used_beds = 0
        used_ventilators = 0
        for other in active_batches:
            if other["id"] == record["id"] or other["state"] not in RESOURCE_HOLD_STATES:
                continue
            if other["payload"].get("station") != station["code"]:
                continue
            used_beds += int(other["payload"].get("required_beds", 0))
            used_ventilators += int(other["payload"].get("required_ventilators", 0))
        missing_beds = int(payload["required_beds"]) - (int(station["beds_total"]) - used_beds)
        missing_ventilators = int(payload["required_ventilators"]) - (int(station["ventilators_total"]) - used_ventilators)
        if missing_beds > 0 or missing_ventilators > 0:
            gaps.append({
                "at": now, "reason": "resource_shortage", "pool_code": pool["code"],
                "detail": "接收台%s容量不足：床位缺口%s、呼吸机缺口%s" % (
                    station["code"], max(0, missing_beds), max(0, missing_ventilators)),
                "missing_beds": max(0, missing_beds), "missing_ventilators": max(0, missing_ventilators), **window,
            })
        return gaps

    def build_reserved(
        self, record: Dict[str, Any], pool: Dict[str, Any], start: datetime, end: datetime
    ) -> Tuple[Dict[str, Any], str]:
        payload = dict(record["payload"])
        assignment = {
            "pool_id": pool["id"],
            "pool_code": pool["code"],
            "scheduled_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "estimated_minutes": int((end - start).total_seconds() // 60),
            "preoccupied_beds": int(payload["required_beds"]),
            "preoccupied_ventilators": int(payload["required_ventilators"]),
        }
        payload["assignment"] = assignment
        payload["preoccupied"] = {"beds": assignment["preoccupied_beds"], "ventilators": assignment["preoccupied_ventilators"]}
        payload["zone"] = ZONE_LABELS[STATE_RESERVED]
        summary = "已预占床位%s、呼吸机%s并锁定池位%s（%s起%s分钟），洗消前不得收治" % (
            assignment["preoccupied_beds"], assignment["preoccupied_ventilators"],
            pool["code"], assignment["scheduled_at"], assignment["estimated_minutes"])
        return payload, summary

    def build_blocked(self, record: Dict[str, Any], gaps: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = dict(record["payload"])
        payload["gaps"] = list(payload.get("gaps") or []) + gaps
        payload["zone"] = ZONE_LABELS[STATE_PENDING]
        return payload

    def start_decontamination(self, record: Dict[str, Any], now: str) -> Tuple[Dict[str, Any], str]:
        payload = dict(record["payload"])
        assignment = payload.get("assignment") or {}
        attempt = {
            "pool_code": assignment.get("pool_code", ""),
            "scheduled_at": assignment.get("scheduled_at", ""),
            "started_at": now,
            "reviewed_at": None,
            "passed": None,
            "note": "",
        }
        attempts = list(payload.get("decon_attempts") or [])
        attempts.append(attempt)
        payload["decon_attempts"] = attempts
        payload["decontamination"] = attempt
        payload["zone"] = ZONE_LABELS[STATE_DECONTAMINATING]
        # 红色伤员同样先洗消：收治只能由本状态之后的复核触发，无任何旁路。
        summary = "批次（%s，含%s级伤员）已在池位%s开始洗消" % (
            payload.get("contamination_level"), payload.get("triage"), attempt["pool_code"])
        return payload, summary

    def review(self, record: Dict[str, Any], passed: bool, note: str, now: str) -> Tuple[str, Dict[str, Any], str, Dict[str, Any]]:
        payload = dict(record["payload"])
        attempts = list(payload.get("decon_attempts") or [])
        if not attempts:
            raise Conflict("尚未开始洗消，不能复核")
        attempts = [dict(item) for item in attempts]
        attempts[-1].update({"reviewed_at": now, "passed": bool(passed), "note": note})
        payload["decon_attempts"] = attempts
        payload["decontamination"] = attempts[-1]

        if passed:
            payload["admission"] = {"admitted_at": now, "note": note}
            payload["occupied"] = {"beds": int(payload["required_beds"]), "ventilators": int(payload["required_ventilators"])}
            payload["preoccupied"] = {"beds": 0, "ventilators": 0}
            payload["assignment"] = None
            payload["zone"] = ZONE_LABELS[STATE_ADMITTED]
            summary = "去污复核通过，预占转为正式收治：床位%s、呼吸机%s" % (
                payload["occupied"]["beds"], payload["occupied"]["ventilators"])
            return STATE_ADMITTED, payload, "admitted", {"summary": summary}

        pool_code = attempts[-1].get("pool_code", "")
        gap = {
            "at": now,
            "reason": "review_failed",
            "pool_code": pool_code,
            "detail": "去污复核未通过%s；批次退回待安排区，需重新安排洗消与复核" % ("：%s" % note if note else ""),
            "missing_beds": 0,
            "missing_ventilators": 0,
            "scheduled_at": attempts[-1].get("scheduled_at", ""),
        }
        payload["gaps"] = list(payload.get("gaps") or []) + [gap]
        payload["preoccupied"] = {"beds": 0, "ventilators": 0}
        payload["assignment"] = None
        payload["zone"] = ZONE_LABELS[STATE_PENDING]
        summary = "去污复核未通过，释放预占并退回待安排区"
        return STATE_PENDING, payload, "returned", {"summary": summary, "reason": "review_failed", "gap": gap}

    def build_pool_fault_return(self, record: Dict[str, Any], pool: Dict[str, Any], note: str, now: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        payload = dict(record["payload"])
        assignment = payload.get("assignment") or {}
        detail = "池位%s故障%s；批次退回待安排区，需重新安排池位（床位与呼吸机预占已释放）" % (
            pool["code"], "：%s" % note if note else "")
        gap = {
            "at": now,
            "reason": "pool_fault",
            "pool_code": pool["code"],
            "detail": detail,
            "missing_beds": 0,
            "missing_ventilators": 0,
            "scheduled_at": assignment.get("scheduled_at", ""),
        }
        payload["gaps"] = list(payload.get("gaps") or []) + [gap]
        payload["assignment"] = None
        payload["preoccupied"] = {"beds": 0, "ventilators": 0}
        payload["decontamination"] = payload.get("decontamination")
        payload["zone"] = ZONE_LABELS[STATE_PENDING]
        details = {
            "from": record["state"],
            "to": STATE_PENDING,
            "reason": "pool_fault",
            "summary": detail,
            "gap": gap,
        }
        return payload, details

    def cancel(self, record: Dict[str, Any], reason: str) -> Tuple[Dict[str, Any], str]:
        payload = dict(record["payload"])
        payload["assignment"] = None
        payload["preoccupied"] = {"beds": 0, "ventilators": 0}
        payload["cancel_reason"] = reason
        payload["zone"] = ZONE_LABELS[STATE_CANCELLED]
        return payload, "批次取消：%s" % reason
