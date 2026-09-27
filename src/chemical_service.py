"""化学事故分区接收台用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .chemical import (
    ChemicalRules,
    POOL_AVAILABLE,
    POOL_FAULTY,
    parse_window,
)
from .chemical_repository import ChemicalRepository
from .domain import Actor, PermissionDenied, boolean, integer, optional_text, text
from .repository import _now


class ChemicalService:
    def __init__(self, repository: ChemicalRepository, rules: ChemicalRules = None) -> None:
        self.repository = repository
        self.rules = rules or ChemicalRules()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _known(self, actor: Actor) -> None:
        if actor.role != "admin" and actor.role not in self.rules.known_roles():
            raise PermissionDenied("角色无权访问该服务")

    def _check(self, actor: Actor, action: str) -> None:
        self._known(actor)
        if not self.rules.role_can(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")

    # -- 接收台与池位管理 --------------------------------------------------

    def configure_station(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._known(actor)
        if actor.role != "admin" and actor.role not in {"incident_commander"}:
            raise PermissionDenied("角色无权配置接收台")
        return self.repository.upsert_station(self.rules.validate_station(data or {}))

    def list_stations(self, actor: Actor) -> List[Dict[str, Any]]:
        self._known(self._actor(actor))
        return self.repository.list_stations()

    def register_pool(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._known(actor)
        if actor.role != "admin" and actor.role not in {"incident_commander"}:
            raise PermissionDenied("角色无权登记洗消池")
        cleaned = self.rules.validate_pool(data or {})
        self.repository.get_station(cleaned["station"])
        return self.repository.create_pool(cleaned, actor.user_id)

    def list_pools(self, actor: Actor, station: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self._known(self._actor(actor))
        return self.repository.list_pools(station=station, status=status)

    def report_pool_fault(self, actor: Actor, pool_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._known(actor)
        if actor.role != "admin" and actor.role not in {"incident_commander", "decon_officer"}:
            raise PermissionDenied("角色无权标记池位故障")
        note = optional_text(data or {}, "note")
        pool = self.repository.get_pool_by_id(pool_id)
        affected = self.repository.mark_pool_faulty(pool, note)
        return {"pool": self.repository.get_pool_by_id(pool_id),
                "returned_batches": affected,
                "message": "池位已标记故障，%s个在等/在洗批次退回待安排区" % len(affected)}

    def repair_pool(self, actor: Actor, pool_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._known(actor)
        if actor.role != "admin" and actor.role not in {"incident_commander", "decon_officer"}:
            raise PermissionDenied("角色无权恢复池位")
        note = optional_text(data or {}, "note")
        pool = self.repository.set_pool_status(pool_id, POOL_AVAILABLE, note)
        return {"pool": pool, "message": "池位%s已恢复可用" % pool["code"]}

    # -- 批次登记与接收 ----------------------------------------------------

    def register_batch(self, actor: Actor, reference: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._known(actor)
        if actor.role != "admin" and actor.role not in {"incident_commander", "transport_coordinator"}:
            raise PermissionDenied("角色无权登记批次")
        reference = text({"reference": reference}, "reference")
        cleaned = self.rules.validate_registration(data or {})
        self.repository.get_station(cleaned["station"])
        payload = self.rules.initial_payload(cleaned)
        return self.repository.create_batch(reference, payload, actor.user_id)

    def list_batches(self, actor: Actor, state: Optional[str] = None, station: Optional[str] = None,
                     created_date: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        self._known(self._actor(actor))
        return self.repository.list_batches(state=state, station=station, created_date=created_date, limit=limit)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        self._known(self._actor(actor))
        return self.repository.get_batch(batch_id)

    def batch_timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        self._known(self._actor(actor))
        return self.repository.batch_timeline(batch_id)

    def act(self, actor: Actor, batch_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check(actor, action)
        data = data or {}
        record = self.repository.get_batch(batch_id)
        self.rules.require_state(record, action)
        now = _now()

        if action == "arrange":
            return self._arrange(actor, record, data, now, expected_version)
        if action == "start_decontamination":
            payload, summary = self.rules.start_decontamination(record, now)
            return self._save(record, expected_version, "decontaminating", "start_decontamination",
                              payload, {"summary": summary}, actor)
        if action == "review":
            passed = boolean(data, "decontamination_passed")
            note = optional_text(data, "review_note")
            new_state, payload, audit_action, details = self.rules.review(record, passed, note, now)
            return self._save(record, expected_version, new_state, audit_action, payload, details, actor)
        if action == "cancel":
            reason = text(data, "cancel_reason")
            payload, summary = self.rules.cancel(record, reason)
            return self._save(record, expected_version, "cancelled", "cancelled",
                              payload, {"summary": summary}, actor)
        raise PermissionDenied("未知操作")

    def _arrange(self, actor: Actor, record: Dict[str, Any], data: Dict[str, Any], now: str,
                 expected_version: int) -> Dict[str, Any]:
        pool_id = integer(data, "pool_id", 1)
        pool = self.repository.get_pool_by_id(pool_id)
        payload = record["payload"]
        if pool["station"] != payload.get("station"):
            from .domain import ValidationError
            raise ValidationError("洗消池不属于该批次所在分区")
        station = self.repository.get_station(payload["station"])
        start, end = parse_window(text(data, "scheduled_at"), integer(data, "estimated_minutes", 1))
        active = self.repository.list_active_batches(station["code"])
        gaps = self.rules.evaluate_arrangement(record, pool, station, active, start, end, now)
        if gaps:
            blocked_payload = self.rules.build_blocked(record, gaps)
            return self._save(
                record, expected_version, "pending_arrangement", "arrange_blocked", blocked_payload,
                {"summary": "安排失败，批次留在待安排区", "gaps": gaps}, actor)
        reserved_payload, summary = self.rules.build_reserved(record, pool, start, end)
        return self._save(record, expected_version, "reserved", "arrange", reserved_payload,
                          {"summary": summary, "pool_code": pool["code"]}, actor)

    def _save(self, record: Dict[str, Any], expected_version: int, new_state: str, audit_action: str,
              payload: Dict[str, Any], details: Dict[str, Any], actor: Actor) -> Dict[str, Any]:
        details = dict(details)
        details.setdefault("from", record["state"])
        details.setdefault("to", new_state)
        return self.repository.mutate_batch(
            batch_id=record["id"],
            expected_version=int(expected_version),
            state=new_state,
            payload=payload,
            actor_id=actor.user_id,
            action=audit_action,
            details=details,
        )
