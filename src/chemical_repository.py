"""化学事故分区接收台 SQLite 持久化。"""
import json
import sqlite3
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .repository import _now


DEFAULT_STATION = {
    "code": "MAIN",
    "name": "主分区接收台",
    "beds_total": 50,
    "ventilators_total": 12,
}


class ChemicalRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chem_stations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    beds_total INTEGER NOT NULL,
                    ventilators_total INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chem_pools (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    station TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chem_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chem_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES chem_batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chem_batches_state ON chem_batches(state);
                CREATE INDEX IF NOT EXISTS idx_chem_batches_created ON chem_batches(created_at);
                CREATE INDEX IF NOT EXISTS idx_chem_batches_station ON chem_batches(json_extract(payload, '$.station'));
                CREATE INDEX IF NOT EXISTS idx_chem_pools_station ON chem_pools(station);
                CREATE INDEX IF NOT EXISTS idx_chem_audit_batch ON chem_audit_events(batch_id, id);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO chem_stations(code,name,beds_total,ventilators_total,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    DEFAULT_STATION["code"], DEFAULT_STATION["name"],
                    DEFAULT_STATION["beds_total"], DEFAULT_STATION["ventilators_total"], _now(), _now(),
                ),
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # -- 接收台分区容量 ----------------------------------------------------

    def upsert_station(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id FROM chem_stations WHERE code=?", (data["code"],)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO chem_stations(code,name,beds_total,ventilators_total,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (data["code"], data["name"], data["beds_total"], data["ventilators_total"], now, now),
                )
            else:
                connection.execute(
                    "UPDATE chem_stations SET name=?,beds_total=?,ventilators_total=?,updated_at=? WHERE code=?",
                    (data["name"], data["beds_total"], data["ventilators_total"], now, data["code"]),
                )
            result = connection.execute("SELECT * FROM chem_stations WHERE code=?", (data["code"],)).fetchone()
            connection.commit()
        return dict(result)

    def get_station(self, code: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chem_stations WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("接收台不存在：%s" % code)
        return dict(row)

    def list_stations(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM chem_stations ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    # -- 洗消池 ------------------------------------------------------------

    def create_pool(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO chem_pools(code,station,name,status,status_note,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (data["code"], data["station"], data["name"], "available", "", actor_id, now, now),
                )
                pool_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM chem_pools WHERE id=?", (pool_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("池位编号已存在：%s" % data["code"]) from exc
        return dict(row)

    def get_pool_by_id(self, pool_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chem_pools WHERE id=?", (pool_id,)).fetchone()
        if row is None:
            raise NotFound("洗消池不存在")
        return dict(row)

    def list_pools(self, station: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM chem_pools"
        args: List[Any] = []
        where = []
        if station:
            where.append("station=?")
            args.append(station)
        if status:
            where.append("status=?")
            args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def set_pool_status(self, pool_id: int, status: str, note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute("SELECT id FROM chem_pools WHERE id=?", (pool_id,)).fetchone()
            if exists is None:
                connection.rollback()
                raise NotFound("洗消池不存在")
            connection.execute(
                "UPDATE chem_pools SET status=?,status_note=?,updated_at=? WHERE id=?",
                (status, note, now, pool_id),
            )
            row = connection.execute("SELECT * FROM chem_pools WHERE id=?", (pool_id,)).fetchone()
            connection.commit()
        return dict(row)

    # -- 批次 --------------------------------------------------------------

    def create_batch(self, reference: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO chem_batches(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (reference, "pending_arrangement", 1,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                batch_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO chem_audit_events(batch_id,action,actor_id,version,details,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (batch_id, "registered", actor_id, 1,
                     json.dumps({"state": "pending_arrangement", "summary": "批次登记，进入待安排区；未占用床位、呼吸机与洗消池"},
                                ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM chem_batches WHERE id=?", (batch_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次编号已存在") from exc
        return self._row(row)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chem_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._row(row)

    def list_batches(self, state: Optional[str] = None, station: Optional[str] = None,
                     created_date: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM chem_batches"
        where: List[str] = []
        args: List[Any] = []
        if state:
            where.append("state=?")
            args.append(state)
        if station:
            where.append("json_extract(payload, '$.station')=?")
            args.append(station)
        if created_date:
            where.append("substr(created_at, 1, 10)=?")
            args.append(created_date)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._row(row) for row in rows]

    def list_active_batches(self, station: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chem_batches"
                " WHERE state IN ('reserved','decontaminating','admitted')"
                " AND json_extract(payload, '$.station')=? ORDER BY id",
                (station,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mutate_batch(self, batch_id: int, expected_version: int, state: str, payload: Dict[str, Any],
                     actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM chem_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE chem_batches SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, batch_id),
            )
            connection.execute(
                "INSERT INTO chem_audit_events(batch_id,action,actor_id,version,details,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (batch_id, action, actor_id, version,
                 json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM chem_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def batch_timeline(self, batch_id: int) -> List[Dict[str, Any]]:
        self.get_batch(batch_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chem_audit_events WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def mark_pool_faulty(self, pool: Dict[str, Any], note: str) -> List[int]:
        """标记池位故障，并把占用该池的批次退回待安排区。返回受影响批次ID。"""
        now = _now()
        affected: List[int] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE chem_pools SET status='faulty',status_note=?,updated_at=? WHERE id=?",
                (note, now, pool["id"]),
            )
            rows = connection.execute(
                "SELECT * FROM chem_batches WHERE state IN ('reserved','decontaminating')"
                " AND json_extract(payload, '$.assignment.pool_id')=? ORDER BY id",
                (pool["id"],),
            ).fetchall()
            for row in rows:
                item = self._row(row)
                payload = item["payload"]
                assignment = payload.get("assignment") or {}
                gap = {
                    "at": now,
                    "reason": "pool_fault",
                    "pool_code": pool["code"],
                    "detail": "池位%s故障%s；批次退回待安排区，需重新安排池位（床位与呼吸机预占已释放）"
                              % (pool["code"], "：%s" % note if note else ""),
                    "missing_beds": 0,
                    "missing_ventilators": 0,
                    "scheduled_at": assignment.get("scheduled_at", ""),
                }
                payload["gaps"] = list(payload.get("gaps") or []) + [gap]
                payload["assignment"] = None
                payload["preoccupied"] = {"beds": 0, "ventilators": 0}
                payload["zone"] = "待安排区"
                version = int(item["version"]) + 1
                connection.execute(
                    "UPDATE chem_batches SET state='pending_arrangement',version=?,payload=?,updated_at=? WHERE id=?",
                    (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), now, item["id"]),
                )
                connection.execute(
                    "INSERT INTO chem_audit_events(batch_id,action,actor_id,version,details,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (item["id"], "returned", "system", version,
                     json.dumps({"from": item["state"], "to": "pending_arrangement",
                                 "reason": "pool_fault", "summary": gap["detail"], "gap": gap},
                                ensure_ascii=False, sort_keys=True), now),
                )
                affected.append(int(item["id"]))
            connection.commit()
        return affected

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
