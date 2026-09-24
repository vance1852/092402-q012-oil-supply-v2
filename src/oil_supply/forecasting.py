"""预测版本解析、实绩汇总与数量闭合偏差分解的确定性计算。"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping

from .clock import parse_utc
from .planning import decimal_text, quantize_money, quantize_volume


ZERO = Decimal("0")


def resolve_effective_version(
    versions: Iterable[Mapping[str, object]],
    at: str,
) -> Mapping[str, object] | None:
    """返回 at 时刻生效的预测版本。

    只考虑曾经批准的版本（approved 或 superseded），取 effective_from 不晚于 at
    且生效时间最晚者；生效时间相同取 version_id 较大者，保证解析结果确定。
    """
    at_dt = parse_utc(at, "at")
    candidates: list[Mapping[str, object]] = []
    for version in versions:
        if version["state"] not in ("approved", "superseded"):
            continue
        effective_from = version["effective_from"]
        if not effective_from:
            continue
        if parse_utc(str(effective_from), "effective_from") <= at_dt:
            candidates.append(version)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            parse_utc(str(item["effective_from"]), "effective_from"),
            int(item["version_id"]),
        ),
    )


def weighted_average_price(actuals: Iterable[Mapping[str, object]]) -> Decimal:
    total = ZERO
    value = ZERO
    for actual in actuals:
        quantity = Decimal(str(actual["delivered_barrels"]))
        price = Decimal(str(actual["price_usd"]))
        if quantity <= ZERO or price <= ZERO:
            raise ValueError("实绩数量和价格必须为正数")
        total += quantity
        value += quantity * price
    if total == ZERO:
        raise ValueError("实绩总量不能为零")
    return quantize_money(value / total)


def decompose_deviation(
    *,
    forecast_quantity: Decimal,
    actual_quantity: Decimal,
    forecast_price: Decimal,
    actual_price: Decimal,
    price_elasticity: Decimal,
    available_supply: Decimal,
) -> dict[str, object]:
    """把 实绩-预测 的总偏差按瀑布顺序分解为价格变化、供应受限和未解释三部分。

    数量闭合：price_effect + supply_effect + unexplained == total_deviation，
    未解释部分作为残差吸收量化尾差，闭合恒成立。
    """
    if min(forecast_quantity, actual_quantity, available_supply) < ZERO:
        raise ValueError("数量不能为负数")
    if forecast_price <= ZERO or actual_price <= ZERO:
        raise ValueError("价格必须为正数")
    total = quantize_volume(actual_quantity - forecast_quantity)
    relative_change = (actual_price - forecast_price) / forecast_price
    price_effect = quantize_volume(forecast_quantity * price_elasticity * relative_change)
    expected_after_price = forecast_quantity + price_effect
    supply_effect = quantize_volume(min(ZERO, available_supply - expected_after_price))
    unexplained = total - price_effect - supply_effect
    return {
        "forecast_quantity": decimal_text(quantize_volume(forecast_quantity)),
        "actual_quantity": decimal_text(quantize_volume(actual_quantity)),
        "total_deviation": decimal_text(total),
        "price_effect": decimal_text(price_effect),
        "supply_effect": decimal_text(supply_effect),
        "unexplained": decimal_text(unexplained),
        "expected_after_price": decimal_text(quantize_volume(expected_after_price)),
        "quantity_closed": price_effect + supply_effect + unexplained == total,
    }


def diff_versions(
    first: Mapping[str, object],
    second: Mapping[str, object],
) -> dict[str, object]:
    """比较两个预测版本的业务字段，输出变化清单和数量、价格差值。"""
    tracked = (
        "region",
        "product",
        "business_day",
        "quantity_barrels",
        "price_assumption_usd",
        "price_elasticity",
        "note",
        "state",
    )
    changes: dict[str, dict[str, str]] = {}
    for field in tracked:
        left = str(first[field])
        right = str(second[field])
        if left != right:
            changes[field] = {"from": left, "to": right}
    quantity_delta = quantize_volume(
        Decimal(str(second["quantity_barrels"])) - Decimal(str(first["quantity_barrels"]))
    )
    price_delta = quantize_money(
        Decimal(str(second["price_assumption_usd"])) - Decimal(str(first["price_assumption_usd"]))
    )
    return {
        "changes": changes,
        "quantity_delta": decimal_text(quantity_delta),
        "price_delta": decimal_text(price_delta),
    }
