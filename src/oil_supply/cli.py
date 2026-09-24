"""预测版本、截单与偏差分析的命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import SupplyError
from .service import SupplyService
from .storage import connect


def _key_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--region", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--business-day", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oil-supply", description="油气供应预测版本、截单与偏差分析命令行")
    parser.add_argument("--database", type=Path, default=Path("oil_supply.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)

    user = commands.add_parser("user-create", help="创建操作者")
    user.add_argument("--user-id", required=True)
    user.add_argument("--display-name", required=True)
    user.add_argument("--role", required=True, choices=["planner", "dispatcher", "risk", "auditor"])

    submit = commands.add_parser("forecast-submit", help="提交预测草稿，同键草稿自动合并")
    submit.add_argument("--actor", required=True)
    _key_arguments(submit)
    submit.add_argument("--quantity", required=True)
    submit.add_argument("--price", required=True)
    submit.add_argument("--elasticity", default="0")
    submit.add_argument("--note", default="")

    approve = commands.add_parser("forecast-approve", help="批准并冻结预测版本，旧版滚动替代")
    approve.add_argument("--actor", required=True)
    approve.add_argument("--version-id", type=int, required=True)
    approve.add_argument("--effective-from", default=None)

    withdraw = commands.add_parser("forecast-withdraw", help="撤回未批准的草稿版本")
    withdraw.add_argument("--actor", required=True)
    withdraw.add_argument("--version-id", type=int, required=True)

    listing = commands.add_parser("forecast-list", help="列出预测版本及批准人、生效时间")
    listing.add_argument("--actor", required=True)
    listing.add_argument("--region")
    listing.add_argument("--product")
    listing.add_argument("--business-day")

    compare = commands.add_parser("forecast-compare", help="比较任意两个预测版本")
    compare.add_argument("--actor", required=True)
    compare.add_argument("--a", type=int, required=True)
    compare.add_argument("--b", type=int, required=True)

    cutoff = commands.add_parser("forecast-cutoff", help="按当前时钟解析截单有效版本，重复执行得到同一选择")
    cutoff.add_argument("--actor", required=True)
    _key_arguments(cutoff)

    cutoff_show = commands.add_parser("forecast-cutoff-show", help="查看已冻结的截单解析结果")
    cutoff_show.add_argument("--actor", required=True)
    _key_arguments(cutoff_show)

    actual = commands.add_parser("forecast-actual", help="登记实绩，迟到实绩只生成后继分析")
    actual.add_argument("--actor", required=True)
    _key_arguments(actual)
    actual.add_argument("--delivered", required=True)
    actual.add_argument("--price", required=True)
    actual.add_argument("--source", required=True)
    actual.add_argument("--idempotency-key", required=True)

    close = commands.add_parser("forecast-close", help="实绩到齐后结账并做数量闭合偏差分解")
    close.add_argument("--actor", required=True)
    _key_arguments(close)
    close.add_argument("--available-supply", required=True)

    analyses = commands.add_parser("forecast-analyses", help="查看偏差分解及迟到实绩后继分析")
    analyses.add_argument("--actor", required=True)
    _key_arguments(analyses)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    connection = connect(args.database)
    try:
        service = SupplyService(connection)
        if args.command == "user-create":
            return service.create_user(args.user_id, args.display_name, args.role)
        if args.command == "forecast-submit":
            return service.submit_forecast(args.actor, {
                "region": args.region,
                "product": args.product,
                "business_day": args.business_day,
                "quantity_barrels": args.quantity,
                "price_assumption_usd": args.price,
                "price_elasticity": args.elasticity,
                "note": args.note,
            })
        if args.command == "forecast-approve":
            return service.approve_forecast(args.actor, args.version_id, args.effective_from)
        if args.command == "forecast-withdraw":
            return service.withdraw_forecast(args.actor, args.version_id)
        if args.command == "forecast-list":
            return service.list_forecasts(args.actor, args.region, args.product, args.business_day)
        if args.command == "forecast-compare":
            return service.compare_forecasts(args.actor, args.a, args.b)
        if args.command == "forecast-cutoff":
            return service.run_cutoff(args.actor, args.region, args.product, args.business_day)
        if args.command == "forecast-cutoff-show":
            return service.get_cutoff(args.actor, args.region, args.product, args.business_day)
        if args.command == "forecast-actual":
            return service.record_actual(args.actor, {
                "region": args.region,
                "product": args.product,
                "business_day": args.business_day,
                "delivered_barrels": args.delivered,
                "price_usd": args.price,
                "source": args.source,
                "idempotency_key": args.idempotency_key,
            })
        if args.command == "forecast-close":
            return service.close_forecast_day(
                args.actor, args.region, args.product, args.business_day, args.available_supply
            )
        if args.command == "forecast-analyses":
            return service.list_analyses(args.actor, args.region, args.product, args.business_day)
        raise SupplyError(f"未知命令 {args.command}")
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except SupplyError as exc:
        print(
            json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
