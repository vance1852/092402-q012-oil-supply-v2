"""贯通报价、线路、库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"price_index": "BRENT", "trade_date": f"2026-09-{index}", "close_usd": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    service.submit_forecast("plan", {"region": "north", "product": "crude", "business_day": "2026-09-25", "quantity_barrels": "82000", "price_assumption_usd": "96", "price_elasticity": "-0.3", "note": "初版需求"})
    merged = service.submit_forecast("plan", {"region": "north", "product": "crude", "business_day": "2026-09-25", "quantity_barrels": "80000", "price_assumption_usd": "96", "price_elasticity": "-0.3", "note": "销售调低"})
    service.approve_forecast("risk", merged["version_id"])
    cutoff = service.run_cutoff("dispatch", "north", "crude", "2026-09-25")
    cutoff_replay = service.run_cutoff("dispatch", "north", "crude", "2026-09-25")
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25", forecast_version_id=cutoff["resolved_version_id"])
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键管道恢复与需求回落", "price_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    service.record_actual("dispatch", {"region": "north", "product": "crude", "business_day": "2026-09-25", "delivered_barrels": "79000", "price_usd": "97", "source": "terminal-wms", "idempotency_key": "act-001"})
    service.record_actual("dispatch", {"region": "north", "product": "crude", "business_day": "2026-09-25", "delivered_barrels": "500", "price_usd": "97.5", "source": "terminal-wms", "idempotency_key": "act-002"})
    close = service.close_forecast_day("dispatch", "north", "crude", "2026-09-25", "79500")
    late = service.record_actual("dispatch", {"region": "north", "product": "crude", "business_day": "2026-09-25", "delivered_barrels": "300", "price_usd": "98", "source": "terminal-wms-late", "idempotency_key": "act-003"})
    second = service.submit_forecast("plan", {"region": "north", "product": "crude", "business_day": "2026-09-25", "quantity_barrels": "78000", "price_assumption_usd": "95.5", "price_elasticity": "-0.3", "note": "滚动修正"})
    service.approve_forecast("risk", second["version_id"])
    comparison = service.compare_forecasts("plan", merged["version_id"], second["version_id"])
    analyses = service.list_analyses("audit", "north", "crude", "2026-09-25")
    forecast_result = {"cutoff_version_id": cutoff["resolved_version_id"], "cutoff_replayed": cutoff_replay["replayed"], "allocation_forecast_version_id": allocation["forecast_version_id"], "close_analysis_id": close["analysis_id"], "close_quantity_closed": close["quantity_closed"], "successor_analysis_id": late["successor_analysis_id"], "compare_quantity_delta": comparison["quantity_delta"], "analyses": len(analyses["analyses"])}
    result = {"status": "ok", "price": service.price_summary("BRENT"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "forecast": forecast_result, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行油气供应服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
