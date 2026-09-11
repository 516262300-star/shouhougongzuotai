"""单TH单单行认领状态机；调用者提供实时业务核验，不自行猜测客户或价格。"""

from decimal import Decimal

from aftersales_workbench.integrations.erp.return_claim import (
    BASE,
    HEADERS,
    draft_form,
    read_draft,
    read_staged_rows,
    validate_row,
    write_request,
)


class ErpReturnClaim:
    def __init__(self, client, journal, account):
        self.client, self.journal, self.account = client, journal, account

    @staticmethod
    def check(row, evidence):
        validate_row(
            row,
            **{
                k: Decimal(v) if k in {"quantity", "unit_price"} else v
                for k, v in evidence.items()
                if k
                in {"receipt", "tracking", "customer", "product", "color", "quantity", "unit_price"}
            },
        )
        original = evidence["row"]
        # 编号整体搬入草稿，id/制单人会变化；其余原实收字段必须保持。
        for key in HEADERS - {"id", "经办人"}:
            if row[key] != original[key]:
                raise ValueError("认领草稿与已占用的原退货明细发生变化")

    def _draft(self, evidence, *, assigned=False):
        rows, fields = read_draft(self.client)
        if len(rows) != 1 or fields["filenr"] != evidence["receipt"]:
            raise ValueError("认领草稿缺失、含其他单据或不唯一")
        self.check(rows[0], evidence)
        if assigned and rows[0]["经办人"] != evidence["customer"]:
            raise ValueError("草稿尚未归属目标客户")
        return rows[0], fields

    def _moved(self, evidence):
        self._draft(evidence)
        if any(
            row["编号"] == evidence["receipt"] or row["运单号"] == evidence["tracking"]
            for row in read_staged_rows(self.client)
        ):
            raise ValueError("暂存仍有同单或同包裹，搬单可能只完成一部分，禁止继续保存")
        return True

    def advance(self, owner, evidence, *, guard, formal_verified):
        """每次最多发送一个写步骤；UNKNOWN阶段只读回查，绝不重发。"""
        receipt = evidence["receipt"]
        with self.journal.execution_lock(self.account):
            active = self.journal.active(owner)
            if active and active != (receipt, evidence):
                raise ValueError("当前业务证据与持久化认领占用不一致")
            if active:
                self.journal.reserve(receipt, owner, self.account, evidence)
            steps = self.journal.steps(receipt)
            if "SAVE" in steps:
                rows, _ = read_draft(self.client)
                if rows:
                    raise ValueError("保存已发起但草稿仍存在，仅允许只读核查")
                if formal_verified():
                    self.journal.confirm_step(receipt, "SAVE")
                    self.journal.complete(receipt)
                    return "claimed"
                return "awaiting_posting"
            if "MOVE" not in steps:
                guard()
                rows, _ = read_draft(self.client)
                if rows:
                    raise ValueError("ERP账号已有未保存单据，不能搬入其他退货")
                staged = read_staged_rows(self.client)
                matches = [
                    r for r in staged if r["编号"] == receipt or r["运单号"] == evidence["tracking"]
                ]
                if len(matches) != 1 or matches[0] != evidence["row"]:
                    raise ValueError("暂存退货整单或同包裹明细发生变化")
                self.check(matches[0], evidence)
                self.journal.reserve(receipt, owner, self.account, evidence)
                self.journal.perform(
                    receipt,
                    "MOVE",
                    lambda: write_request(
                        self.client, "/leedis2/public/b4refund/" + matches[0]["id"]
                    ),
                    lambda: self._moved(evidence),
                )
                return "draft_moved"
            if not active:
                raise ValueError("缺少本售后有效认领占用")
            # 中断恢复：仅已发出MOVE的精确草稿可恢复，不认领陌生草稿。
            self._moved(evidence)
            self.journal.confirm_step(receipt, "MOVE")
            if "ASSIGN" not in steps:
                guard()
                row, fields = self._draft(evidence)
                self.journal.perform(
                    receipt,
                    "ASSIGN",
                    lambda: write_request(
                        self.client,
                        BASE + "/thnew",
                        data=draft_form(row, evidence["customer"], fields),
                    ),
                    lambda: bool(self._draft(evidence, assigned=True)),
                )
                return "draft_assigned"
            self._draft(evidence, assigned=True)
            self.journal.confirm_step(receipt, "ASSIGN")
            guard()
            self._draft(evidence, assigned=True)
            self.journal.perform(
                receipt,
                "SAVE",
                lambda: write_request(self.client, BASE + "/saveallth"),
                lambda: not read_draft(self.client)[0],
            )
            return "awaiting_posting"
