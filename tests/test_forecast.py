from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

from oil_supply import acceptance
from oil_supply.api import JsonApplication
from oil_supply.cli import main as cli_main
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState, NotFound
from oil_supply.planning import decompose_variance
from oil_supply.service import SupplyService


REGION = "east"
PRODUCT = "gasoline-92"
DAY = "2026-09-25"


def draft_payload(lines: list[dict[str, object]], day: str = DAY, region: str = REGION) -> dict[str, object]:
    return {"region_id": region, "product": PRODUCT, "business_date": day, "lines": lines}


def actual_payload(quantity: str, price: str, source: str) -> dict[str, object]:
    return {
        "region_id": REGION,
        "product": PRODUCT,
        "business_date": DAY,
        "quantity_barrels": quantity,
        "avg_price_usd": price,
        "source": source,
    }


class ForecastServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("sales", "sales"),
            ("dispatch", "dispatcher"),
            ("risk", "risk"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def submit_two_channel_draft(self, day: str = DAY) -> dict[str, object]:
        self.service.submit_forecast_draft("sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "1200", "expected_price_usd": "100", "price_elasticity": "-0.5"},
        ], day))
        return self.service.submit_forecast_draft("plan", draft_payload([
            {"line_key": "wholesale", "quantity_barrels": "800", "expected_price_usd": "100", "price_elasticity": "0"},
        ], day))

    def approve(self, version_id: int, revision: int, effective_at: str | None = None) -> dict[str, object]:
        return self.service.approve_forecast("risk", version_id, revision, effective_at)

    def test_draft_merge_combines_lines_by_key(self) -> None:
        first = self.service.submit_forecast_draft("sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "1200", "expected_price_usd": "100", "price_elasticity": "-0.5"},
        ]))
        self.assertEqual(first["merge_action"], "opened")
        self.assertEqual(first["version_no"], 1)
        merged = self.service.submit_forecast_draft("plan", draft_payload([
            {"line_key": "wholesale", "quantity_barrels": "800"},
        ]))
        self.assertEqual(merged["merge_action"], "merged")
        self.assertEqual(merged["version_id"], first["version_id"])
        self.assertEqual(merged["revision"], 2)
        self.assertEqual([line["line_key"] for line in merged["lines"]], ["retail", "wholesale"])
        replaced = self.service.submit_forecast_draft("sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "1150"},
        ]))
        self.assertEqual(len(replaced["lines"]), 2)
        retail = next(line for line in replaced["lines"] if line["line_key"] == "retail")
        self.assertEqual(retail["quantity_barrels"], "1150.000")
        self.assertNotEqual(replaced["content_sha256"], merged["content_sha256"])

    def test_approval_freezes_and_rolls_replacement(self) -> None:
        merged = self.submit_two_channel_draft()
        approved = self.approve(merged["version_id"], merged["revision"])
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["approved_by"], "risk")
        self.assertEqual(approved["approved_at"], "2026-09-24T08:00:00Z")
        self.assertEqual(approved["effective_at"], "2026-09-24T08:00:00.000000Z")
        rolling = self.service.submit_forecast_draft("sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "1100", "expected_price_usd": "100", "price_elasticity": "-0.5"},
        ]))
        self.assertEqual(rolling["version_no"], 2)
        self.assertEqual(rolling["seeded_from_version_id"], approved["version_id"])
        self.assertEqual([line["line_key"] for line in rolling["lines"]], ["retail", "wholesale"])
        second = self.approve(rolling["version_id"], rolling["revision"])
        self.assertEqual(second["supersedes_version_id"], approved["version_id"])
        first_row = self.service.forecast_version("sales", approved["version_id"])
        self.assertEqual(first_row["state"], "superseded")
        timeline = self.service.forecast_timeline("dispatch", REGION, PRODUCT, DAY)
        self.assertEqual([item["state"] for item in timeline["versions"]], ["superseded", "approved"])
        self.assertEqual(timeline["versions"][1]["approved_by"], "risk")

    def test_approval_requires_role_and_current_revision(self) -> None:
        merged = self.submit_two_channel_draft()
        with self.assertRaises(Forbidden):
            self.service.approve_forecast("plan", merged["version_id"], merged["revision"])
        with self.assertRaises(InvalidState):
            self.approve(merged["version_id"], 99)
        self.approve(merged["version_id"], merged["revision"])
        with self.assertRaises(InvalidState):
            self.approve(merged["version_id"], merged["revision"])
        with self.assertRaises(NotFound):
            self.approve(9999, 1)

    def test_cutoff_resolves_version_effective_at_clock_time(self) -> None:
        first = self.approve(*self._approved_version())
        self.clock.advance(hours=2)
        rolling = self.service.submit_forecast_draft("sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "1100"},
        ]))
        self.approve(rolling["version_id"], rolling["revision"])
        self.clock.advance(hours=-1)
        cutoff = self.service.run_forecast_cutoff("dispatch", REGION, PRODUCT, DAY, "5000")
        self.assertEqual(cutoff["version_id"], first["version_id"])
        self.assertEqual(cutoff["resolved_at"], "2026-09-24T09:00:00.000000Z")
        self.assertFalse(cutoff["replayed"])

    def test_cutoff_replay_keeps_same_selection_after_replacement(self) -> None:
        first = self.approve(*self._approved_version())
        cutoff = self.service.run_forecast_cutoff("dispatch", REGION, PRODUCT, DAY, "5000")
        self.clock.advance(hours=3)
        rolling = self.service.submit_forecast_draft("sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "900"},
        ]))
        self.approve(rolling["version_id"], rolling["revision"])
        replayed = self.service.run_forecast_cutoff("dispatch", REGION, PRODUCT, DAY, "9999")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["cutoff_id"], cutoff["cutoff_id"])
        self.assertEqual(replayed["version_id"], first["version_id"])
        self.assertEqual(replayed["supply_cap_barrels"], "5000.000")

    def test_cutoff_summary_retained_after_rolling_replacement(self) -> None:
        first = self.approve(*self._approved_version())
        self.service.run_forecast_cutoff("dispatch", REGION, PRODUCT, DAY, "5000")
        rolling = self.service.submit_forecast_draft("sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "900"},
        ]))
        self.approve(rolling["version_id"], rolling["revision"])
        cutoff = self.service.forecast_cutoff("dispatch", REGION, PRODUCT, DAY)
        summary_version = cutoff["input_summary"]["version"]
        self.assertEqual(summary_version["version_id"], first["version_id"])
        retail = next(line for line in summary_version["lines"] if line["line_key"] == "retail")
        self.assertEqual(retail["quantity_barrels"], "1200.000")
        self.assertEqual(summary_version["approved_by"], "risk")
        self.assertEqual(summary_version["effective_at"], "2026-09-24T08:00:00.000000Z")
        self.assertEqual(self.service.forecast_version("sales", first["version_id"])["state"], "superseded")

    def test_cutoff_requires_effective_version_at_resolution_time(self) -> None:
        merged = self.submit_two_channel_draft()
        self.approve(merged["version_id"], merged["revision"], effective_at="2026-09-25T00:00:00Z")
        with self.assertRaises(InvalidState):
            self.service.run_forecast_cutoff("dispatch", REGION, PRODUCT, DAY, "5000")

    def _approved_version(self) -> tuple[int, int]:
        merged = self.submit_two_channel_draft()
        return merged["version_id"], merged["revision"]

    def test_variance_decomposition_closes_quantity(self) -> None:
        lines = [
            {"line_key": "retail", "quantity_barrels": "1200.000", "expected_price_usd": "100.00", "price_elasticity": "-0.5"},
            {"line_key": "wholesale", "quantity_barrels": "800.000", "expected_price_usd": "100.00", "price_elasticity": "0"},
        ]
        result = decompose_variance(
            forecast_lines=lines,
            actual_quantity=Decimal("1830"),
            actual_avg_price=Decimal("110"),
            supply_cap=Decimal("1900"),
        )
        self.assertEqual(result["deviation_barrels"], "-170.000")
        self.assertEqual(result["price_component_barrels"], "-60.000")
        self.assertEqual(result["supply_constrained_component_barrels"], "-40.000")
        self.assertEqual(result["unexplained_component_barrels"], "-70.000")
        self.assertTrue(result["quantity_closed"])
        ample = decompose_variance(
            forecast_lines=lines,
            actual_quantity=Decimal("1830"),
            actual_avg_price=Decimal("110"),
            supply_cap=Decimal("5000"),
        )
        self.assertEqual(ample["supply_constrained_component_barrels"], "0.000")
        self.assertEqual(ample["unexplained_component_barrels"], "-110.000")
        self.assertTrue(ample["quantity_closed"])

    def test_variance_flow_and_late_actual_generates_successor(self) -> None:
        self.approve(*self._approved_version())
        self.service.run_forecast_cutoff("dispatch", REGION, PRODUCT, DAY, "1900")
        self.service.record_forecast_actual("dispatch", actual_payload("1000", "110", "dn-1"))
        self.service.record_forecast_actual("dispatch", actual_payload("830", "110", "dn-2"))
        first = self.service.analyze_forecast_variance("dispatch", REGION, PRODUCT, DAY)
        self.assertEqual(first["analysis_seq"], 1)
        self.assertEqual(first["deviation_barrels"], "-170.000")
        self.assertEqual(first["price_component_barrels"], "-60.000")
        self.assertEqual(first["supply_constrained_component_barrels"], "-40.000")
        self.assertEqual(first["unexplained_component_barrels"], "-70.000")
        self.assertTrue(first["quantity_closed"])
        replayed = self.service.analyze_forecast_variance("dispatch", REGION, PRODUCT, DAY)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["analysis_id"], first["analysis_id"])
        late = self.service.record_forecast_actual("dispatch", actual_payload("50", "110", "dn-3"))
        successor = late["successor_analysis"]
        self.assertIsNotNone(successor)
        self.assertEqual(successor["analysis_seq"], 2)
        self.assertEqual(successor["supersedes_analysis_id"], first["analysis_id"])
        self.assertEqual(successor["deviation_barrels"], "-120.000")
        self.assertEqual(successor["unexplained_component_barrels"], "-20.000")
        self.assertTrue(successor["quantity_closed"])
        history = self.service.forecast_variance_history("audit", REGION, PRODUCT, DAY)
        self.assertEqual(len(history["analyses"]), 2)
        self.assertEqual(history["analyses"][0]["deviation_barrels"], "-170.000")
        self.assertEqual(history["analyses"][1]["analysis_seq"], 2)

    def test_actual_recording_is_idempotent_per_source(self) -> None:
        self.approve(*self._approved_version())
        self.service.run_forecast_cutoff("dispatch", REGION, PRODUCT, DAY, "5000")
        first = self.service.record_forecast_actual("dispatch", actual_payload("1000", "110", "dn-1"))
        replayed = self.service.record_forecast_actual("dispatch", actual_payload("1000", "110", "dn-1"))
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["actual_id"], first["actual_id"])
        self.assertIsNone(replayed["successor_analysis"])
        with self.assertRaises(Conflict):
            self.service.record_forecast_actual("dispatch", actual_payload("1001", "110", "dn-1"))
        count = self.connection.execute("SELECT count(*) FROM forecast_actuals").fetchone()[0]
        self.assertEqual(count, 1)

    def test_variance_requires_cutoff_and_actuals(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.analyze_forecast_variance("dispatch", REGION, PRODUCT, DAY)
        self.approve(*self._approved_version())
        self.service.run_forecast_cutoff("dispatch", REGION, PRODUCT, DAY, "5000")
        with self.assertRaises(InvalidState):
            self.service.analyze_forecast_variance("dispatch", REGION, PRODUCT, DAY)

    def test_compare_versions_traces_approver_and_effective_time(self) -> None:
        merged = self.submit_two_channel_draft()
        first = self.approve(merged["version_id"], merged["revision"])
        rolling = self.service.submit_forecast_draft("sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "1100"},
            {"line_key": "export", "quantity_barrels": "150"},
        ]))
        second = self.approve(rolling["version_id"], rolling["revision"])
        compared = self.service.compare_forecasts("dispatch", first["version_id"], second["version_id"])
        self.assertEqual(compared["total_a_barrels"], "2000.000")
        self.assertEqual(compared["total_b_barrels"], "2050.000")
        self.assertEqual(compared["total_delta_barrels"], "50.000")
        changes = {line["line_key"]: line["change"] for line in compared["lines"]}
        self.assertEqual(changes, {"export": "added", "retail": "decreased", "wholesale": "unchanged"})
        self.assertEqual(compared["a"]["approved_by"], "risk")
        self.assertEqual(compared["a"]["effective_at"], "2026-09-24T08:00:00.000000Z")
        self.assertEqual(compared["a"]["state"], "superseded")
        self.assertEqual(compared["b"]["state"], "approved")
        with self.assertRaises(NotFound):
            self.service.compare_forecasts("sales", first["version_id"], 9999)

    def test_forecast_permissions(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.submit_forecast_draft("dispatch", draft_payload([{"line_key": "retail", "quantity_barrels": "1"}]))
        merged = self.submit_two_channel_draft()
        with self.assertRaises(Forbidden):
            self.service.run_forecast_cutoff("sales", REGION, PRODUCT, DAY, "100")
        with self.assertRaises(Forbidden):
            self.service.record_forecast_actual("sales", actual_payload("1", "1", "dn-x"))
        self.assertEqual(self.service.forecast_version("audit", merged["version_id"])["version_no"], 1)


class ForecastApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        self.app = JsonApplication(self.service)
        for user_id, role in (("sales", "sales"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, actor: str, payload: dict[str, object]):
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode())

    def get(self, path: str, actor: str):
        return self.app.handle("GET", path, {"X-Actor-Id": actor})

    def test_forecast_endpoints(self) -> None:
        draft = self.post("/forecasts/drafts", "sales", draft_payload([
            {"line_key": "retail", "quantity_barrels": "1200", "expected_price_usd": "100", "price_elasticity": "-0.5"},
        ]))
        self.assertEqual(draft.status, 201)
        version_id = draft.body["version_id"]
        approved = self.post(f"/forecasts/versions/{version_id}/approve", "risk", {"expected_revision": 1})
        self.assertEqual(approved.status, 200)
        self.assertEqual(approved.body["state"], "approved")
        denied = self.post("/forecasts/drafts", "dispatch", draft_payload([{"line_key": "x", "quantity_barrels": "1"}]))
        self.assertEqual(denied.status, 403)
        cutoff = self.post("/forecasts/cutoffs", "dispatch", {"region_id": REGION, "product": PRODUCT, "business_date": DAY, "supply_cap_barrels": "1900"})
        self.assertEqual(cutoff.status, 201)
        self.assertEqual(cutoff.body["version_no"], 1)
        fetched = self.get(f"/forecasts/cutoffs?region_id={REGION}&product={PRODUCT}&business_date={DAY}", "sales")
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["input_sha256"], cutoff.body["input_sha256"])
        actual = self.post("/forecasts/actuals", "dispatch", actual_payload("1830", "110", "dn-1"))
        self.assertEqual(actual.status, 201)
        variance = self.post("/forecasts/variance", "dispatch", {"region_id": REGION, "product": PRODUCT, "business_date": DAY})
        self.assertEqual(variance.status, 200)
        self.assertTrue(variance.body["quantity_closed"])
        history = self.get(f"/forecasts/variance?region_id={REGION}&product={PRODUCT}&business_date={DAY}", "audit")
        self.assertEqual(len(history.body["analyses"]), 1)
        compared = self.get(f"/forecasts/compare?a={version_id}&b={version_id}", "sales")
        self.assertEqual(compared.status, 200)
        self.assertEqual(compared.body["total_delta_barrels"], "0.000")
        timeline = self.get(f"/forecasts/timeline?region_id={REGION}&product={PRODUCT}&business_date={DAY}", "dispatch")
        self.assertEqual(timeline.body["versions"][0]["approved_by"], "risk")
        detail = self.get(f"/forecasts/versions/{version_id}", "audit")
        self.assertEqual(detail.body["effective_at"], "2026-09-24T08:00:00.000000Z")


class ForecastCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tempdir.name) / "cli.sqlite3")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, dict[str, object]]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(list(args))
        output = stdout.getvalue().strip()
        errors = stderr.getvalue().strip()
        return code, json.loads(output or errors or "{}")

    def cli(self, *args: str) -> tuple[int, dict[str, object]]:
        return self.run_cli("--database", self.database, *args)

    def test_cli_forecast_flow(self) -> None:
        for user_id, role in (("sales", "sales"), ("risk", "risk"), ("dispatch", "dispatcher")):
            code, _ = self.cli("--actor", "ops", "user-create", "--user-id", user_id, "--display-name", user_id, "--role", role)
            self.assertEqual(code, 0)
        draft = json.dumps(draft_payload([
            {"line_key": "retail", "quantity_barrels": "1200", "expected_price_usd": "100", "price_elasticity": "-0.5"},
        ]))
        code, body = self.cli("--actor", "sales", "forecast-draft", "--data", draft)
        self.assertEqual(code, 0)
        self.assertEqual(body["version_no"], 1)
        code, body = self.cli("--actor", "risk", "forecast-approve", "--version-id", "1", "--expected-revision", "1")
        self.assertEqual(code, 0)
        self.assertEqual(body["state"], "approved")
        changed = json.dumps(draft_payload([
            {"line_key": "retail", "quantity_barrels": "1100", "expected_price_usd": "100", "price_elasticity": "-0.5"},
        ]))
        code, body = self.cli("--actor", "sales", "forecast-draft", "--data", changed)
        self.assertEqual(body["version_no"], 2)
        code, body = self.cli("--actor", "risk", "forecast-approve", "--version-id", "2", "--expected-revision", "1")
        self.assertEqual(code, 0)
        code, body = self.cli("--actor", "sales", "forecast-compare", "--a", "1", "--b", "2")
        self.assertEqual(code, 0)
        self.assertEqual(body["total_delta_barrels"], "-100.000")
        self.assertEqual(body["a"]["approved_by"], "risk")
        code, body = self.cli(
            "--actor", "dispatch", "cutoff-run",
            "--region", REGION, "--product", PRODUCT, "--business-date", DAY,
            "--supply-cap", "5000", "--at", "2099-01-01T00:00:00Z",
        )
        self.assertEqual(code, 0)
        self.assertEqual(body["version_no"], 2)
        self.assertEqual(body["resolved_at"], "2099-01-01T00:00:00.000000Z")
        code, replayed = self.cli(
            "--actor", "dispatch", "cutoff-run",
            "--region", REGION, "--product", PRODUCT, "--business-date", DAY,
            "--supply-cap", "5000", "--at", "2099-01-01T00:00:00Z",
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["cutoff_id"], body["cutoff_id"])
        actual = json.dumps(actual_payload("1050", "110", "dn-1"))
        code, _ = self.cli("--actor", "dispatch", "actual-record", "--data", actual)
        self.assertEqual(code, 0)
        code, variance = self.cli("--actor", "dispatch", "variance-analyze", "--region", REGION, "--product", PRODUCT, "--business-date", DAY)
        self.assertEqual(code, 0)
        self.assertTrue(variance["quantity_closed"])
        self.assertEqual(variance["deviation_barrels"], "-50.000")
        self.assertEqual(variance["price_component_barrels"], "-55.000")
        self.assertEqual(variance["supply_constrained_component_barrels"], "0.000")
        self.assertEqual(variance["unexplained_component_barrels"], "5.000")
        code, history = self.cli("--actor", "dispatch", "variance-list", "--region", REGION, "--product", PRODUCT, "--business-date", DAY)
        self.assertEqual(len(history["analyses"]), 1)
        code, timeline = self.cli("--actor", "sales", "forecast-timeline", "--region", REGION, "--product", PRODUCT, "--business-date", DAY)
        self.assertEqual([item["state"] for item in timeline["versions"]], ["superseded", "approved"])
        code, error = self.cli("--actor", "risk", "forecast-approve", "--version-id", "2", "--expected-revision", "1")
        self.assertEqual(code, 1)
        self.assertEqual(error["error"]["code"], "invalid_state")


class ForecastAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance_includes_forecast_governance(self) -> None:
        result = acceptance.run(Path.cwd())
        self.assertEqual(result["status"], "ok")
        forecast = result["forecast"]
        self.assertEqual(forecast["resolved_version_no"], 2)
        self.assertTrue(forecast["quantity_closed"])
        self.assertEqual(forecast["successor_analysis_seq"], 2)
        self.assertEqual(forecast["version_delta_barrels"], "-100.000")
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
