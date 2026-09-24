"""预测版本治理与偏差分解的命令行入口。

销售和调度无需翻查旧日志即可比较任意两版预测、追溯批准人和生效时间；
截单任务支持用 --at 注入时钟，重复执行同一键得到同一选择。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .clock import FrozenClock, SystemClock, parse_utc
from .errors import SupplyError
from .planning import canonical_json
from .service import SupplyService
from .storage import connect


def _json_payload(value: str) -> dict[str, Any]:
    if value == "-":
        text = sys.stdin.read()
    elif value.startswith("@"):
        text = Path(value[1:]).read_text(encoding="utf-8")
    else:
        text = value
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("请求数据必须是 JSON 对象")
    return parsed


def _key_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--region", required=True, help="区域编号")
    parser.add_argument("--product", required=True, help="油品，如 gasoline-92")
    parser.add_argument("--business-date", required=True, help="营业日 YYYY-MM-DD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oil-supply", description="油气供应预测版本治理与偏差分解命令行")
    parser.add_argument("--database", type=Path, required=True, help="SQLite 数据库路径")
    parser.add_argument("--actor", default="cli", help="操作者编号，默认 cli")
    sub = parser.add_subparsers(dest="command", required=True)

    user = sub.add_parser("user-create", help="创建操作者")
    user.add_argument("--user-id", required=True)
    user.add_argument("--display-name", required=True)
    user.add_argument("--role", required=True, choices=["planner", "sales", "dispatcher", "risk", "auditor"])

    draft = sub.add_parser("forecast-draft", help="提交并合并预测草稿行")
    draft.add_argument("--data", required=True, help="JSON 文本、@文件路径，或 - 表示标准输入")

    approve = sub.add_parser("forecast-approve", help="审批并冻结预测版本")
    approve.add_argument("--version-id", type=int, required=True)
    approve.add_argument("--expected-revision", type=int, required=True)
    approve.add_argument("--effective-at", default=None, help="生效时间，默认审批时刻")

    show = sub.add_parser("forecast-show", help="查看单个预测版本")
    show.add_argument("--version-id", type=int, required=True)

    timeline = sub.add_parser("forecast-timeline", help="列出某键全部版本及批准人、生效时间")
    _key_arguments(timeline)

    compare = sub.add_parser("forecast-compare", help="比较任意两个预测版本")
    compare.add_argument("--a", type=int, required=True)
    compare.add_argument("--b", type=int, required=True)

    cutoff = sub.add_parser("cutoff-run", help="执行截单任务，解析当时有效版本")
    _key_arguments(cutoff)
    cutoff.add_argument("--supply-cap", required=True, help="截单时确认的供应上限（桶）")
    cutoff.add_argument("--at", default=None, help="注入的 UTC 时间，默认系统时间")

    cutoff_show = sub.add_parser("cutoff-show", help="查看截单运行及其永久保留的输入摘要")
    _key_arguments(cutoff_show)

    actual = sub.add_parser("actual-record", help="登记实际出库实绩")
    actual.add_argument("--data", required=True, help="JSON 文本、@文件路径，或 - 表示标准输入")

    analyze = sub.add_parser("variance-analyze", help="实绩到齐后在数量闭合下分解偏差")
    _key_arguments(analyze)

    history = sub.add_parser("variance-list", help="查看某键的偏差分析序列")
    _key_arguments(history)
    return parser


def _dispatch(service: SupplyService, args: argparse.Namespace) -> Any:
    actor = args.actor
    if args.command == "user-create":
        return service.create_user(args.user_id, args.display_name, args.role)
    if args.command == "forecast-draft":
        return service.submit_forecast_draft(actor, _json_payload(args.data))
    if args.command == "forecast-approve":
        return service.approve_forecast(actor, args.version_id, args.expected_revision, args.effective_at)
    if args.command == "forecast-show":
        return service.forecast_version(actor, args.version_id)
    if args.command == "forecast-timeline":
        return service.forecast_timeline(actor, args.region, args.product, args.business_date)
    if args.command == "forecast-compare":
        return service.compare_forecasts(actor, args.a, args.b)
    if args.command == "cutoff-run":
        return service.run_forecast_cutoff(actor, args.region, args.product, args.business_date, args.supply_cap)
    if args.command == "cutoff-show":
        return service.forecast_cutoff(actor, args.region, args.product, args.business_date)
    if args.command == "actual-record":
        return service.record_forecast_actual(actor, _json_payload(args.data))
    if args.command == "variance-analyze":
        return service.analyze_forecast_variance(actor, args.region, args.product, args.business_date)
    if args.command == "variance-list":
        return service.forecast_variance_history(actor, args.region, args.product, args.business_date)
    raise ValueError(f"未知命令 {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    injected = getattr(args, "at", None)
    clock = FrozenClock(parse_utc(injected, "at")) if injected else SystemClock()
    connection = connect(args.database)
    try:
        result = _dispatch(SupplyService(connection, clock), args)
    except SupplyError as exc:
        print(canonical_json({"error": {"code": exc.code, "message": str(exc)}}), file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError) as exc:
        print(canonical_json({"error": {"code": "invalid_request", "message": str(exc)}}), file=sys.stderr)
        return 2
    finally:
        connection.close()
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
