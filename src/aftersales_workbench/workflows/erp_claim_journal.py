"""本机ERP专用账号/TH单认领占用及逐步骤持久化；未知请求只回查。

与资金money_operations分开：认领不是第二次平台退款。此库必须留在持久化.runtime，
不能使用每次重建的发布目录，也不能删除账本来重试。
"""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path


class ClaimJournal:
    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("认领账本必须使用持久化绝对路径")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS claims (
                    receipt TEXT PRIMARY KEY, owner TEXT NOT NULL, account TEXT NOT NULL,
                    state TEXT NOT NULL, evidence TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    account TEXT PRIMARY KEY, receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS steps (
                    receipt TEXT NOT NULL, step TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(receipt,step));
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def execution_lock(self, account):
        """进程死亡由操作系统释放锁；持久化业务占用不会随之删除。"""
        path = self.path.with_name("erp-claim-" + sha256(account.encode()).hexdigest() + ".lock")
        with path.open("a+b") as stream:
            try:
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("ERP认领执行器已占用该账号") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def active(self, owner):
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT receipt,evidence FROM claims WHERE owner=? AND state=?", (owner, "ACTIVE")
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("同售后存在多个未完成认领，须人工核查")
        return (rows[0][0], json.loads(rows[0][1])) if rows else None

    def steps(self, receipt):
        with closing(self.connect()) as db:
            return dict(db.execute("SELECT step,state FROM steps WHERE receipt=?", (receipt,)))

    def active_records(self):
        with closing(self.connect()) as db:
            return [
                (receipt, owner, account, json.loads(proof))
                for receipt, owner, account, proof in db.execute(
                    "SELECT receipt,owner,account,evidence FROM claims WHERE state=?", ("ACTIVE",)
                )
            ]

    def reserve(self, receipt, owner, account, evidence):
        encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT owner,account,state,evidence FROM claims WHERE receipt=?", (receipt,)
            ).fetchone()
            held = db.execute("SELECT receipt FROM accounts WHERE account=?", (account,)).fetchone()
            if held and held[0] != receipt:
                raise ValueError("ERP账号存在另一笔未结束的认领，禁止混用草稿")
            if previous and previous != (owner, account, "ACTIVE", encoded):
                raise ValueError("该退货单已占用、完成或批准明细改变")
            db.execute(
                "INSERT OR IGNORE INTO claims VALUES (?,?,?,?,?)",
                (receipt, owner, account, "ACTIVE", encoded),
            )
            db.execute("INSERT OR IGNORE INTO accounts VALUES (?,?)", (account, receipt))

    def perform(self, receipt, step, write, verify):
        sequence = ("MOVE", "ASSIGN", "SAVE")
        if step not in sequence:
            raise ValueError("未知认领步骤")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            claim = db.execute("SELECT state FROM claims WHERE receipt=?", (receipt,)).fetchone()
            if claim != ("ACTIVE",):
                raise ValueError("认领缺少有效占用")
            previous = db.execute(
                "SELECT state FROM steps WHERE receipt=? AND step=?", (receipt, step)
            ).fetchone()
            if previous:
                raise ValueError("该认领步骤已经发起，仅允许只读回查，不自动重发")
            prior = dict(db.execute("SELECT step,state FROM steps WHERE receipt=?", (receipt,)))
            if any(prior.get(s) != "VERIFIED" for s in sequence[: sequence.index(step)]):
                raise ValueError("前序认领步骤尚未核实")
            db.execute(
                "INSERT INTO steps VALUES (?,?,?,?)",
                (receipt, step, "REQUEST_STARTED", datetime.now(UTC).isoformat()),
            )
        try:
            write()
            if not verify():
                raise ValueError("ERP认领请求已发出，但结果尚未核实")
        except Exception:
            with closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE steps SET state=? WHERE receipt=? AND step=?",
                    ("UNKNOWN", receipt, step),
                )
            raise
        self.confirm_step(receipt, step)

    def confirm_step(self, receipt, step):
        # 只能由上层读取到对应真实状态后调用；不创建伪造的已发送步骤。
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE steps SET state=? WHERE receipt=? AND step=?", ("VERIFIED", receipt, step)
            )

    def complete(self, receipt):
        # 只能在原TH单真实进入对应客户名下后释放账号，不以save响应完成。
        with closing(self.connect()) as db, db:
            states = dict(db.execute("SELECT step,state FROM steps WHERE receipt=?", (receipt,)))
            if states != dict.fromkeys(("MOVE", "ASSIGN", "SAVE"), "VERIFIED"):
                raise ValueError("认领步骤未核实，不允许释放账号")
            db.execute("UPDATE claims SET state=? WHERE receipt=?", ("VERIFIED", receipt))
            db.execute("DELETE FROM accounts WHERE receipt=?", (receipt,))
