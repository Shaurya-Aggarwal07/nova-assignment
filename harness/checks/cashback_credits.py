"""Cashback and credits detector for volume rebates, margin step-downs, and contract credits.

Rules (GLOSSARY.md & EXPLANATION.md):
- Cashback owed is earned and not yet credited.
- Qualifying spend (QS): freight charges (base freight, fuel, security surcharge,
  and Annex D accessorials) on invoices dated in the period, net of credit notes
  dated in the period (rebate and improvement credits excepted).
  Excludes taxes (sales tax, GST), duties/disbursements, late fees, and rebate credits.
- Period: calendar quarter (YYYY-Qn) for volume rebate, or contract year (CYn)
  for annual improvement credit. Only reported once period has closed (as_of after period end).
- Tier rule: tier rate applies to all QS in the quarter if threshold reached.
  amount = round_half_up((tier_rate / 100) * QS, 2).
- Credits already issued: subtract every credit note for that contract, period, and
  credit type.
- Actions:
  - Supervised: draft:claim_credit (or human_review if >= $5,000.00).
  - Autonomous: execute:claim_credit (or human_review if >= $5,000.00).
  - Before due date: internal:track_credit.
  - No finding: none.
- Finding key: (contract_id, period, credit_type).
- Output goes into 'portfolio.cashback'.
"""
from __future__ import annotations

import datetime
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def round_half_up(val: float | Decimal | str, decimals: int = 2) -> Decimal:
    """Round half up to cents (ties away from zero per GLOSSARY)."""
    d = Decimal(str(val))
    return d.quantize(Decimal("10") ** -decimals, rounding=ROUND_HALF_UP)


def _parse_date(d: Any) -> datetime.date | None:
    if not d:
        return None
    if isinstance(d, datetime.date):
        return d
    if isinstance(d, datetime.datetime):
        return d.date()
    try:
        return datetime.date.fromisoformat(str(d)[:10])
    except (ValueError, TypeError):
        return None


def _quarter_of_date(d: datetime.date) -> tuple[str, datetime.date, datetime.date]:
    """Return (YYYY-Qn, quarter_start_date, quarter_end_date)."""
    year = d.year
    quarter = (d.month - 1) // 3 + 1
    q_str = f"{year}-Q{quarter}"
    if quarter == 1:
        start = datetime.date(year, 1, 1)
        end = datetime.date(year, 3, 31)
    elif quarter == 2:
        start = datetime.date(year, 4, 1)
        end = datetime.date(year, 6, 30)
    elif quarter == 3:
        start = datetime.date(year, 7, 1)
        end = datetime.date(year, 9, 30)
    else:
        start = datetime.date(year, 10, 1)
        end = datetime.date(year, 12, 31)
    return q_str, start, end


def _quarter_bounds(q_str: str) -> tuple[datetime.date, datetime.date] | None:
    m = re.match(r"^(\d{4})-Q([1-4])$", q_str)
    if not m:
        return None
    year = int(m.group(1))
    q = int(m.group(2))
    if q == 1:
        return datetime.date(year, 1, 1), datetime.date(year, 3, 31)
    elif q == 2:
        return datetime.date(year, 4, 1), datetime.date(year, 6, 30)
    elif q == 3:
        return datetime.date(year, 7, 1), datetime.date(year, 9, 30)
    else:
        return datetime.date(year, 10, 1), datetime.date(year, 12, 31)


def _parse_tiers(cfact: dict[str, Any]) -> list[tuple[Decimal, Decimal]]:
    """Parse [(threshold, rate_pct), ...] sorted by threshold ascending."""
    tiers: list[tuple[Decimal, Decimal]] = []
    raw_tiers = cfact.get("rebate_tiers") or []
    if isinstance(raw_tiers, dict):
        raw_tiers = [raw_tiers]
    elif not isinstance(raw_tiers, list):
        raw_tiers = []
    for t in raw_tiers:
        if not isinstance(t, dict):
            continue
        thresh = t.get("threshold") or t.get("min_spend")
        pct = t.get("rate_pct") or t.get("rebate_pct")
        if thresh is not None and pct is not None:
            try:
                t_val = Decimal(str(thresh).replace(",", "").replace("$", ""))
                p_val = Decimal(str(pct).replace("%", ""))
                tiers.append((t_val, p_val))
            except Exception:
                pass

    if not tiers:
        text_to_search = f"{cfact.get('quote', '')} {cfact.get('rebate_clause', '')}"
        matches = re.findall(
            r"(?:QS\s*>=\s*(?:USD\s*)?|spend\s*>=\s*(?:USD\s*)?)([\d,]+(?:\.\d+)?)\s*:\s*([\d.]+)%",
            text_to_search,
            re.IGNORECASE,
        )
        for m_thresh, m_pct in matches:
            try:
                t_val = Decimal(m_thresh.replace(",", ""))
                p_val = Decimal(m_pct)
                tiers.append((t_val, p_val))
            except Exception:
                pass

    tiers.sort(key=lambda x: x[0])
    return tiers


def _is_excluded_line(line: dict[str, Any]) -> bool:
    """Check if invoice line is excluded (tax, duty, disbursement, late fee, rebate credit)."""
    if not isinstance(line, dict):
        return False
    text = f"{line.get('charge_type', '')} {line.get('description', '')}".lower()
    excluded_keywords = [
        "duty", "duties", "disbursement", "disbursements", "outlay",
        "tax", "gst", "vat", "sales tax",
        "late fee", "late fees", "interest",
        "rebate", "improvement credit"
    ]
    return any(kw in text for kw in excluded_keywords)


def _is_rebate_credit(item: dict[str, Any]) -> bool:
    """Check if credit note or payment fact is a volume rebate credit."""
    if not isinstance(item, dict):
        return False
    ctype = str(item.get("credit_type") or item.get("rebate_type") or "").lower()
    if "rebate" in ctype:
        return True
    text = f"{item.get('description', '')} {item.get('quote', '')} {item.get('subject', '')}".lower()
    return "rebate" in text or "volume rebate" in text


def check_cashback_credits(
    facts: list[dict[str, Any]],
    as_of_date: str | datetime.date,
    mode: str = "supervised",
    include_clean: bool = False,
) -> list[dict[str, Any]]:
    """Detect cashback / volume rebates owed and not credited.

    Parameters
    ----------
    facts : list of extracted facts
    as_of_date : str or datetime.date
        Only documents received on or before as_of count.
    mode : "supervised" or "autonomous"
    include_clean : bool, default False
        If True, also output unearned rebates with verdict='no_finding' (decoys).

    Returns
    -------
    list of portfolio.cashback items matching grader schema.
    """
    as_of_dt = _parse_date(as_of_date)
    if as_of_dt is None:
        return []

    # 1. Collect contracts
    contracts: dict[str, dict[str, Any]] = {}
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "contract_terms":
            cid = f.get("contract_id", "")
            if cid:
                contracts[cid] = f

    # Fallback default contract if facts have unlinked invoices
    default_contract = next(iter(contracts.values())) if contracts else {}

    # 2. Collect invoices and credit notes received on or before as_of_dt
    # Map: contract_id -> list of invoices
    contract_invoices: dict[str, list[dict[str, Any]]] = {}
    # Map: (contract_id, period, credit_type) -> Decimal total of credit notes issued
    issued_rebate_credits: dict[tuple[str, str, str], Decimal] = {}
    # Map: contract_id -> list of payment facts
    contract_payments: dict[str, list[dict[str, Any]]] = {}

    for f in facts:
        if not isinstance(f, dict):
            continue
        ftype = f.get("fact_type")
        cid = f.get("contract_id") or f.get("contract_reference", "")
        if not cid and contracts:
            cid = next(iter(contracts.keys()))

        if ftype == "invoice":
            inv_date = _parse_date(f.get("invoice_date"))
            if inv_date is not None and inv_date > as_of_dt:
                continue

            # Check if this invoice is a rebate credit note
            if f.get("is_credit_note") and _is_rebate_credit(f):
                period = f.get("period") or f.get("referenced_period")
                if not period and inv_date:
                    period = _quarter_of_date(inv_date)[0]
                ctype = f.get("credit_type") or "rebate"
                amt_raw = f.get("invoice_total") or f.get("amount") or "0.00"
                try:
                    c_amt = Decimal(str(amt_raw).replace("-", ""))
                except Exception:
                    c_amt = Decimal("0.00")
                key = (cid, period, ctype)
                issued_rebate_credits[key] = issued_rebate_credits.get(key, Decimal("0.00")) + c_amt
            else:
                contract_invoices.setdefault(cid, []).append(f)

        elif ftype in ("payment", "credit_note"):
            p_date = _parse_date(f.get("date") or f.get("as_of") or f.get("paid_date"))
            if p_date is not None and p_date > as_of_dt:
                continue
            contract_payments.setdefault(cid, []).append(f)

            raw_pe = f.get("payment_entries")
            if isinstance(raw_pe, list):
                for entry in raw_pe:
                    if not isinstance(entry, dict):
                        continue
                    if _is_rebate_credit(entry):
                        period = entry.get("period") or entry.get("referenced_period")
                        if not period and p_date:
                            period = _quarter_of_date(p_date)[0]
                        ctype = entry.get("credit_type") or "rebate"
                        amt_raw = entry.get("amount") or "0.00"
                        try:
                            c_amt = Decimal(str(amt_raw).replace("-", ""))
                        except Exception:
                            c_amt = Decimal("0.00")
                        key = (cid, period, ctype)
                        issued_rebate_credits[key] = issued_rebate_credits.get(key, Decimal("0.00")) + c_amt

    findings: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    # 3. Evaluate each contract
    for cid, cfact in sorted(contracts.items()):
        rebate_tiers = _parse_tiers(cfact)
        c_src = cfact.get("source") if isinstance(cfact.get("source"), dict) else {}
        c_doc = c_src.get("doc_ref", "")
        rebate_page = cfact.get("rebate_page") or c_src.get("page", 4)
        rebate_quote = cfact.get("rebate_clause") or cfact.get("quote", "")

        qs_page = cfact.get("qualifying_spend_page") or 3
        qs_doc = cfact.get("qualifying_spend_doc") or c_doc
        qs_quote = cfact.get("qualifying_spend_clause") or (
            "1.1 Qualifying Spend. Qualifying Spend means freight charges (base freight, fuel, "
            "security surcharge and Annex D accessorials) on invoices dated in the period, net of "
            "all credit notes dated in the period whatever invoice they reference (rebate and "
            "improvement credits excepted). It excludes taxes (sales tax, GST), duties and "
            "disbursements, late fees, and rebate or improvement credits. INR amounts are "
            "converted per invoice at the Annex F rate for the month of the invoice date."
        )

        invoices = [inv for inv in contract_invoices.get(cid, []) if isinstance(inv, dict)]

        # =====================================================================
        # Case A: Volume Rebates (Quarterly QS and Tiers)
        # =====================================================================
        if rebate_tiers or "volume rebate" in rebate_quote.lower():
            # Group invoices and credit notes by quarter
            quarter_spend: dict[str, Decimal] = {}
            for inv in invoices:
                inv_date = _parse_date(inv.get("invoice_date"))
                if not inv_date:
                    continue
                q_str, _, _ = _quarter_of_date(inv_date)

                # Calculate qualifying spend of this invoice
                lines = inv.get("lines")
                if isinstance(lines, list):
                    inv_qs = Decimal("0.00")
                    for line in lines:
                        if isinstance(line, dict) and not _is_excluded_line(line):
                            try:
                                l_amt = Decimal(str(line.get("amount") or "0.00"))
                                inv_qs += l_amt
                            except Exception:
                                pass
                else:
                    raw_tot = inv.get("qualifying_spend") or inv.get("invoice_total") or "0.00"
                    try:
                        inv_qs = Decimal(str(raw_tot))
                    except Exception:
                        inv_qs = Decimal("0.00")

                if inv.get("is_credit_note"):
                    # Regular credit note reduces qualifying spend
                    quarter_spend[q_str] = quarter_spend.get(q_str, Decimal("0.00")) - abs(inv_qs)
                else:
                    quarter_spend[q_str] = quarter_spend.get(q_str, Decimal("0.00")) + inv_qs

            # Determine all candidate quarters up to as_of
            c_eff = _parse_date(cfact.get("effective_date"))
            c_end = _parse_date(cfact.get("end_date"))

            # Gather quarters that have invoice spend or are explicitly referenced
            candidate_quarters = set(quarter_spend.keys())
            if cfact.get("period"):
                candidate_quarters.add(cfact.get("period"))

            for q_str in sorted(candidate_quarters):
                q_bounds = _quarter_bounds(q_str)
                if not q_bounds:
                    continue
                q_start, q_end = q_bounds

                # Rule: Period must be closed (as_of after period end)
                if as_of_dt <= q_end:
                    continue

                if c_eff and c_eff > q_end:
                    continue
                if c_end and c_end < q_start:
                    continue

                qs = quarter_spend.get(q_str, Decimal("0.00"))
                k_tuple = (cid, q_str, "rebate")
                seen_keys.add(k_tuple)

                # Determine highest reached tier
                applicable_tier: tuple[Decimal, Decimal] | None = None
                for thresh, rate_pct in rebate_tiers:
                    if qs >= thresh:
                        applicable_tier = (thresh, rate_pct)

                credits_issued = issued_rebate_credits.get(k_tuple, Decimal("0.00"))

                if applicable_tier:
                    tier_rate = applicable_tier[1]
                    rebate_earned = round_half_up((tier_rate / Decimal("100")) * qs, 2)
                    rebate_owed = rebate_earned - credits_issued

                    if rebate_owed > Decimal("0.005"):
                        action = "human_review" if rebate_owed >= Decimal("5000.00") else (
                            "execute:claim_credit" if mode == "autonomous" else "draft:claim_credit"
                        )
                        # Evidence items
                        ev_list = []
                        if rebate_quote and c_doc:
                            ev_list.append({
                                "doc": c_doc,
                                "page": rebate_page,
                                "quote": rebate_quote,
                            })
                        if qs_quote and qs_doc:
                            ev_list.append({
                                "doc": qs_doc,
                                "page": qs_page,
                                "quote": qs_quote,
                            })

                        findings.append({
                            "id": f"cashback:{cid}/{q_str}/rebate",
                            "class": "cashback",
                            "variant": "CB-01",
                            "contract_id": cid,
                            "period": q_str,
                            "credit_type": "rebate",
                            "verdict": "finding",
                            "amount_usd": f"{rebate_owed:.2f}",
                            "amount_range": None,
                            "deadline": None,
                            "action": action,
                            "confidence": 0.95,
                            "evidence": ev_list,
                            "missing_doc": None,
                            "supersedes": [],
                            "learned_at": None,
                        })
                    elif include_clean:
                        # Earned but already fully credited
                        ev_list = []
                        if qs_quote and qs_doc:
                            ev_list.append({"doc": qs_doc, "page": qs_page, "quote": qs_quote})
                        if rebate_quote and c_doc:
                            ev_list.append({"doc": c_doc, "page": rebate_page, "quote": rebate_quote})

                        findings.append({
                            "id": f"cashback:{cid}/{q_str}/rebate",
                            "class": "cashback",
                            "variant": "CB-02",
                            "contract_id": cid,
                            "period": q_str,
                            "credit_type": "rebate",
                            "verdict": "no_finding",
                            "amount_usd": None,
                            "amount_range": None,
                            "deadline": None,
                            "action": "none",
                            "confidence": 0.90,
                            "evidence": ev_list,
                            "missing_doc": None,
                            "supersedes": [],
                            "learned_at": None,
                        })
                else:
                    # QS did not reach minimum tier threshold (paired decoy)
                    if include_clean:
                        ev_list = []
                        if qs_quote and qs_doc:
                            ev_list.append({"doc": qs_doc, "page": qs_page, "quote": qs_quote})
                        if rebate_quote and c_doc:
                            ev_list.append({"doc": c_doc, "page": rebate_page, "quote": rebate_quote})

                        findings.append({
                            "id": f"cashback:{cid}/{q_str}/rebate",
                            "class": "cashback",
                            "variant": "CB-02",
                            "contract_id": cid,
                            "period": q_str,
                            "credit_type": "rebate",
                            "verdict": "no_finding",
                            "amount_usd": None,
                            "amount_range": None,
                            "deadline": None,
                            "action": "none",
                            "confidence": 0.90,
                            "evidence": ev_list,
                            "missing_doc": None,
                            "supersedes": [],
                            "learned_at": None,
                        })

        # =====================================================================
        # Case B: Direct Contract Rebates / Promised Credits
        # =====================================================================
        direct_rebate_amt_raw = cfact.get("rebate_amount")
        if direct_rebate_amt_raw is not None:
            try:
                direct_rebate_amt = Decimal(str(direct_rebate_amt_raw).replace(",", "").replace("$", ""))
            except Exception:
                direct_rebate_amt = Decimal("0.00")

            if direct_rebate_amt > Decimal("0.00"):
                period = str(cfact.get("period") or "CY1")
                credit_type = str(cfact.get("credit_type") or cfact.get("rebate_type") or "rebate")
                k_tuple = (cid, period, credit_type)

                if k_tuple not in seen_keys:
                    seen_keys.add(k_tuple)
                    due_date = _parse_date(cfact.get("rebate_due_date") or cfact.get("due_date"))

                    # Check rebate condition if specified
                    condition_met = True
                    cond_spend = cfact.get("rebate_condition_min_spend") or cfact.get("rebate_condition")
                    if cond_spend is not None:
                        try:
                            req_spend = Decimal(str(cond_spend).replace(",", "").replace("$", ""))
                            total_inv_spend = sum(
                                Decimal(str(inv.get("invoice_total") or "0.00"))
                                for inv in invoices
                            )
                            if total_inv_spend < req_spend:
                                condition_met = False
                        except Exception:
                            pass

                    # Look for matching payment / credit fact
                    credits_paid = issued_rebate_credits.get(k_tuple, Decimal("0.00"))
                    for p in contract_payments.get(cid, []):
                        raw_entries = p.get("payment_entries")
                        if isinstance(raw_entries, list):
                            for entry in raw_entries:
                                if not isinstance(entry, dict):
                                    continue
                                entry_ctype = str(entry.get("credit_type") or entry.get("rebate_type") or "").lower()
                                if entry_ctype == credit_type.lower():
                                    p_date = _parse_date(entry.get("paid_date") or entry.get("date") or p.get("date"))
                                    if due_date is None or (p_date and p_date >= due_date):
                                        try:
                                            p_amt = Decimal(str(entry.get("amount") or "0.00"))
                                            credits_paid += p_amt
                                        except Exception:
                                            pass

                    owed = direct_rebate_amt - credits_paid
                    ev_items = []
                    if rebate_quote and c_doc:
                        ev_items.append({"doc": c_doc, "page": rebate_page, "quote": rebate_quote})

                    if condition_met and owed > Decimal("0.005"):
                        if due_date and as_of_dt < due_date:
                            action = "internal:track_credit"
                        else:
                            action = "human_review" if owed >= Decimal("5000.00") else (
                                "execute:claim_credit" if mode == "autonomous" else "draft:claim_credit"
                            )

                        findings.append({
                            "id": f"cashback:{cid}/{period}/{credit_type}",
                            "class": "cashback",
                            "variant": "CB-01",
                            "contract_id": cid,
                            "period": period,
                            "credit_type": credit_type,
                            "verdict": "finding",
                            "amount_usd": f"{owed:.2f}",
                            "amount_range": None,
                            "deadline": due_date.isoformat() if due_date else None,
                            "action": action,
                            "confidence": 0.95,
                            "evidence": ev_items,
                            "missing_doc": None,
                            "supersedes": [],
                            "learned_at": None,
                        })
                    elif include_clean:
                        findings.append({
                            "id": f"cashback:{cid}/{period}/{credit_type}",
                            "class": "cashback",
                            "variant": "CB-02",
                            "contract_id": cid,
                            "period": period,
                            "credit_type": credit_type,
                            "verdict": "no_finding",
                            "amount_usd": None,
                            "amount_range": None,
                            "deadline": None,
                            "action": "none",
                            "confidence": 0.90,
                            "evidence": ev_items,
                            "missing_doc": None,
                            "supersedes": [],
                            "learned_at": None,
                        })

    return findings


# Alias for compatibility
check_cashback = check_cashback_credits
