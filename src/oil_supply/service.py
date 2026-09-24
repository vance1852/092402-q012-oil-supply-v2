"""报价、库存、线路、提名和预测版本的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .forecasting import (
    decompose_deviation,
    diff_versions,
    resolve_effective_version,
    weighted_average_price,
)
from .models import (
    PRODUCTS,
    ForecastActual,
    ForecastDraft,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SupplyScenario,
    date_text,
    decimal_value,
    identifier,
    required_text,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_money,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "forecast.write", "forecast.read"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write", "forecast.cutoff", "forecast.actual", "forecast.close", "forecast.read"},
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

    def allocate(self, actor_id: str, route_id: str, service_date: str, forecast_version_id: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("线路不存在")
        forecast_version = None
        if forecast_version_id is not None:
            forecast_version = self._forecast_row(int(forecast_version_id))
            if forecast_version["state"] not in ("approved", "superseded"):
                raise InvalidState("只能引用已批准的预测版本")
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
            if forecast_version is not None:
                summary = self._version_summary(forecast_version)
                self.connection.execute(
                    "INSERT INTO allocation_forecast_links(allocation_id,forecast_version_id,version_summary_json,"
                    "summary_sha256,created_at) VALUES(?,?,?,?,?)",
                    (allocation_id, forecast_version["version_id"], canonical_json(summary), digest(summary), self._now()),
                )
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        response = {"allocation_id": allocation_id, **result}
        if forecast_version is not None:
            response["forecast_version_id"] = forecast_version["version_id"]
        return response

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

    def _forecast_row(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM forecast_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预测版本不存在")
        return row

    @staticmethod
    def _forecast_key(region: object, product: object, business_day: object) -> tuple[str, str, str]:
        product_text = required_text(product, "product", 32)
        if product_text not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        return (
            identifier(region, "region"),
            product_text,
            date_text(business_day, "business_day"),
        )

    @staticmethod
    def _version_summary(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        return {
            "version_id": data["version_id"],
            "region": data["region"],
            "product": data["product"],
            "business_day": data["business_day"],
            "quantity_barrels": data["quantity_barrels"],
            "price_assumption_usd": data["price_assumption_usd"],
            "price_elasticity": data["price_elasticity"],
            "note": data["note"],
            "state": data["state"],
            "revision": data["revision"],
            "content_sha256": data["content_sha256"],
            "approved_by": data["approved_by"],
            "approved_at": data["approved_at"],
            "effective_from": data["effective_from"],
        }

    def _version_view(self, row: sqlite3.Row) -> dict[str, Any]:
        view = self._version_summary(row)
        data = dict(row)
        view.update({
            "supersedes_version_id": data["supersedes_version_id"],
            "created_by": data["created_by"],
            "created_at": data["created_at"],
        })
        return view

    @staticmethod
    def _cutoff_view(row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        data = dict(row)
        return {
            "cutoff_id": data["cutoff_id"],
            "region": data["region"],
            "product": data["product"],
            "business_day": data["business_day"],
            "resolved_version_id": data["resolved_version_id"],
            "resolved_at": data["resolved_at"],
            "summary_sha256": data["summary_sha256"],
            "version_summary": json.loads(data["version_summary_json"]),
            "replayed": replayed,
        }

    def submit_forecast(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "forecast.write")
        draft = ForecastDraft.from_dict(raw)
        content = {
            "region": draft.region,
            "product": draft.product,
            "business_day": draft.business_day,
            "quantity_barrels": decimal_text(quantize_volume(draft.quantity_barrels)),
            "price_assumption_usd": decimal_text(quantize_money(draft.price_assumption_usd)),
            "price_elasticity": decimal_text(draft.price_elasticity),
            "note": draft.note,
        }
        content_sha256 = digest(content)
        key = (draft.region, draft.product, draft.business_day)
        try:
            with transaction(self.connection, immediate=True):
                existing = self.connection.execute(
                    "SELECT * FROM forecast_versions WHERE region=? AND product=? AND business_day=? AND state='draft'",
                    key,
                ).fetchone()
                if existing is None:
                    cursor = self.connection.execute(
                        "INSERT INTO forecast_versions(region,product,business_day,quantity_barrels,price_assumption_usd,"
                        "price_elasticity,note,content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            draft.region,
                            draft.product,
                            draft.business_day,
                            content["quantity_barrels"],
                            content["price_assumption_usd"],
                            content["price_elasticity"],
                            draft.note,
                            content_sha256,
                            actor_id,
                            self._now(),
                        ),
                    )
                    version_id = int(cursor.lastrowid)
                    self._audit("forecast_version", str(version_id), "forecast.draft_created", actor_id, content)
                    return {"version_id": version_id, "state": "draft", "revision": 1, "merged": False, "content_sha256": content_sha256}
                revision = int(existing["revision"]) + 1
                self.connection.execute(
                    "UPDATE forecast_versions SET quantity_barrels=?,price_assumption_usd=?,price_elasticity=?,"
                    "note=?,content_sha256=?,revision=? WHERE version_id=? AND state='draft'",
                    (
                        content["quantity_barrels"],
                        content["price_assumption_usd"],
                        content["price_elasticity"],
                        draft.note,
                        content_sha256,
                        revision,
                        existing["version_id"],
                    ),
                )
                self._audit(
                    "forecast_version",
                    str(existing["version_id"]),
                    "forecast.draft_merged",
                    actor_id,
                    {**content, "revision": revision},
                )
                return {"version_id": existing["version_id"], "state": "draft", "revision": revision, "merged": True, "content_sha256": content_sha256}
        except sqlite3.IntegrityError as exc:
            raise Conflict("预测草稿合并冲突") from exc

    def approve_forecast(self, actor_id: str, version_id: int, effective_from: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "forecast.approve")
        row = self._forecast_row(version_id)
        if row["state"] != "draft":
            raise InvalidState("只有草稿版本可以批准")
        if effective_from is None:
            effective = self._now()
        else:
            try:
                effective = utc_text(parse_utc(effective_from, "effective_from"))
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
        approved_at = self._now()
        with transaction(self.connection, immediate=True):
            current = self.connection.execute(
                "SELECT version_id FROM forecast_versions WHERE region=? AND product=? AND business_day=? AND state='approved'",
                (row["region"], row["product"], row["business_day"]),
            ).fetchone()
            if current is not None:
                self.connection.execute(
                    "UPDATE forecast_versions SET state='superseded',revision=revision+1 "
                    "WHERE version_id=? AND state='approved'",
                    (current["version_id"],),
                )
            cursor = self.connection.execute(
                "UPDATE forecast_versions SET state='approved',approved_by=?,approved_at=?,effective_from=?,"
                "supersedes_version_id=?,revision=revision+1 WHERE version_id=? AND state='draft'",
                (
                    actor_id,
                    approved_at,
                    effective,
                    None if current is None else current["version_id"],
                    version_id,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidState("版本状态已变化，无法批准")
            self._audit(
                "forecast_version",
                str(version_id),
                "forecast.approved",
                actor_id,
                {
                    "effective_from": effective,
                    "supersedes_version_id": None if current is None else current["version_id"],
                },
            )
        return {
            "version_id": version_id,
            "state": "approved",
            "approved_by": actor_id,
            "approved_at": approved_at,
            "effective_from": effective,
            "supersedes_version_id": None if current is None else current["version_id"],
        }

    def withdraw_forecast(self, actor_id: str, version_id: int) -> dict[str, Any]:
        self._require(actor_id, "forecast.write")
        self._forecast_row(version_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE forecast_versions SET state='withdrawn',revision=revision+1 "
                "WHERE version_id=? AND state='draft'",
                (version_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有草稿版本可以撤回")
            self._audit("forecast_version", str(version_id), "forecast.withdrawn", actor_id, {})
        return {"version_id": version_id, "state": "withdrawn"}

    def list_forecasts(
        self,
        actor_id: str,
        region: str | None = None,
        product: str | None = None,
        business_day: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "forecast.read")
        clauses: list[str] = []
        params: list[Any] = []
        if region:
            clauses.append("region=?")
            params.append(region)
        if product:
            clauses.append("product=?")
            params.append(product)
        if business_day:
            clauses.append("business_day=?")
            params.append(business_day)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM forecast_versions{where} ORDER BY region,product,business_day,version_id",
            params,
        ).fetchall()
        return {"versions": [self._version_view(row) for row in rows]}

    def compare_forecasts(self, actor_id: str, version_a: int, version_b: int) -> dict[str, Any]:
        self._require(actor_id, "forecast.read")
        first = self._forecast_row(version_a)
        second = self._forecast_row(version_b)
        diff = diff_versions(first, second)
        return {
            "a": self._version_view(first),
            "b": self._version_view(second),
            "changes": diff["changes"],
            "quantity_delta": diff["quantity_delta"],
            "price_delta": diff["price_delta"],
        }

    def run_cutoff(self, actor_id: str, region: str, product: str, business_day: str) -> dict[str, Any]:
        self._require(actor_id, "forecast.cutoff")
        key = self._forecast_key(region, product, business_day)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                existing = self.connection.execute(
                    "SELECT * FROM forecast_cutoffs WHERE region=? AND product=? AND business_day=?",
                    key,
                ).fetchone()
                if existing is not None:
                    return self._cutoff_view(existing, replayed=True)
                versions = self.connection.execute(
                    "SELECT * FROM forecast_versions WHERE region=? AND product=? AND business_day=?",
                    key,
                ).fetchall()
                resolved = resolve_effective_version(versions, now)
                if resolved is None:
                    raise InvalidState("截单时刻没有已生效的预测版本")
                summary = self._version_summary(resolved)
                summary_sha256 = digest(summary)
                cursor = self.connection.execute(
                    "INSERT INTO forecast_cutoffs(region,product,business_day,resolved_version_id,resolved_at,"
                    "version_summary_json,summary_sha256,created_by) VALUES(?,?,?,?,?,?,?,?)",
                    (*key, resolved["version_id"], now, canonical_json(summary), summary_sha256, actor_id),
                )
                cutoff_id = int(cursor.lastrowid)
                self._audit(
                    "forecast_cutoff",
                    str(cutoff_id),
                    "forecast.cutoff_resolved",
                    actor_id,
                    {"resolved_version_id": resolved["version_id"], "summary_sha256": summary_sha256},
                )
                row = self.connection.execute(
                    "SELECT * FROM forecast_cutoffs WHERE cutoff_id=?", (cutoff_id,)
                ).fetchone()
                return self._cutoff_view(row, replayed=False)
        except sqlite3.IntegrityError:
            existing = self.connection.execute(
                "SELECT * FROM forecast_cutoffs WHERE region=? AND product=? AND business_day=?",
                key,
            ).fetchone()
            if existing is None:
                raise
            return self._cutoff_view(existing, replayed=True)

    def get_cutoff(self, actor_id: str, region: str, product: str, business_day: str) -> dict[str, Any]:
        self._require(actor_id, "forecast.read")
        key = self._forecast_key(region, product, business_day)
        row = self.connection.execute(
            "SELECT * FROM forecast_cutoffs WHERE region=? AND product=? AND business_day=?",
            key,
        ).fetchone()
        if row is None:
            raise NotFound("截单记录不存在")
        return self._cutoff_view(row, replayed=False)

    def allocation_forecast_summary(self, actor_id: str, allocation_id: int) -> dict[str, Any]:
        self._require(actor_id, "forecast.read")
        rows = self.connection.execute(
            "SELECT * FROM allocation_forecast_links WHERE allocation_id=? ORDER BY forecast_version_id",
            (allocation_id,),
        ).fetchall()
        if not rows:
            raise NotFound("分配运行没有引用预测版本")
        return {
            "allocation_id": allocation_id,
            "forecasts": [
                {
                    "forecast_version_id": row["forecast_version_id"],
                    "summary_sha256": row["summary_sha256"],
                    "version_summary": json.loads(row["version_summary_json"]),
                }
                for row in rows
            ],
        }

    def _actual_totals(self, key: tuple[str, str, str]) -> tuple[Decimal, Decimal]:
        rows = self.connection.execute(
            "SELECT delivered_barrels,price_usd FROM forecast_actuals "
            "WHERE region=? AND product=? AND business_day=? ORDER BY actual_id",
            key,
        ).fetchall()
        if not rows:
            raise InvalidState("没有实绩记录，无法结账")
        total = sum((Decimal(row["delivered_barrels"]) for row in rows), Decimal("0"))
        return quantize_volume(total), weighted_average_price(rows)

    def record_actual(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "forecast.actual")
        actual = ForecastActual.from_dict(raw)
        request_digest = digest({
            "region": actual.region,
            "product": actual.product,
            "business_day": actual.business_day,
            "delivered_barrels": decimal_text(actual.delivered_barrels),
            "price_usd": decimal_text(actual.price_usd),
            "source": actual.source,
        })
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency "
            "WHERE scope='forecast_actual' AND idempotency_key=?",
            (actual.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同实绩内容")
            return json.loads(stored["response_json"])
        key = (actual.region, actual.product, actual.business_day)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO forecast_actuals(region,product,business_day,delivered_barrels,price_usd,source,"
                    "idempotency_key,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        *key,
                        decimal_text(actual.delivered_barrels),
                        decimal_text(actual.price_usd),
                        actual.source,
                        actual.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                actual_id = int(cursor.lastrowid)
                closed = self.connection.execute(
                    "SELECT analysis_id FROM deviation_analyses WHERE region=? AND product=? AND business_day=? "
                    "AND trigger_kind='initial_close'",
                    key,
                ).fetchone()
                successor_id = None
                if closed is not None:
                    successor_id = self._successor_analysis(actor_id, key)
                response = {"actual_id": actual_id, "late": closed is not None, "successor_analysis_id": successor_id}
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('forecast_actual',?,?,?,?)",
                    (actual.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "forecast_actual",
                    str(actual_id),
                    "forecast.actual_recorded",
                    actor_id,
                    {
                        "region": actual.region,
                        "product": actual.product,
                        "business_day": actual.business_day,
                        "successor_analysis_id": successor_id,
                    },
                )
                return response
        except sqlite3.IntegrityError as exc:
            raise Conflict("实绩幂等键冲突") from exc

    def _insert_analysis(
        self,
        actor_id: str,
        key: tuple[str, str, str],
        *,
        revision: int,
        forecast_version_id: int,
        version_source: str,
        actual_price: Decimal,
        available_supply: str,
        result: Mapping[str, Any],
        trigger_kind: str,
        supersedes_analysis_id: int | None,
    ) -> int:
        version = self._forecast_row(forecast_version_id)
        cursor = self.connection.execute(
            "INSERT INTO deviation_analyses(region,product,business_day,revision,forecast_version_id,version_source,"
            "forecast_quantity,actual_quantity,forecast_price,actual_price,available_supply,price_effect,supply_effect,"
            "unexplained,total_deviation,quantity_closed,trigger_kind,supersedes_analysis_id,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *key,
                revision,
                forecast_version_id,
                version_source,
                result["forecast_quantity"],
                result["actual_quantity"],
                decimal_text(quantize_money(Decimal(version["price_assumption_usd"]))),
                decimal_text(actual_price),
                available_supply,
                result["price_effect"],
                result["supply_effect"],
                result["unexplained"],
                result["total_deviation"],
                1 if result["quantity_closed"] else 0,
                trigger_kind,
                supersedes_analysis_id,
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def _successor_analysis(self, actor_id: str, key: tuple[str, str, str]) -> int:
        previous = self.connection.execute(
            "SELECT * FROM deviation_analyses WHERE region=? AND product=? AND business_day=? "
            "ORDER BY revision DESC LIMIT 1",
            key,
        ).fetchone()
        version = self._forecast_row(int(previous["forecast_version_id"]))
        total_quantity, average_price = self._actual_totals(key)
        result = decompose_deviation(
            forecast_quantity=Decimal(version["quantity_barrels"]),
            actual_quantity=total_quantity,
            forecast_price=Decimal(version["price_assumption_usd"]),
            actual_price=average_price,
            price_elasticity=Decimal(version["price_elasticity"]),
            available_supply=Decimal(previous["available_supply"]),
        )
        revision = int(previous["revision"]) + 1
        analysis_id = self._insert_analysis(
            actor_id,
            key,
            revision=revision,
            forecast_version_id=int(previous["forecast_version_id"]),
            version_source=previous["version_source"],
            actual_price=average_price,
            available_supply=previous["available_supply"],
            result=result,
            trigger_kind="late_actual",
            supersedes_analysis_id=int(previous["analysis_id"]),
        )
        self._audit(
            "deviation_analysis",
            str(analysis_id),
            "forecast.successor_analysis",
            actor_id,
            {"revision": revision, "supersedes_analysis_id": previous["analysis_id"]},
        )
        return analysis_id

    def close_forecast_day(
        self,
        actor_id: str,
        region: str,
        product: str,
        business_day: str,
        available_supply: object,
    ) -> dict[str, Any]:
        self._require(actor_id, "forecast.close")
        key = self._forecast_key(region, product, business_day)
        supply = decimal_value(available_supply, "available_supply_barrels", minimum=Decimal("0"))
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT analysis_id FROM deviation_analyses WHERE region=? AND product=? AND business_day=? "
                "AND trigger_kind='initial_close'",
                key,
            ).fetchone()
            if existing is not None:
                raise InvalidState("营业日已结账，迟到实绩只会生成后继分析")
            cutoff = self.connection.execute(
                "SELECT resolved_version_id FROM forecast_cutoffs WHERE region=? AND product=? AND business_day=?",
                key,
            ).fetchone()
            if cutoff is not None:
                version_id = int(cutoff["resolved_version_id"])
                version_source = "cutoff"
            else:
                versions = self.connection.execute(
                    "SELECT * FROM forecast_versions WHERE region=? AND product=? AND business_day=?",
                    key,
                ).fetchall()
                resolved = resolve_effective_version(versions, self._now())
                if resolved is None:
                    raise InvalidState("没有可用于结账的预测版本")
                version_id = int(resolved["version_id"])
                version_source = "resolved_at_close"
            version = self._forecast_row(version_id)
            total_quantity, average_price = self._actual_totals(key)
            result = decompose_deviation(
                forecast_quantity=Decimal(version["quantity_barrels"]),
                actual_quantity=total_quantity,
                forecast_price=Decimal(version["price_assumption_usd"]),
                actual_price=average_price,
                price_elasticity=Decimal(version["price_elasticity"]),
                available_supply=supply,
            )
            analysis_id = self._insert_analysis(
                actor_id,
                key,
                revision=1,
                forecast_version_id=version_id,
                version_source=version_source,
                actual_price=average_price,
                available_supply=decimal_text(quantize_volume(supply)),
                result=result,
                trigger_kind="initial_close",
                supersedes_analysis_id=None,
            )
            self._audit(
                "deviation_analysis",
                str(analysis_id),
                "forecast.day_closed",
                actor_id,
                {"forecast_version_id": version_id, "version_source": version_source},
            )
        return {
            "analysis_id": analysis_id,
            "revision": 1,
            "forecast_version_id": version_id,
            "version_source": version_source,
            **result,
        }

    def list_analyses(self, actor_id: str, region: str, product: str, business_day: str) -> dict[str, Any]:
        self._require(actor_id, "forecast.read")
        key = self._forecast_key(region, product, business_day)
        rows = self.connection.execute(
            "SELECT * FROM deviation_analyses WHERE region=? AND product=? AND business_day=? ORDER BY revision",
            key,
        ).fetchall()
        analyses = []
        for row in rows:
            view = dict(row)
            view["quantity_closed"] = bool(view["quantity_closed"])
            analyses.append(view)
        return {
            "region": key[0],
            "product": key[1],
            "business_day": key[2],
            "analyses": analyses,
        }

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
