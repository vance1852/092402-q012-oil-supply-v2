"""报价、库存、线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text, utc_text_fixed
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    ForecastActual,
    ForecastDraftSubmission,
    ForecastKey,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SupplyScenario,
    decimal_value,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    compare_forecast_lines,
    decimal_text,
    decompose_variance,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    merge_forecast_lines,
    moving_average,
    quantize_money,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "forecast.write", "forecast.read"},
    "sales": {"forecast.write", "forecast.read"},
    "dispatcher": {
        "nomination.write",
        "allocation.run",
        "transfer.write",
        "inventory.write",
        "forecast.read",
        "forecast.cutoff",
        "forecast.actuals",
    },
    "risk": {"outage.write", "scenario.approve", "report.read", "forecast.approve", "forecast.read"},
    "auditor": {"report.read", "audit.read", "forecast.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM price_index_quotes WHERE price_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.price_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO price_index_quotes(price_index,trade_date,close_usd,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.price_index,
                        quote.trade_date,
                        decimal_text(quote.close_usd),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"price_index": quote.price_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("报价版本冲突") from exc
        return {"quote_id": quote_id, "price_index": quote.price_index, "trade_date": quote.trade_date}

    def price_summary(self, price_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_usd FROM price_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM price_index_quotes "
            "WHERE price_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (price_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_usd"])) for row in rows]
        if not points:
            raise NotFound("没有基准报价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "price_index": price_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_usd": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_barrels,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_barrels),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("线路不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_barrels,available_barrels,"
                    "unit_cost_usd,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.unit_cost_usd),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("库存批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("库存批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_barrels,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_barrels),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_barrels"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_barrels"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_barrels=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_barrels"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可发运版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        allocated = Decimal(nomination["allocated_barrels"])
        available = Decimal(lot["available_barrels"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("库存批次与线路起点或油品不匹配")
        if available < allocated:
            raise Conflict("库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_barrels,"
                "expected_delivered_barrels,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_barrels": decimal_text(allocated),
            "expected_delivered_barrels": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_usd FROM price_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用报价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_barrels AS REAL)) available_barrels "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_usd"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_usd"]),
            price_index_drop_percent=scenario.price_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    # ---- 预测版本治理：草稿合并、审批冻结、滚动替代 ----

    def _forecast_row(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM forecast_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预测版本不存在")
        return row

    @staticmethod
    def _forecast_version_dict(row: sqlite3.Row) -> dict[str, Any]:
        lines = json.loads(row["lines_json"])
        total = sum((Decimal(line["quantity_barrels"]) for line in lines), Decimal("0"))
        return {
            "version_id": row["version_id"],
            "region_id": row["region_id"],
            "product": row["product"],
            "business_date": row["business_date"],
            "version_no": row["version_no"],
            "state": row["state"],
            "revision": row["revision"],
            "lines": lines,
            "total_quantity_barrels": decimal_text(quantize_volume(total)),
            "content_sha256": row["content_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "effective_at": row["effective_at"],
            "supersedes_version_id": row["supersedes_version_id"],
        }

    def submit_forecast_draft(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """提交草稿行并合并进当前草稿版本；已冻结键会滚动出新草稿。"""
        self._require(actor_id, "forecast.write")
        submission = ForecastDraftSubmission.from_dict(raw)
        key = submission.key
        submitted = [line.as_dict() for line in submission.lines]
        latest = self.connection.execute(
            "SELECT * FROM forecast_versions WHERE region_id=? AND product=? AND business_date=? "
            "ORDER BY version_no DESC LIMIT 1",
            key.as_tuple(),
        ).fetchone()
        try:
            with transaction(self.connection, immediate=True):
                if latest is not None and latest["state"] == "draft":
                    merged = merge_forecast_lines(json.loads(latest["lines_json"]), submitted)
                    content = canonical_json(merged)
                    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    self.connection.execute(
                        "UPDATE forecast_versions SET lines_json=?,content_sha256=?,revision=revision+1 "
                        "WHERE version_id=?",
                        (content, content_sha256, latest["version_id"]),
                    )
                    version_id = latest["version_id"]
                    seeded_from = None
                    action = "merged"
                else:
                    seed: list[dict[str, Any]] = []
                    seeded_from = None
                    if latest is not None:
                        head = self.connection.execute(
                            "SELECT * FROM forecast_versions WHERE region_id=? AND product=? AND business_date=? "
                            "AND state='approved'",
                            key.as_tuple(),
                        ).fetchone()
                        if head is not None:
                            seed = json.loads(head["lines_json"])
                            seeded_from = head["version_id"]
                    merged = merge_forecast_lines(seed, submitted)
                    content = canonical_json(merged)
                    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    version_no = 1 if latest is None else int(latest["version_no"]) + 1
                    cursor = self.connection.execute(
                        "INSERT INTO forecast_versions(region_id,product,business_date,version_no,lines_json,"
                        "content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            key.region_id,
                            key.product,
                            key.business_date,
                            version_no,
                            content,
                            content_sha256,
                            actor_id,
                            self._now(),
                        ),
                    )
                    version_id = int(cursor.lastrowid)
                    action = "opened"
                self._audit(
                    "forecast_version",
                    str(version_id),
                    "forecast.draft_merged",
                    actor_id,
                    {"line_keys": [line["line_key"] for line in submitted], "merge_action": action},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("预测草稿版本冲突") from exc
        row = self._forecast_row(version_id)
        return {
            **self._forecast_version_dict(row),
            "merge_action": action,
            "seeded_from_version_id": seeded_from,
        }

    def approve_forecast(
        self,
        actor_id: str,
        version_id: int,
        expected_revision: int,
        effective_at: str | None = None,
    ) -> dict[str, Any]:
        """审批并冻结草稿版本；同键已生效版本随即被滚动替代。"""
        self._require(actor_id, "forecast.approve")
        row = self._forecast_row(version_id)
        if effective_at is None:
            effective_text = utc_text_fixed(self.clock.now())
        else:
            try:
                effective_text = utc_text_fixed(parse_utc(effective_at, "effective_at"))
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE forecast_versions SET state='approved',approved_by=?,approved_at=?,effective_at=?,"
                "revision=revision+1 WHERE version_id=? AND state='draft' AND revision=?",
                (actor_id, self._now(), effective_text, version_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("预测版本不是当前草稿版本")
            head = self.connection.execute(
                "SELECT version_id FROM forecast_versions WHERE region_id=? AND product=? AND business_date=? "
                "AND state='approved' AND version_id<>?",
                (row["region_id"], row["product"], row["business_date"], version_id),
            ).fetchone()
            supersedes = None
            if head is not None:
                supersedes = head["version_id"]
                self.connection.execute(
                    "UPDATE forecast_versions SET state='superseded',revision=revision+1 WHERE version_id=?",
                    (supersedes,),
                )
                self.connection.execute(
                    "UPDATE forecast_versions SET supersedes_version_id=? WHERE version_id=?",
                    (supersedes, version_id),
                )
            self._audit(
                "forecast_version",
                str(version_id),
                "forecast.approved",
                actor_id,
                {"supersedes_version_id": supersedes, "effective_at": effective_text},
            )
        return self._forecast_version_dict(self._forecast_row(version_id))

    def forecast_version(self, actor_id: str, version_id: int) -> dict[str, Any]:
        self._require(actor_id, "forecast.read")
        return self._forecast_version_dict(self._forecast_row(version_id))

    def forecast_timeline(
        self, actor_id: str, region_id: str, product: str, business_date: str
    ) -> dict[str, Any]:
        """按键列出全部版本及批准人、生效时间，无需翻查旧日志。"""
        self._require(actor_id, "forecast.read")
        key = ForecastKey.from_values(region_id, product, business_date)
        rows = self.connection.execute(
            "SELECT * FROM forecast_versions WHERE region_id=? AND product=? AND business_date=? "
            "ORDER BY version_no",
            key.as_tuple(),
        ).fetchall()
        versions = []
        for row in rows:
            summary = self._forecast_version_dict(row)
            del summary["lines"]
            versions.append(summary)
        return {
            "region_id": key.region_id,
            "product": key.product,
            "business_date": key.business_date,
            "versions": versions,
        }

    def compare_forecasts(self, actor_id: str, version_id_a: int, version_id_b: int) -> dict[str, Any]:
        """比较任意两版预测，逐行给出增减并附批准人和生效时间。"""
        self._require(actor_id, "forecast.read")
        row_a = self._forecast_row(version_id_a)
        row_b = self._forecast_row(version_id_b)
        diff = compare_forecast_lines(json.loads(row_a["lines_json"]), json.loads(row_b["lines_json"]))
        return {
            "a": self._forecast_version_dict(row_a),
            "b": self._forecast_version_dict(row_b),
            **diff,
        }

    # ---- 截单任务：按可注入时钟解析当时有效版本，重复执行同一选择 ----

    @staticmethod
    def _cutoff_dict(row: sqlite3.Row) -> dict[str, Any]:
        summary = json.loads(row["input_summary_json"])
        return {
            "cutoff_id": row["cutoff_id"],
            "region_id": row["region_id"],
            "product": row["product"],
            "business_date": row["business_date"],
            "version_id": row["version_id"],
            "version_no": summary["version"]["version_no"],
            "supply_cap_barrels": row["supply_cap_barrels"],
            "resolved_at": row["resolved_at"],
            "input_sha256": row["input_sha256"],
            "input_summary": summary,
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def _cutoff_row(self, key: ForecastKey) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM forecast_cutoff_runs WHERE region_id=? AND product=? AND business_date=?",
            key.as_tuple(),
        ).fetchone()

    def run_forecast_cutoff(
        self, actor_id: str, region_id: str, product: str, business_date: str, supply_cap: object
    ) -> dict[str, Any]:
        """执行截单：解析当前时钟下有效版本并永久保留输入摘要；同键重复执行返回原选择。"""
        self._require(actor_id, "forecast.cutoff")
        key = ForecastKey.from_values(region_id, product, business_date)
        cap = decimal_value(supply_cap, "supply_cap_barrels", minimum=Decimal("0"))
        existing = self._cutoff_row(key)
        if existing is not None:
            return {**self._cutoff_dict(existing), "replayed": True}
        resolved_at = utc_text_fixed(self.clock.now())
        version = self.connection.execute(
            "SELECT * FROM forecast_versions WHERE region_id=? AND product=? AND business_date=? "
            "AND state IN ('approved','superseded') AND effective_at<=? "
            "ORDER BY effective_at DESC, version_no DESC LIMIT 1",
            (*key.as_tuple(), resolved_at),
        ).fetchone()
        if version is None:
            raise InvalidState("截单时没有已生效的预测版本")
        summary = {
            "version": self._forecast_version_dict(version),
            "supply_cap_barrels": decimal_text(quantize_volume(cap)),
            "resolved_at": resolved_at,
        }
        input_sha256 = digest(summary)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO forecast_cutoff_runs(region_id,product,business_date,version_id,"
                    "supply_cap_barrels,resolved_at,input_summary_json,input_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        key.region_id,
                        key.product,
                        key.business_date,
                        version["version_id"],
                        decimal_text(quantize_volume(cap)),
                        resolved_at,
                        canonical_json(summary),
                        input_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                cutoff_id = int(cursor.lastrowid)
                self._audit(
                    "forecast_cutoff",
                    str(cutoff_id),
                    "forecast.cutoff_completed",
                    actor_id,
                    {"version_id": version["version_id"], "resolved_at": resolved_at},
                )
        except sqlite3.IntegrityError:
            existing = self._cutoff_row(key)
            if existing is None:  # pragma: no cover - 唯一冲突只可能来自并发写入
                raise
            return {**self._cutoff_dict(existing), "replayed": True}
        row = self.connection.execute(
            "SELECT * FROM forecast_cutoff_runs WHERE cutoff_id=?", (cutoff_id,)
        ).fetchone()
        return {**self._cutoff_dict(row), "replayed": False}

    def forecast_cutoff(
        self, actor_id: str, region_id: str, product: str, business_date: str
    ) -> dict[str, Any]:
        self._require(actor_id, "forecast.read")
        key = ForecastKey.from_values(region_id, product, business_date)
        row = self._cutoff_row(key)
        if row is None:
            raise NotFound("截单运行不存在")
        return self._cutoff_dict(row)

    # ---- 实绩登记与偏差分解：数量闭合，迟到实绩只生成后继分析 ----

    @staticmethod
    def _actual_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "actual_id": row["actual_id"],
            "region_id": row["region_id"],
            "product": row["product"],
            "business_date": row["business_date"],
            "quantity_barrels": row["quantity_barrels"],
            "avg_price_usd": row["avg_price_usd"],
            "source": row["source"],
            "recorded_by": row["recorded_by"],
            "recorded_at": row["recorded_at"],
        }

    def record_forecast_actual(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记实际出库实绩；若已有偏差分析，迟到实绩只生成后继分析。"""
        self._require(actor_id, "forecast.actuals")
        actual = ForecastActual.from_dict(raw)
        key = actual.key
        quantity_text = decimal_text(quantize_volume(actual.quantity_barrels))
        price_text = decimal_text(quantize_money(actual.avg_price_usd))
        existing = self.connection.execute(
            "SELECT * FROM forecast_actuals WHERE region_id=? AND product=? AND business_date=? AND source=?",
            (*key.as_tuple(), actual.source),
        ).fetchone()
        if existing is not None:
            if existing["quantity_barrels"] != quantity_text or existing["avg_price_usd"] != price_text:
                raise Conflict("同一来源的实绩内容不一致")
            return {**self._actual_dict(existing), "replayed": True, "successor_analysis": None}
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO forecast_actuals(region_id,product,business_date,quantity_barrels,avg_price_usd,"
                "source,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    key.region_id,
                    key.product,
                    key.business_date,
                    quantity_text,
                    price_text,
                    actual.source,
                    actor_id,
                    self._now(),
                ),
            )
            actual_id = int(cursor.lastrowid)
            self._audit(
                "forecast_actual",
                str(actual_id),
                "forecast.actual_recorded",
                actor_id,
                {"source": actual.source, "quantity_barrels": quantity_text},
            )
        row = self.connection.execute(
            "SELECT * FROM forecast_actuals WHERE actual_id=?", (actual_id,)
        ).fetchone()
        successor = None
        analyzed = self.connection.execute(
            "SELECT 1 FROM forecast_variance_analyses WHERE region_id=? AND product=? AND business_date=? LIMIT 1",
            key.as_tuple(),
        ).fetchone()
        if analyzed is not None:
            successor = self._analyze_variance(actor_id, key)
        return {**self._actual_dict(row), "replayed": False, "successor_analysis": successor}

    @staticmethod
    def _analysis_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "analysis_id": row["analysis_id"],
            "region_id": row["region_id"],
            "product": row["product"],
            "business_date": row["business_date"],
            "analysis_seq": row["analysis_seq"],
            "cutoff_id": row["cutoff_id"],
            "supersedes_analysis_id": row["supersedes_analysis_id"],
            "input_sha256": row["input_sha256"],
            **json.loads(row["result_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def _analyze_variance(self, actor_id: str, key: ForecastKey) -> dict[str, Any]:
        cutoff = self._cutoff_row(key)
        if cutoff is None:
            raise InvalidState("缺少截单运行，无法分解偏差")
        actuals = self.connection.execute(
            "SELECT * FROM forecast_actuals WHERE region_id=? AND product=? AND business_date=? "
            "ORDER BY actual_id",
            key.as_tuple(),
        ).fetchall()
        if not actuals:
            raise InvalidState("实绩尚未登记，无法分解偏差")
        total_quantity = sum((Decimal(row["quantity_barrels"]) for row in actuals), Decimal("0"))
        total_value = sum(
            (Decimal(row["quantity_barrels"]) * Decimal(row["avg_price_usd"]) for row in actuals),
            Decimal("0"),
        )
        avg_price = total_value / total_quantity
        input_sha256 = digest({
            "cutoff": cutoff["input_sha256"],
            "actuals": [
                {
                    "source": row["source"],
                    "quantity_barrels": row["quantity_barrels"],
                    "avg_price_usd": row["avg_price_usd"],
                }
                for row in actuals
            ],
        })
        latest = self.connection.execute(
            "SELECT * FROM forecast_variance_analyses WHERE region_id=? AND product=? AND business_date=? "
            "ORDER BY analysis_seq DESC LIMIT 1",
            key.as_tuple(),
        ).fetchone()
        if latest is not None and latest["input_sha256"] == input_sha256:
            return {**self._analysis_dict(latest), "replayed": True}
        summary = json.loads(cutoff["input_summary_json"])
        decomposition = decompose_variance(
            forecast_lines=summary["version"]["lines"],
            actual_quantity=total_quantity,
            actual_avg_price=avg_price,
            supply_cap=Decimal(summary["supply_cap_barrels"]),
        )
        result = {
            **decomposition,
            "version_id": summary["version"]["version_id"],
            "version_no": summary["version"]["version_no"],
        }
        seq = 1 if latest is None else int(latest["analysis_seq"]) + 1
        supersedes = None if latest is None else latest["analysis_id"]
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO forecast_variance_analyses(region_id,product,business_date,analysis_seq,cutoff_id,"
                "supersedes_analysis_id,input_sha256,result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key.region_id,
                    key.product,
                    key.business_date,
                    seq,
                    cutoff["cutoff_id"],
                    supersedes,
                    input_sha256,
                    canonical_json(result),
                    actor_id,
                    self._now(),
                ),
            )
            analysis_id = int(cursor.lastrowid)
            self._audit(
                "forecast_variance",
                str(analysis_id),
                "forecast.variance_analyzed",
                actor_id,
                {"analysis_seq": seq, "supersedes_analysis_id": supersedes},
            )
        row = self.connection.execute(
            "SELECT * FROM forecast_variance_analyses WHERE analysis_id=?", (analysis_id,)
        ).fetchone()
        return {**self._analysis_dict(row), "replayed": False}

    def analyze_forecast_variance(
        self, actor_id: str, region_id: str, product: str, business_date: str
    ) -> dict[str, Any]:
        """实绩到齐后运行偏差分解；相同输入重复执行返回原分析。"""
        self._require(actor_id, "forecast.actuals")
        key = ForecastKey.from_values(region_id, product, business_date)
        return self._analyze_variance(actor_id, key)

    def forecast_variance_history(
        self, actor_id: str, region_id: str, product: str, business_date: str
    ) -> dict[str, Any]:
        self._require(actor_id, "forecast.read")
        key = ForecastKey.from_values(region_id, product, business_date)
        rows = self.connection.execute(
            "SELECT * FROM forecast_variance_analyses WHERE region_id=? AND product=? AND business_date=? "
            "ORDER BY analysis_seq",
            key.as_tuple(),
        ).fetchall()
        return {
            "region_id": key.region_id,
            "product": key.product,
            "business_date": key.business_date,
            "analyses": [self._analysis_dict(row) for row in rows],
        }
