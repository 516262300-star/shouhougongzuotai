from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AutomationActionType,
    AutomationTaskStatus,
)
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.actions import (
    ActionCoordinator,
    ErpResultCode,
    ExternalActionExecutor,
    InterceptResult,
)


def _print_ok(**details: object) -> None:
    print(json.dumps({"ok": True, **details}, ensure_ascii=False, indent=2))


def execute_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预览或执行模块 1/3 的外部动作队列。")
    parser.add_argument(
        "--types",
        nargs="*",
        choices=("QYWX_INTERCEPT_NOTIFY", "PDD_AGREE_REFUND"),
        help="限定动作类型；默认同时扫描两类",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真实调用外部接口；省略时仅预览数量",
    )
    args = parser.parse_args(argv)
    action_types = (
        tuple(AutomationActionType(value) for value in args.types) if args.types else None
    )
    with SessionLocal() as session:
        result = ExternalActionExecutor(session, get_settings()).run(
            action_types=action_types,
            limit=args.limit,
            dry_run=not args.apply,
        )
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return 0 if result.failed == 0 else 1


def list_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="列出模块 1/3 动作任务。")
    parser.add_argument("--status", choices=[item.value for item in AutomationTaskStatus])
    parser.add_argument("--type", choices=[item.value for item in AutomationActionType])
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 500:
        parser.error("--limit 必须在 1–500 之间")
    statement = select(AftersalesActionTask).order_by(AftersalesActionTask.id.desc())
    if args.status:
        statement = statement.where(
            AftersalesActionTask.action_status == AutomationTaskStatus(args.status)
        )
    if args.type:
        statement = statement.where(
            AftersalesActionTask.action_type == AutomationActionType(args.type)
        )
    statement = statement.limit(args.limit)
    with SessionLocal() as session:
        tasks = session.scalars(statement).all()
    print(
        json.dumps(
            [
                {
                    "id": task.id,
                    "after_sales_sn": task.after_sales_sn,
                    "action_type": str(task.action_type),
                    "action_status": str(task.action_status),
                    "attempts": task.attempts,
                    "last_error": task.last_error,
                }
                for task in tasks
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def confirm_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填 ERP 动作结果并推进模块 3 状态机。")
    parser.add_argument("--task-id", required=True, type=int)
    outcome = parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--success", action="store_true")
    outcome.add_argument("--failed", action="store_true")
    parser.add_argument("--result-code", choices=[item.value for item in ErpResultCode])
    parser.add_argument("--reference-sn")
    parser.add_argument("--message")
    args = parser.parse_args(argv)
    result_code = ErpResultCode(args.result_code) if args.result_code else None
    with SessionLocal() as session:
        ActionCoordinator(session).confirm_erp_action(
            task_id=args.task_id,
            success=args.success,
            result_code=result_code,
            reference_sn=args.reference_sn,
            message=args.message,
        )
    _print_ok(task_id=args.task_id)
    return 0


def intercept_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填模块 1 快递拦截结果。")
    parser.add_argument("--after-sales-sn", required=True)
    parser.add_argument("--result", required=True, choices=[item.value for item in InterceptResult])
    parser.add_argument("--note")
    args = parser.parse_args(argv)
    with SessionLocal() as session:
        changed = ActionCoordinator(session).confirm_intercept_result(
            after_sales_sn=args.after_sales_sn,
            result=InterceptResult(args.result),
            note=args.note,
        )
    _print_ok(changed=changed)
    return 0
