"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    price_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_usd TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES price_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(price_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON price_index_quotes(price_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_barrels TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_barrels TEXT NOT NULL,
    available_barrels TEXT NOT NULL,
    unit_cost_usd TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_barrels TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_barrels TEXT NOT NULL,
    allocated_barrels TEXT NOT NULL DEFAULT '0',
    delivered_barrels TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    loaded_barrels TEXT NOT NULL,
    expected_delivered_barrels TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS forecast_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region TEXT NOT NULL,
    product TEXT NOT NULL,
    business_day TEXT NOT NULL,
    quantity_barrels TEXT NOT NULL,
    price_assumption_usd TEXT NOT NULL,
    price_elasticity TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','superseded','withdrawn')),
    revision INTEGER NOT NULL DEFAULT 1,
    content_sha256 TEXT NOT NULL,
    supersedes_version_id INTEGER REFERENCES forecast_versions(version_id),
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    approved_by TEXT REFERENCES supply_users(user_id),
    approved_at TEXT,
    effective_from TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_forecast_single_draft
ON forecast_versions(region, product, business_day) WHERE state='draft';

CREATE UNIQUE INDEX IF NOT EXISTS idx_forecast_single_approved
ON forecast_versions(region, product, business_day) WHERE state='approved';

CREATE INDEX IF NOT EXISTS idx_forecast_key
ON forecast_versions(region, product, business_day, version_id);

CREATE TABLE IF NOT EXISTS forecast_cutoffs (
    cutoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region TEXT NOT NULL,
    product TEXT NOT NULL,
    business_day TEXT NOT NULL,
    resolved_version_id INTEGER NOT NULL REFERENCES forecast_versions(version_id),
    resolved_at TEXT NOT NULL,
    version_summary_json TEXT NOT NULL,
    summary_sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    UNIQUE(region, product, business_day)
);

CREATE TABLE IF NOT EXISTS allocation_forecast_links (
    allocation_id INTEGER NOT NULL REFERENCES allocation_runs(allocation_id),
    forecast_version_id INTEGER NOT NULL REFERENCES forecast_versions(version_id),
    version_summary_json TEXT NOT NULL,
    summary_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(allocation_id, forecast_version_id)
);

CREATE TABLE IF NOT EXISTS forecast_actuals (
    actual_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region TEXT NOT NULL,
    product TEXT NOT NULL,
    business_day TEXT NOT NULL,
    delivered_barrels TEXT NOT NULL,
    price_usd TEXT NOT NULL,
    source TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actuals_key
ON forecast_actuals(region, product, business_day, actual_id);

CREATE TABLE IF NOT EXISTS deviation_analyses (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region TEXT NOT NULL,
    product TEXT NOT NULL,
    business_day TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    forecast_version_id INTEGER NOT NULL REFERENCES forecast_versions(version_id),
    version_source TEXT NOT NULL CHECK(version_source IN ('cutoff','resolved_at_close')),
    forecast_quantity TEXT NOT NULL,
    actual_quantity TEXT NOT NULL,
    forecast_price TEXT NOT NULL,
    actual_price TEXT NOT NULL,
    available_supply TEXT NOT NULL,
    price_effect TEXT NOT NULL,
    supply_effect TEXT NOT NULL,
    unexplained TEXT NOT NULL,
    total_deviation TEXT NOT NULL,
    quantity_closed INTEGER NOT NULL CHECK(quantity_closed IN (0,1)),
    trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('initial_close','late_actual')),
    supersedes_analysis_id INTEGER REFERENCES deviation_analyses(analysis_id),
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(region, product, business_day, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_single_close
ON deviation_analyses(region, product, business_day) WHERE trigger_kind='initial_close';

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
