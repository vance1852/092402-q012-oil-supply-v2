from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from oil_supply import acceptance
from oil_supply.api import JsonApplication
from oil_supply.cli import main as cli_main
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState
from oil_supply.forecasting import decompose_deviation, diff_versions, resolve_effective_version
from oil_supply.service import SupplyService


ROOT = Path(__file__).resolve().parents[1]


class DecompositionTests(unittest.TestCase):
    def test_decomposition_is_quantity_closed(self) -> None:
        result = decompose_deviation(
            forecast_quantity=Decimal("80000"),
            actual_quantity=Decimal("79500"),
            forecast_price=Decimal("96"),
            actual_price=Decimal("97"),
            price_elasticity=Decimal("-0.3"),
            available_supply=Decimal("79500"),
        )
        self.assertEqual(result["price_effect"], "-250.000")
        self.assertEqual(result["supply_effect"], "-250.000")
        self.assertEqual(result["unexplained"], "0.000")
        self.assertEqual(result["total_deviation"], "-500.000")
        self.assertTrue(result["quantity_closed"])

    def test_decomposition_without_elasticity_or_constraint(self) -> None:
        result = decompose_deviation(
            forecast_quantity=Decimal("1000"),
            actual_quantity=Decimal("900"),
            forecast_price=Decimal("100"),
            actual_price=Decimal("110"),
            price_elasticity=Decimal("0"),
            available_supply=Decimal("2000"),
        )
        self.assertEqual(result["price_effect"], "0.000")
        self.assertEqual(result["supply_effect"], "0.000")
        self.assertEqual(result["unexplained"], "-100.000")
        self.assertTrue(result["quantity_closed"])

    def test_resolve_effective_version_picks_latest_effective(self) -> None:
        versions = [
            {"version_id": 1, "state": "superseded", "effective_from": "2026-09-20T00:00:00Z"},
            {"version_id": 2, "state": "approved", "effective_from": "2026-09-24T10:00:00Z"},
            {"version_id": 3, "state": "draft", "effective_from": None},
        ]
        self.assertEqual(resolve_effective_version(versions, "2026-09-24T09:00:00Z")["version_id"], 1)
        self.assertEqual(resolve_effective_version(versions, "2026-09-24T10:30:00Z")["version_id"], 2)
        self.assertIsNone(resolve_effective_version(versions, "2026-09-19T00:00:00Z"))

    def test_diff_versions_reports_changes(self) -> None:
        diff = diff_versions(
            {"region": "north", "product": "crude", "business_day": "2026-09-25", "quantity_barrels": "80000.000", "price_assumption_usd": "96.00", "price_elasticity": "-0.3", "note": "初版", "state": "approved"},
            {"region": "north", "product": "crude", "business_day": "2026-09-25", "quantity_barrels": "78000.000", "price_assumption_usd": "95.50", "price_elasticity": "-0.3", "note": "初版", "state": "approved"},
        )
        self.assertEqual(diff["quantity_delta"], "-2000.000")
        self.assertEqual(diff["price_delta"], "-0.50")
        self.assertEqual(set(diff["changes"]), {"quantity_barrels", "price_assumption_usd"})


class ForecastServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
        self.service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
        self.service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def submit(self, quantity: str = "80000", note: str = "初版", day: str = "2026-09-25", price: str = "96") -> dict[str, object]:
        return self.service.submit_forecast("plan", {"region": "north", "product": "crude", "business_day": day, "quantity_barrels": quantity, "price_assumption_usd": price, "price_elasticity": "-0.3", "note": note})

    def approve(self, version_id: int, effective_from: str | None = None) -> dict[str, object]:
        return self.service.approve_forecast("risk", version_id, effective_from)

    def actual(self, delivered: str, price: str, idempotency_key: str) -> dict[str, object]:
        return self.service.record_actual("dispatch", {"region": "north", "product": "crude", "business_day": "2026-09-25", "delivered_barrels": delivered, "price_usd": price, "source": "wms", "idempotency_key": idempotency_key})

    def nominate(self) -> None:
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-1", "route_id": "pipe-a-b", "shipper_id": "refinery", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "key-1"})

    def test_draft_merge_keeps_single_open_draft(self) -> None:
        first = self.submit("82000", note="初版")
        merged = self.submit("80000", note="销售调低")
        self.assertEqual(first["version_id"], merged["version_id"])
        self.assertFalse(first["merged"])
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["revision"], 2)
        rows = self.connection.execute("SELECT * FROM forecast_versions WHERE state='draft'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity_barrels"], "80000.000")
        self.assertEqual(rows[0]["note"], "销售调低")

    def test_approve_freezes_and_rolls_supersede(self) -> None:
        first = self.submit("80000")
        approved = self.approve(first["version_id"])
        self.assertEqual(approved["approved_by"], "risk")
        self.assertEqual(approved["effective_from"], "2026-09-24T08:00:00Z")
        second = self.submit("78000")
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.approve(second["version_id"])
        versions = {row["version_id"]: row for row in self.connection.execute("SELECT * FROM forecast_versions").fetchall()}
        self.assertEqual(versions[first["version_id"]]["state"], "superseded")
        self.assertEqual(versions[first["version_id"]]["quantity_barrels"], "80000.000")
        self.assertEqual(versions[second["version_id"]]["state"], "approved")
        self.assertEqual(versions[second["version_id"]]["supersedes_version_id"], first["version_id"])
        with self.assertRaises(InvalidState):
            self.approve(first["version_id"])

    def test_approve_requires_risk_role(self) -> None:
        version = self.submit()
        with self.assertRaises(Forbidden):
            self.service.approve_forecast("plan", version["version_id"])
        with self.assertRaises(Forbidden):
            self.service.approve_forecast("dispatch", version["version_id"])

    def test_cutoff_replay_keeps_original_selection(self) -> None:
        first = self.submit("80000")
        self.approve(first["version_id"])
        second = self.submit("78000")
        self.approve(second["version_id"], effective_from="2026-09-24T10:00:00Z")
        self.clock.advance(hours=1)
        cutoff = self.service.run_cutoff("dispatch", "north", "crude", "2026-09-25")
        self.assertEqual(cutoff["resolved_version_id"], first["version_id"])
        self.assertFalse(cutoff["replayed"])
        self.clock.advance(hours=2)
        replay = self.service.run_cutoff("dispatch", "north", "crude", "2026-09-25")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["resolved_version_id"], first["version_id"])
        self.assertEqual(replay["version_summary"]["quantity_barrels"], "80000.000")

    def test_cutoff_resolves_version_effective_at_clock_time(self) -> None:
        first = self.submit("80000", day="2026-09-26")
        self.approve(first["version_id"], effective_from="2026-09-24T09:00:00Z")
        second = self.submit("78000", day="2026-09-26")
        self.approve(second["version_id"], effective_from="2026-09-24T10:00:00Z")
        self.clock.advance(hours=2, minutes=30)
        cutoff = self.service.run_cutoff("dispatch", "north", "crude", "2026-09-26")
        self.assertEqual(cutoff["resolved_version_id"], second["version_id"])

    def test_cutoff_without_effective_version_fails(self) -> None:
        self.submit()
        with self.assertRaises(InvalidState):
            self.service.run_cutoff("dispatch", "north", "crude", "2026-09-25")

    def test_allocation_retains_forecast_summary(self) -> None:
        version = self.submit("80000")
        self.approve(version["version_id"])
        self.nominate()
        allocation = self.service.allocate("dispatch", "pipe-a-b", "2026-09-25", forecast_version_id=version["version_id"])
        self.assertEqual(allocation["forecast_version_id"], version["version_id"])
        second = self.submit("78000")
        self.approve(second["version_id"])
        summary = self.service.allocation_forecast_summary("audit", allocation["allocation_id"])
        stored = summary["forecasts"][0]["version_summary"]
        self.assertEqual(stored["quantity_barrels"], "80000.000")
        self.assertEqual(stored["state"], "approved")
        self.assertEqual(len(summary["forecasts"][0]["summary_sha256"]), 64)

    def test_allocation_rejects_draft_forecast(self) -> None:
        version = self.submit()
        self.nominate()
        with self.assertRaises(InvalidState):
            self.service.allocate("dispatch", "pipe-a-b", "2026-09-25", forecast_version_id=version["version_id"])

    def test_close_decomposes_with_quantity_closure(self) -> None:
        version = self.submit("80000")
        self.approve(version["version_id"])
        self.actual("79000", "97", "act-1")
        self.actual("500", "97.5", "act-2")
        close = self.service.close_forecast_day("dispatch", "north", "crude", "2026-09-25", "79500")
        self.assertEqual(close["forecast_quantity"], "80000.000")
        self.assertEqual(close["actual_quantity"], "79500.000")
        self.assertEqual(close["price_effect"], "-250.000")
        self.assertEqual(close["supply_effect"], "-250.000")
        self.assertEqual(close["unexplained"], "0.000")
        self.assertEqual(close["total_deviation"], "-500.000")
        self.assertTrue(close["quantity_closed"])
        self.assertEqual(close["version_source"], "resolved_at_close")

    def test_close_uses_cutoff_selected_version(self) -> None:
        first = self.submit("80000")
        self.approve(first["version_id"])
        self.service.run_cutoff("dispatch", "north", "crude", "2026-09-25")
        second = self.submit("78000")
        self.approve(second["version_id"])
        self.actual("79500", "97", "act-1")
        close = self.service.close_forecast_day("dispatch", "north", "crude", "2026-09-25", "80000")
        self.assertEqual(close["forecast_version_id"], first["version_id"])
        self.assertEqual(close["version_source"], "cutoff")
        self.assertEqual(close["forecast_quantity"], "80000.000")

    def test_late_actual_generates_successor_only(self) -> None:
        version = self.submit("80000")
        self.approve(version["version_id"])
        self.actual("79000", "97", "act-1")
        self.actual("500", "97.5", "act-2")
        self.service.close_forecast_day("dispatch", "north", "crude", "2026-09-25", "79500")
        late = self.actual("300", "98", "act-3")
        self.assertTrue(late["late"])
        self.assertIsNotNone(late["successor_analysis_id"])
        analyses = self.service.list_analyses("audit", "north", "crude", "2026-09-25")["analyses"]
        self.assertEqual(len(analyses), 2)
        first, second = analyses
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["trigger_kind"], "initial_close")
        self.assertEqual(first["actual_quantity"], "79500.000")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["trigger_kind"], "late_actual")
        self.assertEqual(second["supersedes_analysis_id"], first["analysis_id"])
        self.assertEqual(second["actual_quantity"], "79800.000")
        self.assertEqual(second["unexplained"], "300.000")
        self.assertTrue(second["quantity_closed"])
        with self.assertRaises(InvalidState):
            self.service.close_forecast_day("dispatch", "north", "crude", "2026-09-25", "79500")

    def test_close_requires_actuals(self) -> None:
        version = self.submit()
        self.approve(version["version_id"])
        with self.assertRaises(InvalidState):
            self.service.close_forecast_day("dispatch", "north", "crude", "2026-09-25", "79500")

    def test_actual_recording_is_idempotent(self) -> None:
        first = self.actual("79000", "97", "act-1")
        replay = self.actual("79000", "97", "act-1")
        self.assertEqual(first, replay)
        with self.assertRaises(Conflict):
            self.actual("79100", "97", "act-1")

    def test_compare_versions_reports_delta_and_trace(self) -> None:
        first = self.submit("80000")
        self.approve(first["version_id"])
        second = self.submit("78000", price="95.5")
        self.approve(second["version_id"])
        comparison = self.service.compare_forecasts("plan", first["version_id"], second["version_id"])
        self.assertEqual(comparison["quantity_delta"], "-2000.000")
        self.assertEqual(comparison["price_delta"], "-0.50")
        self.assertEqual(comparison["changes"]["quantity_barrels"], {"from": "80000.000", "to": "78000.000"})
        self.assertEqual(comparison["a"]["approved_by"], "risk")
        self.assertEqual(comparison["a"]["effective_from"], "2026-09-24T08:00:00Z")
        self.assertEqual(comparison["a"]["state"], "superseded")
        self.assertEqual(comparison["b"]["state"], "approved")

    def test_audit_chain_covers_forecast_events(self) -> None:
        version = self.submit()
        self.approve(version["version_id"])
        self.service.run_cutoff("dispatch", "north", "crude", "2026-09-25")
        self.actual("79500", "97", "act-1")
        self.service.close_forecast_day("dispatch", "north", "crude", "2026-09-25", "79500")
        self.actual("100", "98", "act-2")
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        event_types = [row["event_type"] for row in self.connection.execute("SELECT event_type FROM supply_audit_events").fetchall()]
        self.assertIn("forecast.draft_created", event_types)
        self.assertIn("forecast.approved", event_types)
        self.assertIn("forecast.cutoff_resolved", event_types)
        self.assertIn("forecast.day_closed", event_types)
        self.assertIn("forecast.successor_analysis", event_types)


class ForecastApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
        self.service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
        self.service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_forecast_endpoints(self) -> None:
        plan = {"X-Actor-Id": "plan"}
        risk = {"X-Actor-Id": "risk"}
        dispatch = {"X-Actor-Id": "dispatch"}
        submit = self.app.handle("POST", "/forecasts", plan, json.dumps({"region": "north", "product": "crude", "business_day": "2026-09-25", "quantity_barrels": "80000", "price_assumption_usd": "96", "price_elasticity": "-0.3"}).encode())
        self.assertEqual(submit.status, 201)
        version_id = submit.body["version_id"]
        approve = self.app.handle("POST", f"/forecasts/{version_id}/approve", risk, b"{}")
        self.assertEqual(approve.status, 200)
        self.assertEqual(approve.body["approved_by"], "risk")
        second = self.app.handle("POST", "/forecasts", plan, json.dumps({"region": "north", "product": "crude", "business_day": "2026-09-25", "quantity_barrels": "78000", "price_assumption_usd": "95.5", "price_elasticity": "-0.3"}).encode())
        self.app.handle("POST", f"/forecasts/{second.body['version_id']}/approve", risk, b"{}")
        compare = self.app.handle("GET", f"/forecasts/compare?a={version_id}&b={second.body['version_id']}", plan)
        self.assertEqual(compare.status, 200)
        self.assertEqual(compare.body["quantity_delta"], "-2000.000")
        self.assertEqual(compare.body["a"]["approved_by"], "risk")
        listing = self.app.handle("GET", "/forecasts?region=north&product=crude&business_day=2026-09-25", plan)
        self.assertEqual(len(listing.body["versions"]), 2)
        cutoff = self.app.handle("POST", "/forecast-cutoffs", dispatch, json.dumps({"region": "north", "product": "crude", "business_day": "2026-09-25"}).encode())
        self.assertEqual(cutoff.status, 201)
        replay = self.app.handle("POST", "/forecast-cutoffs", dispatch, json.dumps({"region": "north", "product": "crude", "business_day": "2026-09-25"}).encode())
        self.assertTrue(replay.body["replayed"])
        self.assertEqual(replay.body["resolved_version_id"], cutoff.body["resolved_version_id"])
        self.app.handle("POST", "/nominations", dispatch, json.dumps({"nomination_id": "nom-1", "route_id": "pipe-a-b", "shipper_id": "refinery", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "key-1"}).encode())
        allocation = self.app.handle("POST", "/routes/pipe-a-b/allocate", dispatch, json.dumps({"service_date": "2026-09-25", "forecast_version_id": version_id}).encode())
        self.assertEqual(allocation.status, 200)
        summary = self.app.handle("GET", f"/allocations/{allocation.body['allocation_id']}/forecast", {"X-Actor-Id": "audit"})
        self.assertEqual(summary.status, 200)
        self.assertEqual(summary.body["forecasts"][0]["version_summary"]["quantity_barrels"], "80000.000")
        actual = self.app.handle("POST", "/forecast-actuals", dispatch, json.dumps({"region": "north", "product": "crude", "business_day": "2026-09-25", "delivered_barrels": "79500", "price_usd": "97", "source": "wms", "idempotency_key": "act-1"}).encode())
        self.assertEqual(actual.status, 201)
        self.assertFalse(actual.body["late"])
        close = self.app.handle("POST", "/forecast-closes", dispatch, json.dumps({"region": "north", "product": "crude", "business_day": "2026-09-25", "available_supply_barrels": "79500"}).encode())
        self.assertEqual(close.status, 200)
        self.assertTrue(close.body["quantity_closed"])
        analyses = self.app.handle("GET", "/forecast-analyses?region=north&product=crude&business_day=2026-09-25", plan)
        self.assertEqual(len(analyses.body["analyses"]), 1)


class ForecastCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "cli.sqlite3"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def cli(self, *args: str) -> dict[str, object]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--database", str(self.database), *args])
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())

    def test_cli_forecast_workflow(self) -> None:
        self.cli("user-create", "--user-id", "plan", "--display-name", "计划", "--role", "planner")
        self.cli("user-create", "--user-id", "risk", "--display-name", "风险", "--role", "risk")
        self.cli("user-create", "--user-id", "dispatch", "--display-name", "调度", "--role", "dispatcher")
        key = ["--region", "east", "--product", "gasoline-92", "--business-day", "2026-09-25"]
        first = self.cli("forecast-submit", "--actor", "plan", *key, "--quantity", "12000", "--price", "100", "--elasticity", "-0.5")
        merged = self.cli("forecast-submit", "--actor", "plan", *key, "--quantity", "11500", "--price", "100", "--elasticity", "-0.5")
        self.assertTrue(merged["merged"])
        self.assertEqual(first["version_id"], merged["version_id"])
        approved = self.cli("forecast-approve", "--actor", "risk", "--version-id", str(merged["version_id"]))
        self.assertEqual(approved["approved_by"], "risk")
        second = self.cli("forecast-submit", "--actor", "plan", *key, "--quantity", "11000", "--price", "99", "--elasticity", "-0.5")
        self.cli("forecast-approve", "--actor", "risk", "--version-id", str(second["version_id"]))
        comparison = self.cli("forecast-compare", "--actor", "plan", "--a", str(merged["version_id"]), "--b", str(second["version_id"]))
        self.assertEqual(comparison["quantity_delta"], "-500.000")
        self.assertEqual(comparison["a"]["approved_by"], "risk")
        self.assertIn("effective_from", comparison["a"])
        cutoff = self.cli("forecast-cutoff", "--actor", "dispatch", *key)
        replay = self.cli("forecast-cutoff", "--actor", "dispatch", *key)
        self.assertEqual(cutoff["resolved_version_id"], second["version_id"])
        self.assertEqual(replay["resolved_version_id"], cutoff["resolved_version_id"])
        self.assertTrue(replay["replayed"])
        self.cli("forecast-actual", "--actor", "dispatch", *key, "--delivered", "11400", "--price", "101", "--source", "wms", "--idempotency-key", "act-1")
        closed = self.cli("forecast-close", "--actor", "dispatch", *key, "--available-supply", "11300")
        self.assertTrue(closed["quantity_closed"])
        late = self.cli("forecast-actual", "--actor", "dispatch", *key, "--delivered", "100", "--price", "101", "--source", "wms-late", "--idempotency-key", "act-2")
        self.assertIsNotNone(late["successor_analysis_id"])
        analyses = self.cli("forecast-analyses", "--actor", "plan", *key)
        self.assertEqual(len(analyses["analyses"]), 2)
        self.assertEqual(analyses["analyses"][1]["trigger_kind"], "late_actual")


class ForecastAcceptanceTests(unittest.TestCase):
    def test_acceptance_includes_forecast_stage(self) -> None:
        result = acceptance.run(ROOT)
        self.assertEqual(result["status"], "ok")
        forecast = result["forecast"]
        self.assertTrue(forecast["cutoff_replayed"])
        self.assertEqual(forecast["cutoff_version_id"], forecast["allocation_forecast_version_id"])
        self.assertTrue(forecast["close_quantity_closed"])
        self.assertIsNotNone(forecast["successor_analysis_id"])
        self.assertEqual(forecast["compare_quantity_delta"], "-2000.000")
        self.assertEqual(forecast["analyses"], 2)
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
