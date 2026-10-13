"""Financial reconciliation service — Phase 15 Prompt 4 + Prompt 6.

READ-ONLY detection over the authoritative records (wallet, ledger, orders,
refund records, adjustment records, invoices, statements).  It never writes
financial data and never repairs anything — discrepancies are recorded for
an authorized review workflow (reconciliation_runs / _discrepancies).

Prompt-4 checks (wallet↔ledger, chain, duplicates, orphans, refund caps,
currency) plus Prompt-6 families:

  REFUND_LEDGER_MISMATCH      refund record ↔ ledger row (amount/reseller)
  MISSING_REFUND_RECORD       ledger refund with no business record
                              (INFO for pre-P15 legacy rows)
  ADJUSTMENT_LEDGER_MISMATCH  completed adjustment ↔ ledger row
  MISSING_LEDGER_TRANSACTION  completed record posted nothing
  INVOICE_LEDGER_MISMATCH     finalized invoice totals vs ledger window
  STATEMENT_LEDGER_MISMATCH   finalized statement balances/lines vs ledger

Severity policy is documented in database/reconciliation_db.py.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta

from database.db import get_conn, get_cursor
from utils.timezone import now_text
import database.reseller_db as rdb

_TX_DIRECTION = {
    "topup_credit": "credit",
    "refund_credit": "credit",
    "adjustment_credit": "credit",
    "purchase_debit": "debit",
    "adjustment_debit": "debit",
}
_REFUND_PREFIXES = ("rapi.refund.order.", "rapi.cancel_refund.order.",
                    "rapi.partial_refund.order.", "rapi.admin_refund.order.")
_ORDER_REF_RE = re.compile(r"\.order\.(\d+)(?:\.\d+)?$")

# check -> (discrepancy type, severity)
_SEVERITY = {
    "balance_mismatch": "critical",
    "chain_break": "critical",
    "negative_balance": "critical",
    "refund_exceeds_charge": "critical",
    "duplicate_purchase_debit": "critical",
    "refund_ledger_mismatch": "high",
    "adjustment_ledger_mismatch": "high",
    "missing_ledger_transaction": "high",
    "invoice_ledger_mismatch": "high",
    "statement_ledger_mismatch": "high",
    "orphan_transaction": "high",
    "debit_without_order": "high",
    "refund_without_debit": "high",
    "duplicate_reference": "medium",
    "currency_mismatch": "medium",
    "duplicate_refund": "medium",
    "missing_refund_record": "info",
    "missing_adjustment_record": "info",
    "zero_amount": "info",
    "balance_overflow": "info",
}


def _sev(check: str) -> str:
    return _SEVERITY.get(check, "medium")


def _issue(reseller_id, check, detail, row_id=None):
    return {"check": check, "reseller_id": reseller_id,
            "ledger_id": row_id, "detail": str(detail)[:300],
            "type": check, "severity": _sev(check)}


def reconcile_reseller(reseller_id: int, date_from: str = None,
                       date_to: str = None) -> list:
    """Run every rule for one reseller; return a list of issues (empty =
    clean).  Read-only.  Optional half-open date window scopes the
    row-level checks; the balance chain always runs full-history."""
    cur = get_cursor()
    rid = int(reseller_id)
    issues = []

    where = "reseller_id=?"
    params = [rid]
    if date_from:
        where += " AND created >= ?"
        params.append(f"{date_from} 00:00:00")
    if date_to:
        where += " AND created < ?"
        params.append(
            (datetime.strptime(str(date_to), "%Y-%m-%d") + timedelta(days=1)
             ).strftime("%Y-%m-%d %H:%M:%S"))

    cur.execute(
        "SELECT id, tx_type, amount_cents, balance_after_cents, reference,"
        " currency FROM reseller_wallet_transactions"
        f" WHERE {where} ORDER BY id",
        tuple(params),
    )
    rows = cur.fetchall()

    # wallet vs ledger is a whole-wallet fact (the balance is the ledger's
    # total) — it runs even when the caller scopes row-checks to a window
    cur.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM reseller_wallet_transactions"
        " WHERE reseller_id=?", (rid,))
    ledger_sum = int(cur.fetchone()[0] or 0)
    wallet = rdb.get_wallet_balance_cents(rid)
    if wallet != ledger_sum:
        issues.append(_issue(rid, "balance_mismatch",
                             f"wallet={wallet} ledger={ledger_sum}"))

    # chain consistency (full history, deterministic id order)
    cur.execute(
        "SELECT id, amount_cents, balance_after_cents FROM"
        " reseller_wallet_transactions WHERE reseller_id=? ORDER BY id",
        (rid,),
    )
    running = 0
    for row_id, amount, bal_after in cur.fetchall():
        running += int(amount)
        if int(bal_after) != running:
            issues.append(_issue(rid, "chain_break",
                                 f"row {row_id}: balance_after={bal_after} "
                                 f"expected {running}", row_id))

    # row-level checks over the window
    seen = defaultdict(int)
    for row_id, tx_type, amount, bal_after, ref, currency in rows:
        if int(bal_after) < 0:
            issues.append(_issue(rid, "negative_balance",
                                 f"row {row_id}: {bal_after}", row_id))
        if currency != "USDT":
            issues.append(_issue(rid, "currency_mismatch",
                                 f"row {row_id}: {currency}", row_id))
        if int(amount) == 0:
            issues.append(_issue(rid, "zero_amount", f"row {row_id}", row_id))
        if int(bal_after) > rdb.MAX_WALLET_BALANCE_CENTS:
            issues.append(_issue(rid, "balance_overflow", f"row {row_id}",
                                 row_id))
        seen[(tx_type, ref)] += 1
        if seen[(tx_type, ref)] > 1:
            check = ("duplicate_purchase_debit" if tx_type == "purchase_debit"
                     else "duplicate_refund" if tx_type == "refund_credit"
                     else "duplicate_reference")
            issues.append(_issue(rid, check, f"{tx_type} {ref}", row_id))

    # order ↔ debit linkage
    cur.execute(
        "SELECT request_id FROM orders WHERE reseller_id=? AND source='reseller_api'",
        (rid,),
    )
    order_refs = {f"rapi.{rid}.{r[0]}" for r in cur.fetchall() if r[0]}
    for row_id, tx_type, _a, _b, ref, _c in rows:
        if tx_type == "purchase_debit" and ref not in order_refs:
            issues.append(_issue(rid, "debit_without_order",
                                 f"row {row_id} ref {ref}", row_id))

    # refund ↔ order linkage + caps
    for row_id, tx_type, amount, _b, ref, _c in rows:
        if tx_type != "refund_credit":
            continue
        m = _ORDER_REF_RE.search(str(ref))
        if m is None:
            issues.append(_issue(rid, "refund_without_order_ref",
                                 f"row {row_id} ref {ref}", row_id))
            continue
        oid = int(m.group(1))
        cur.execute(
            "SELECT unit_price_cents, quantity FROM orders WHERE id=? AND reseller_id=?",
            (oid, rid))
        order = cur.fetchone()
        if not order:
            issues.append(_issue(rid, "refund_without_debit",
                                 f"row {row_id} order {oid}", row_id))
            continue
        charged = int(order[0]) * int(order[1])
        order_refund_total = sum(
            int(a) for _i, t2, a, _b, r2, _c2 in rows
            if t2 == "refund_credit" and _ORDER_REF_RE.search(str(r2) or "")
            and int(_ORDER_REF_RE.search(str(r2)).group(1)) == oid)
        if order_refund_total > charged:
            issues.append(_issue(rid, "refund_exceeds_charge",
                                 f"order {oid}: refunded {order_refund_total} "
                                 f"of {charged}", row_id))

    issues.extend(_refund_record_checks(rid, where, params))
    issues.extend(_adjustment_checks(rid))
    issues.extend(_invoice_checks(rid))
    issues.extend(_statement_checks(rid))
    return issues


def _refund_record_checks(rid: int, ledger_where: str, ledger_params) -> list:
    """Refund record ↔ ledger row, both directions."""
    import database.adjustment_db as adb
    cur = get_cursor()
    issues = []
    cur.execute(
        "SELECT id, reference, amount_cents FROM reseller_wallet_transactions"
        f" WHERE {ledger_where} AND tx_type='refund_credit'",
        tuple(ledger_params),
    )
    ledger_refunds = cur.fetchall()
    for row_id, ref, amount in ledger_refunds:
        rec = adb.get_refund_by_ledger_reference(ref)
        if not rec:
            issues.append(_issue(rid, "missing_refund_record",
                                 f"ledger row {row_id} ref {ref} has no "
                                 f"refund record (pre-P15 legacy rows only)",
                                 row_id))
            continue
        if int(rec["amount_cents"]) != int(amount):
            issues.append(_issue(rid, "refund_ledger_mismatch",
                                 f"{rec['public_id']}: record={rec['amount_cents']}"
                                 f" ledger={amount}", row_id))
    # reverse direction: record without a ledger row
    adb.ensure_adjustment_tables()
    cur.execute(
        "SELECT id, public_id, ledger_reference, amount_cents FROM"
        " reseller_refunds WHERE reseller_id=?",
        (rid,),
    )
    for row_id, pub, ref, amount in cur.fetchall():
        cur.execute(
            "SELECT amount_cents FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit' AND reference=?",
            (rid, ref))
        led = cur.fetchone()
        if not led:
            issues.append(_issue(rid, "missing_ledger_transaction",
                                 f"refund record {pub} has no ledger row"))
    return issues


def _adjustment_checks(rid: int) -> list:
    """Adjustment record ↔ ledger row, both directions."""
    import database.adjustment_db as adb
    cur = get_cursor()
    issues = []
    adb.ensure_adjustment_tables()
    cur.execute(
        "SELECT id, public_id, type, amount_cents, status, ledger_reference"
        " FROM reseller_adjustments WHERE reseller_id=?",
        (rid,),
    )
    for row_id, pub, typ, amount, status, ref in cur.fetchall():
        cur.execute(
            "SELECT tx_type, amount_cents FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND reference=?",
            (rid, ref))
        led = cur.fetchone()
        if status == "completed":
            if not led:
                issues.append(_issue(rid, "missing_ledger_transaction",
                                     f"adjustment {pub} completed with no "
                                     f"ledger row"))
                continue
            expected_type = ("adjustment_credit" if typ == "credit"
                             else "adjustment_debit")
            expected_amount = int(amount) if typ == "credit" else -int(amount)
            if led[0] != expected_type or int(led[1]) != expected_amount:
                issues.append(_issue(rid, "adjustment_ledger_mismatch",
                                     f"adjustment {pub}: {typ} {amount} vs "
                                     f"ledger {led[0]} {led[1]}"))
        elif status in ("pending", "approved", "rejected", "cancelled"):
            if led:
                issues.append(_issue(rid, "adjustment_ledger_mismatch",
                                     f"adjustment {pub} is {status} but a "
                                     f"ledger row exists"))
    # ledger adjustment rows without a record (pre-P15 legacy → INFO)
    cur.execute(
        "SELECT id, reference FROM reseller_wallet_transactions"
        " WHERE reseller_id=? AND tx_type IN ('adjustment_credit','adjustment_debit')",
        (rid,),
    )
    for row_id, ref in cur.fetchall():
        cur.execute(
            "SELECT id FROM reseller_adjustments WHERE ledger_reference=?",
            (ref,))
        if not cur.fetchone():
            issues.append(_issue(rid, "missing_adjustment_record",
                                 f"ledger row {row_id} ref {ref} has no "
                                 f"adjustment record (pre-P15 legacy)",
                                 row_id))
    return issues


def _invoice_checks(rid: int) -> list:
    """Finalized invoice totals vs the ledger over the invoice's window."""
    import database.invoice_db as idb
    cur = get_cursor()
    issues = []
    idb.ensure_invoice_tables()
    cur.execute(
        "SELECT id, public_id, period_start, period_end, subtotal_cents,"
        " refund_cents, adjustment_credit_cents, adjustment_debit_cents,"
        " topup_cents, total_cents FROM reseller_invoices"
        " WHERE reseller_id=? AND status='finalized'",
        (rid,),
    )
    for (inv_id, pub, ps, pe, sub, ref, ac, ad, top, total) in cur.fetchall():
        lo = f"{ps} 00:00:00"
        hi = (datetime.strptime(pe, "%Y-%m-%d") + timedelta(days=1)
              ).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            "SELECT"
            " COALESCE(SUM(CASE WHEN tx_type='purchase_debit' THEN -amount_cents END),0),"
            " COALESCE(SUM(CASE WHEN tx_type='refund_credit' THEN amount_cents END),0),"
            " COALESCE(SUM(CASE WHEN tx_type='adjustment_credit' THEN amount_cents END),0),"
            " COALESCE(SUM(CASE WHEN tx_type='adjustment_debit' THEN -amount_cents END),0),"
            " COALESCE(SUM(CASE WHEN tx_type='topup_credit' THEN amount_cents END),0)"
            " FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND created >= ? AND created < ?",
            (rid, lo, hi),
        )
        lp, lr, lac, lad, lt = cur.fetchone()
        expected_total = max(0, int(lp) - int(lr) + int(lad) - int(lac))
        if (int(lp), int(lr), int(lac), int(lad), int(lt)) != \
                (int(sub), int(ref), int(ac), int(ad), int(top)) \
                or int(total) != expected_total:
            issues.append(_issue(
                rid, "invoice_ledger_mismatch",
                f"{pub}: invoice (sub={sub},ref={ref},adjC={ac},adjD={ad},"
                f"top={top},total={total}) vs ledger ({lp},{lr},{lac},{lad},{lt})"))
    return issues


def _statement_checks(rid: int) -> list:
    """Finalized statement balances vs the ledger over its window."""
    import database.statement_db as sdb
    cur = get_cursor()
    issues = []
    sdb.ensure_statement_tables()
    cur.execute(
        "SELECT id, public_id, period_start, period_end,"
        " opening_balance_cents, closing_balance_cents,"
        " total_credits_cents, total_debits_cents, transactions_count"
        " FROM reseller_statements WHERE reseller_id=? AND status='finalized'",
        (rid,),
    )
    for (st_id, pub, ps, pe, opening, closing, credits, debits,
         tx_count) in cur.fetchall():
        cur.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM"
            " reseller_wallet_transactions WHERE reseller_id=? AND created < ?",
            (rid, ps))
        real_opening = int(cur.fetchone()[0] or 0)
        cur.execute(
            "SELECT COALESCE(SUM(CASE WHEN amount_cents>0 THEN amount_cents END),0),"
            " COALESCE(SUM(CASE WHEN amount_cents<0 THEN -amount_cents END),0),"
            " COUNT(*)"
            " FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND created >= ? AND created < ?",
            (rid, ps, pe))
        cred, deb, cnt = cur.fetchone()
        if (int(opening) != real_opening or int(cred or 0) != int(credits)
                or int(deb or 0) != int(debits)
                or int(cnt or 0) != int(tx_count)
                or int(closing) != real_opening + int(cred or 0) - int(deb or 0)):
            issues.append(_issue(
                rid, "statement_ledger_mismatch",
                f"{pub}: opening {opening} vs ledger {real_opening}; "
                f"closing {closing} vs {real_opening + int(cred or 0) - int(deb or 0)}"))
    return issues


def reconcile_platform(limit_resellers: int = 500, date_from: str = None,
                       date_to: str = None) -> dict:
    """Owner-wide reconciliation sweep.  Read-only; batched per reseller."""
    cur = get_cursor()
    cur.execute("SELECT id FROM resellers ORDER BY id LIMIT ?",
                (min(5000, max(1, int(limit_resellers))),))
    rids = [int(r[0]) for r in cur.fetchall()]
    issues = []
    per_reseller = []
    for rid in rids:
        res_issues = reconcile_reseller(rid, date_from=date_from,
                                        date_to=date_to)
        per_reseller.append({"reseller_id": rid, "issues": len(res_issues)})
        issues.extend(res_issues)
    # orphan ledger rows (reseller deleted despite RESTRICT)
    cur.execute(
        "SELECT COUNT(*) FROM reseller_wallet_transactions t"
        " LEFT JOIN resellers r ON r.id=t.reseller_id WHERE r.id IS NULL")
    orphans = int(cur.fetchone()[0] or 0)
    if orphans:
        issues.append(_issue(None, "orphan_transaction",
                             f"{orphans} ledger rows without a reseller"))
    return {
        "ok": not issues,
        "resellers_checked": len(rids),
        "issues": issues,
        "issues_total": len(issues),
        "per_reseller": per_reseller,
    }


# ---------------------------------------------------------------------------
# Phase 15 P6 — persistent runs, discrepancy records, resolution workflow
# ---------------------------------------------------------------------------

def _fingerprint(issue: dict) -> str:
    return "|".join([
        str(issue.get("type") or ""),
        str(issue.get("reseller_id") or ""),
        str(issue.get("ledger_id") or ""),
        str(issue.get("detail") or "")[:120],
    ])


def run_reconciliation(scope: str = "platform", reseller_id: int = None,
                       date_from: str = None, date_to: str = None,
                       created_by: str = "") -> dict:
    """Execute a reconciliation run and persist its discrepancies.

    Runs are read-only over financial data.  Each detected issue fingerprints
    to (type, reseller, row, detail):  an already-open/investigating finding
    is re-touched (last-seen), a resolved finding that reappears opens a NEW
    discrepancy (regression — never rewrites history).
    """
    import database.reconciliation_db as rdb_rec
    rdb_rec.ensure_reconciliation_tables()
    now = now_text()

    run_id = rdb_rec.insert_run({
        "public_id": rdb_rec.generate_run_public_id(),
        "scope": "reseller" if reseller_id else "platform",
        "reseller_id": int(reseller_id) if reseller_id else None,
        "period_start": str(date_from or "") or None,
        "period_end": str(date_to or "") or None,
        "status": "running",
        "created_by": str(created_by or ""),
        "started_at": now,
        "created": now,
    })
    get_conn().commit()

    try:
        if reseller_id:
            issues = reconcile_reseller(int(reseller_id), date_from=date_from,
                                        date_to=date_to)
            checked = 1
        else:
            report = reconcile_platform(date_from=date_from, date_to=date_to)
            issues = report["issues"]
            checked = report["resellers_checked"]

        severity_summary = defaultdict(int)
        for issue in issues:
            issue["fingerprint"] = _fingerprint(issue)
            severity_summary[issue["severity"]] += 1
            existing = rdb_rec.find_open_discrepancy(issue["fingerprint"])
            if existing:
                rdb_rec.touch_discrepancy_seen(int(existing["id"]), run_id)
                continue
            rdb_rec.insert_discrepancy({
                "public_id": rdb_rec.generate_discrepancy_public_id(),
                "run_id": run_id,
                "fingerprint": issue["fingerprint"],
                "reseller_id": issue.get("reseller_id"),
                "type": issue["type"],
                "severity": issue["severity"],
                "source_type": "wallet_ledger",
                "source_ref": str(issue.get("ledger_id") or ""),
                "expected_value": "",
                "actual_value": str(issue.get("detail") or "")[:300],
                "currency": "USDT",
                "status": "open",
                "description": str(issue.get("detail") or "")[:500],
                "detected_at": now_text(),
                "created": now_text(),
                "updated": now_text(),
            })

        import json as _json
        rdb_rec.finish_run(run_id, "completed", checked, len(issues),
                           _json.dumps(dict(severity_summary)))
    except Exception as exc:
        rdb_rec.finish_run(run_id, "failed", 0, 0, "{}", str(exc)[:300])
        raise
    get_conn().commit()
    run = rdb_rec.get_run(run_id)
    run["issues"] = issues
    return run


def investigate_discrepancy(public_id: str, actor: str = "") -> dict:
    return _transition(public_id, "investigating", ("open",), actor)


def resolve_discrepancy(public_id: str, reason: str, resolver: str = "",
                        reference: str = "") -> dict:
    """Resolve requires a real explanation — never a bare status flip."""
    if not str(reason or "").strip():
        raise ReconciliationError("resolution_reason_required",
                                  "A resolution reason is required.", 400)
    return _transition(public_id, "resolved", ("open", "investigating"),
                       resolver, reason, reference)


def ignore_discrepancy(public_id: str, reason: str, resolver: str = "") -> dict:
    if not str(reason or "").strip():
        raise ReconciliationError("resolution_reason_required",
                                  "A reason is required.", 400)
    return _transition(public_id, "ignored", ("open", "investigating"),
                       resolver, reason, "")


def reopen_discrepancy(public_id: str, actor: str = "") -> dict:
    """Resolved/ignored → open (e.g. the fix did not hold)."""
    return _transition(public_id, "open", ("resolved", "ignored"), actor)


def _transition(public_id: str, status: str, allowed_from: tuple,
                actor: str, reason: str = "", reference: str = "") -> dict:
    import database.reconciliation_db as rdb_rec
    disc = rdb_rec.get_discrepancy_by_public_id(public_id)
    if not disc:
        raise ReconciliationError("discrepancy_not_found",
                                  "Discrepancy not found.", 404)
    if disc["status"] == status:
        disc["is_duplicate"] = True
        return disc
    ok = rdb_rec.transition_discrepancy(
        int(disc["id"]), status, allowed_from, reason=reason,
        resolver=actor, reference=reference)
    if not ok:
        raise ReconciliationError("invalid_transition",
                                  f"Cannot move {disc['status']} → {status}.",
                                  409)
    out = rdb_rec.get_discrepancy_by_public_id(public_id)
    out["is_duplicate"] = False
    return out


class ReconciliationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


__all__ = ["reconcile_reseller", "reconcile_platform", "run_reconciliation",
           "investigate_discrepancy", "resolve_discrepancy",
           "ignore_discrepancy", "reopen_discrepancy", "ReconciliationError"]
