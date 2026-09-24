"""确定性的价格、能力与库存计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
HUNDRED = Decimal("100")
BASIS_POINTS = Decimal("10000")


def quantize_volume(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PricePoint:
    trade_date: str
    close: Decimal


@dataclass(frozen=True, slots=True)
class Streak:
    direction: str
    sessions: int
    start_date: str
    end_date: str
    start_close: Decimal
    end_close: Decimal
    percent_change: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "sessions": self.sessions,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "start_close": decimal_text(self.start_close),
            "end_close": decimal_text(self.end_close),
            "percent_change": decimal_text(self.percent_change),
        }


def latest_streak(points: Sequence[PricePoint]) -> Streak | None:
    ordered = sorted(points, key=lambda item: item.trade_date)
    if len(ordered) < 2:
        return None
    last = ordered[-1]
    previous = ordered[-2]
    if last.close == previous.close:
        return Streak("flat", 1, last.trade_date, last.trade_date, last.close, last.close, ZERO)
    direction = "down" if last.close < previous.close else "up"
    start_index = len(ordered) - 2
    while start_index > 0:
        left = ordered[start_index - 1]
        right = ordered[start_index]
        matches = right.close < left.close if direction == "down" else right.close > left.close
        if not matches:
            break
        start_index -= 1
    start = ordered[start_index]
    change = (last.close - start.close) / start.close * HUNDRED
    return Streak(
        direction=direction,
        sessions=len(ordered) - start_index,
        start_date=start.trade_date,
        end_date=last.trade_date,
        start_close=start.close,
        end_close=last.close,
        percent_change=change.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
    )


def moving_average(points: Sequence[PricePoint], sessions: int) -> Decimal | None:
    if sessions <= 0:
        raise ValueError("sessions 必须大于零")
    ordered = sorted(points, key=lambda item: item.trade_date)
    if len(ordered) < sessions:
        return None
    values = [item.close for item in ordered[-sessions:]]
    return quantize_money(sum(values, ZERO) / Decimal(len(values)))


def effective_capacity(
    nominal: Decimal,
    capacity_percentages: Iterable[Decimal],
) -> Decimal:
    result = nominal
    for percentage in capacity_percentages:
        bounded = max(ZERO, min(HUNDRED, percentage))
        result *= bounded / HUNDRED
    return quantize_volume(result)


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    nomination_id: str
    requested: Decimal
    priority: int
    submitted_at: str


def allocate_capacity(
    available: Decimal,
    requests: Iterable[AllocationRequest],
) -> list[dict[str, str]]:
    if available < ZERO:
        raise ValueError("可用能力不能为负数")
    remaining = quantize_volume(available)
    result: list[dict[str, str]] = []
    ordered = sorted(requests, key=lambda item: (item.priority, item.submitted_at, item.nomination_id))
    for request in ordered:
        allocated = min(remaining, request.requested)
        allocated = quantize_volume(max(ZERO, allocated))
        remaining = quantize_volume(remaining - allocated)
        result.append({
            "nomination_id": request.nomination_id,
            "requested_barrels": decimal_text(request.requested),
            "allocated_barrels": decimal_text(allocated),
            "unfilled_barrels": decimal_text(quantize_volume(request.requested - allocated)),
        })
    return result


def delivered_after_loss(loaded: Decimal, loss_basis_points: int) -> Decimal:
    if not 0 <= loss_basis_points <= 1000:
        raise ValueError("损耗基点超出范围")
    retained = Decimal(1) - Decimal(loss_basis_points) / BASIS_POINTS
    return quantize_volume(loaded * retained)


def weighted_inventory_cost(lots: Iterable[Mapping[str, object]]) -> dict[str, str]:
    quantity = ZERO
    value = ZERO
    for lot in lots:
        available = Decimal(str(lot["available_barrels"]))
        unit_cost = Decimal(str(lot["unit_cost_usd"]))
        if available < ZERO or unit_cost < ZERO:
            raise ValueError("库存数量和成本不能为负数")
        quantity += available
        value += available * unit_cost
    average = ZERO if quantity == ZERO else value / quantity
    return {
        "available_barrels": decimal_text(quantize_volume(quantity)),
        "inventory_value_usd": decimal_text(quantize_money(value)),
        "weighted_unit_cost_usd": decimal_text(quantize_money(average)),
    }


def reconcile_inventory(
    book_quantity: Decimal,
    measured_quantity: Decimal,
    tolerance_percent: Decimal,
) -> dict[str, object]:
    if book_quantity < ZERO or measured_quantity < ZERO:
        raise ValueError("库存数量不能为负数")
    if tolerance_percent < ZERO:
        raise ValueError("容差不能为负数")
    delta = quantize_volume(measured_quantity - book_quantity)
    ratio = ZERO if book_quantity == ZERO else abs(delta) / book_quantity * HUNDRED
    return {
        "book_quantity": decimal_text(quantize_volume(book_quantity)),
        "measured_quantity": decimal_text(quantize_volume(measured_quantity)),
        "delta_barrels": decimal_text(delta),
        "variance_percent": decimal_text(ratio.quantize(Decimal("0.0001"))),
        "within_tolerance": ratio <= tolerance_percent,
    }


def merge_forecast_lines(
    existing: Iterable[Mapping[str, object]],
    submitted: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """按 line_key 确定性合并草稿行，后提交的行覆盖同键行，输出按 line_key 排序。"""
    merged: dict[str, dict[str, object]] = {}
    for line in existing:
        merged[str(line["line_key"])] = dict(line)
    for line in submitted:
        merged[str(line["line_key"])] = dict(line)
    return [merged[key] for key in sorted(merged)]


def compare_forecast_lines(
    lines_a: Iterable[Mapping[str, object]],
    lines_b: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """逐行比较两个版本的预测行，输出每行增减和总量差。"""
    by_a = {str(line["line_key"]): line for line in lines_a}
    by_b = {str(line["line_key"]): line for line in lines_b}
    rows: list[dict[str, object]] = []
    total_a = ZERO
    total_b = ZERO
    for key in sorted(set(by_a) | set(by_b)):
        line_a = by_a.get(key)
        line_b = by_b.get(key)
        quantity_a = ZERO if line_a is None else Decimal(str(line_a["quantity_barrels"]))
        quantity_b = ZERO if line_b is None else Decimal(str(line_b["quantity_barrels"]))
        total_a += quantity_a
        total_b += quantity_b
        if line_a is None:
            change = "added"
        elif line_b is None:
            change = "removed"
        elif quantity_b > quantity_a:
            change = "increased"
        elif quantity_b < quantity_a:
            change = "decreased"
        else:
            change = "unchanged"
        rows.append({
            "line_key": key,
            "a_quantity_barrels": None if line_a is None else decimal_text(quantize_volume(quantity_a)),
            "b_quantity_barrels": None if line_b is None else decimal_text(quantize_volume(quantity_b)),
            "delta_barrels": decimal_text(quantize_volume(quantity_b - quantity_a)),
            "change": change,
        })
    return {
        "lines": rows,
        "total_a_barrels": decimal_text(quantize_volume(total_a)),
        "total_b_barrels": decimal_text(quantize_volume(total_b)),
        "total_delta_barrels": decimal_text(quantize_volume(total_b - total_a)),
    }


def decompose_variance(
    *,
    forecast_lines: Iterable[Mapping[str, object]],
    actual_quantity: Decimal,
    actual_avg_price: Decimal,
    supply_cap: Decimal,
) -> dict[str, object]:
    """把预测与实绩的偏差在数量闭合下分解为价格变化、供应受限和未解释三部分。

    价格分量按各预测行的价格弹性计算；供应受限分量是供应上限相对价格调整后
    需求的缺口（只可能小于等于零）；未解释分量是闭合残差，保证三项之和严格
    等于总偏差。
    """
    if actual_quantity < ZERO or supply_cap < ZERO or actual_avg_price <= ZERO:
        raise ValueError("实绩数量和供应上限不能为负，实绩均价必须为正")
    forecast_total = ZERO
    price_component = ZERO
    for line in forecast_lines:
        quantity = Decimal(str(line["quantity_barrels"]))
        forecast_total += quantity
        expected = line.get("expected_price_usd")
        elasticity = Decimal(str(line.get("price_elasticity") or "0"))
        if expected is None:
            continue
        expected_price = Decimal(str(expected))
        if expected_price <= ZERO:
            raise ValueError("预测行期望价格必须为正数")
        price_component += quantity * elasticity * (actual_avg_price - expected_price) / expected_price
    expected_unconstrained = max(ZERO, forecast_total + price_component)
    supply_component = min(expected_unconstrained, supply_cap) - expected_unconstrained
    deviation = actual_quantity - forecast_total
    deviation_q = quantize_volume(deviation)
    price_q = quantize_volume(price_component)
    supply_q = quantize_volume(supply_component)
    unexplained_q = deviation_q - price_q - supply_q
    return {
        "forecast_quantity_barrels": decimal_text(quantize_volume(forecast_total)),
        "actual_quantity_barrels": decimal_text(quantize_volume(actual_quantity)),
        "actual_avg_price_usd": decimal_text(quantize_money(actual_avg_price)),
        "supply_cap_barrels": decimal_text(quantize_volume(supply_cap)),
        "deviation_barrels": decimal_text(deviation_q),
        "price_component_barrels": decimal_text(price_q),
        "supply_constrained_component_barrels": decimal_text(supply_q),
        "unexplained_component_barrels": decimal_text(unexplained_q),
        "quantity_closed": deviation_q == price_q + supply_q + unexplained_q,
    }


def scenario_projection(    *,
    current_price: Decimal,
    price_index_drop_percent: Decimal,
    routes: Iterable[Mapping[str, object]],
    inventory: Iterable[Mapping[str, object]],
    route_capacity_changes: Mapping[str, Decimal],
    demand_changes: Mapping[str, Decimal],
) -> dict[str, object]:
    projected_price = current_price * (Decimal(1) - price_index_drop_percent / HUNDRED)
    route_rows: list[dict[str, str]] = []
    total_capacity = ZERO
    for route in sorted(routes, key=lambda item: str(item["route_id"])):
        route_id = str(route["route_id"])
        nominal = Decimal(str(route["daily_capacity"]))
        change = route_capacity_changes.get(route_id, ZERO)
        projected = max(ZERO, nominal * (Decimal(1) + change / HUNDRED))
        total_capacity += projected
        route_rows.append({
            "route_id": route_id,
            "base_capacity": decimal_text(quantize_volume(nominal)),
            "change_percent": decimal_text(change),
            "projected_capacity": decimal_text(quantize_volume(projected)),
        })
    inventory_rows: list[dict[str, str]] = []
    total_inventory = ZERO
    for row in sorted(inventory, key=lambda item: (str(item["facility_id"]), str(item["product"]))):
        key = f"{row['facility_id']}:{row['product']}"
        available = Decimal(str(row["available_barrels"]))
        demand_change = demand_changes.get(key, ZERO)
        days_factor = max(Decimal("0.01"), Decimal(1) + demand_change / HUNDRED)
        adjusted = available / days_factor
        total_inventory += adjusted
        inventory_rows.append({
            "inventory_key": key,
            "base_available": decimal_text(quantize_volume(available)),
            "demand_change_percent": decimal_text(demand_change),
            "demand_adjusted_inventory": decimal_text(quantize_volume(adjusted)),
        })
    return {
        "projected_price_index_usd": decimal_text(quantize_money(projected_price)),
        "total_projected_capacity": decimal_text(quantize_volume(total_capacity)),
        "demand_adjusted_inventory": decimal_text(quantize_volume(total_inventory)),
        "routes": route_rows,
        "inventory": inventory_rows,
    }
