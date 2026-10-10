"""Duplicate detector for freight invoice duplicates and resent paid invoices.

Rules (GLOSSARY.md & EXPLANATION.md):
- Two invoice numbers covering the same shipment line: flag the one with the later invoice date.
  The amount is the duplicated lines on that later invoice only.
- Match by: same shipment ID/AWB AND similar charge type (fuzzy match >= 0.8)
  AND similar amount (within 1%).
- The same invoice number sent twice: a finding only when the payment ledger/AP register
  shows the first copy paid or scheduled AND the resend asks for payment again.
  The amount is the invoice total.
- Credited in full and then billed again is fine (no finding).
  Credited in part: amount is overlap minus credit.
- Multiple duplicate lines on one invoice add up to one finding keyed by (invoice_id, 'duplicate').
- Clean invoices / decoys (e.g. different house AWBs or different charge types) are not flagged.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
import re
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


def normalize_charge_type(s: str) -> str:
    s = str(s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def charge_type_similarity(s1: str, s2: str) -> float:
    """Fuzzy similarity between charge descriptions (threshold ~0.8)."""
    n1 = normalize_charge_type(s1)
    n2 = normalize_charge_type(s2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2 or n1.replace(" ", "") == n2.replace(" ", ""):
        return 1.0
    if n1 in n2 or n2 in n1:
        return 0.9

    # Strip generic suffix words (fee, charge, surcharge)
    base1 = re.sub(r"\b(fee|charge|surcharge)\b", "", n1).strip()
    base2 = re.sub(r"\b(fee|charge|surcharge)\b", "", n2).strip()
    if base1 and base2:
        if base1 == base2 or base1.replace(" ", "") == base2.replace(" ", ""):
            return 0.95
        if base1 in base2 or base2 in base1:
            return 0.85

    return SequenceMatcher(None, n1, n2).ratio()


def amounts_match(a1: Decimal, a2: Decimal) -> bool:
    """Check if amounts are identical or within 1% variation."""
    if a1 <= 0 or a2 <= 0:
        return False
    diff = abs(a1 - a2)
    if diff <= Decimal("0.01"):
        return True
    return (diff / max(a1, a2)) <= Decimal("0.01")


def check_duplicate(
    facts: list[dict[str, Any]],
    as_of_date: str | datetime.date,
    mode: str = "supervised",
    include_clean: bool = False,
) -> list[dict[str, Any]]:
    """Detect duplicate freight invoices and duplicate lines across invoices.

    Parameters
    ----------
    facts : list of extracted facts
    as_of_date : str or datetime.date
        Only documents received on or before as_of count.
    mode : "supervised" or "autonomous"
    include_clean : bool, default False
        If True, also include clean invoices as verdict='no_finding'.

    Returns
    -------
    list of invoice finding dicts matching grader schema.
    """
    as_of_dt = _parse_date(as_of_date)
    if as_of_dt is None:
        return []

    # 1. Collect invoices received on or before as_of_dt
    invoices: list[dict[str, Any]] = []
    credit_notes: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "invoice":
            inv_date = _parse_date(f.get("invoice_date"))
            if inv_date is not None and inv_date > as_of_dt:
                continue
            if f.get("is_credit_note"):
                credit_notes.append(f)
            else:
                invoices.append(f)

    # 2. Collect payment ledger facts and email events
    payments: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "payment":
            p_date = _parse_date(f.get("date") or f.get("as_of") or f.get("document_date"))
            if p_date is not None and p_date > as_of_dt:
                continue
            payments.append(f)

    email_events: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "email_event":
            e_date = _parse_date(f.get("email_date"))
            if e_date is not None and e_date > as_of_dt:
                continue
            email_events.append(f)

    findings: list[dict[str, Any]] = []
    flagged_invoices: set[str] = set()

    # -------------------------------------------------------------------------
    # Pattern 1: Same invoice number sent twice after payment (DU-01)
    # -------------------------------------------------------------------------
    # Build map of paid invoices from payment entries
    paid_invoices: dict[str, dict[str, Any]] = {}
    for p in payments:
        raw_pe = p.get("payment_entries")
        if isinstance(raw_pe, list):
            for entry in raw_pe:
                if not isinstance(entry, dict):
                    continue
                inv_id = str(entry.get("invoice_id") or "").strip().upper()
                status = str(entry.get("status") or "").lower()
                if inv_id and ("paid" in status or "scheduled" in status):
                    p_quote = entry.get("quote") or p.get("quote", "")
                    p_src = entry.get("source") if isinstance(entry.get("source"), dict) else (p.get("source", {}) if isinstance(p.get("source"), dict) else {})
                    paid_invoices[inv_id] = {
                        "entry": entry,
                        "paid_date": _parse_date(entry.get("paid_date")),
                        "amount": entry.get("amount"),
                        "quote": p_quote,
                        "source": p_src,
                    }

    # Check for resend emails requesting payment for already-paid invoices
    for inv_id, paid_info in paid_invoices.items():
        try:
            paid_dt = paid_info.get("paid_date")
            # Look for emails referencing this invoice received after paid_date requesting payment
            for ev in email_events:
                ref_inv = str(ev.get("referenced_invoice_id") or "").strip().upper()
                quote_text = str(ev.get("quote") or "")
                subj_text = str(ev.get("subject") or "")
                body_text = f"{quote_text} {subj_text}".lower()

                if (ref_inv == inv_id or inv_id.lower() in body_text):
                    ev_date = _parse_date(ev.get("email_date"))
                    if paid_dt and ev_date and ev_date >= paid_dt:
                        # Check if email asks for payment and is not a mere copy/receipt
                        asks_payment = any(
                            phrase in body_text
                            for phrase in ("process payment", "outstanding", "due", "remit", "please pay", "unpaid")
                        )
                        is_mere_copy = any(
                            phrase in body_text
                            for phrase in ("for your records", "copy only", "receipt", "paid in full")
                        )
                        if asks_payment and not is_mere_copy:
                            # Resend of paid invoice found!
                            amt_str = str(paid_info.get("amount") or "0.00").replace(",", "").replace("$", "")
                            try:
                                amt_val = Decimal(amt_str)
                            except Exception:
                                amt_val = Decimal("0.00")

                            # If invoice fact has invoice_total, prefer it
                            for inv in invoices:
                                if str(inv.get("invoice_id", "")).strip().upper() == inv_id:
                                    try:
                                        amt_val = Decimal(str(inv.get("invoice_total") or amt_val))
                                    except Exception:
                                        pass
                                    break

                            action = "human_review" if amt_val >= Decimal("5000.00") else (
                                "execute:dispute" if mode == "autonomous" else "draft:dispute"
                            )

                            ev_list = []
                            if paid_info.get("quote") and paid_info.get("source"):
                                ev_list.append({
                                    "doc": paid_info["source"].get("doc_ref", ""),
                                    "page": paid_info["source"].get("page", 1),
                                    "quote": paid_info["quote"],
                                })
                            if ev.get("quote") and ev.get("source"):
                                ev_list.append({
                                    "doc": ev["source"].get("doc_ref", ""),
                                    "page": ev["source"].get("page", 1),
                                    "quote": ev["quote"],
                                })

                            findings.append({
                                "id": f"duplicate:{inv_id}",
                                "class": "duplicate",
                                "variant": "DU-01",
                                "invoice_id": inv_id,
                                "verdict": "finding",
                                "amount_usd": f"{amt_val:.2f}",
                                "amount_range": None,
                                "deadline": None,
                                "action": action,
                                "confidence": 0.95,
                                "evidence": ev_list,
                                "missing_doc": None,
                                "supersedes": [],
                                "learned_at": None,
                            })
                            flagged_invoices.add(inv_id)
                            break
        except Exception:
            continue

    # -------------------------------------------------------------------------
    # Pattern 2: Two different invoice numbers covering the same shipment line
    # -------------------------------------------------------------------------
    # Flatten all lines with invoice metadata
    all_lines: list[dict[str, Any]] = []
    for inv in invoices:
        try:
            inv_id = str(inv.get("invoice_id", "")).strip().upper()
            inv_date = _parse_date(inv.get("invoice_date"))
            inv_src = inv.get("source") if isinstance(inv.get("source"), dict) else {}
            inv_quote = str(inv.get("quote") or "")

            raw_lines = inv.get("lines")
            if not isinstance(raw_lines, list):
                continue

            for line_idx, line in enumerate(raw_lines):
                if not isinstance(line, dict):
                    continue
                shp = str(line.get("shipment_id") or line.get("awb") or "").strip().upper()
                ctype = str(line.get("charge_type") or line.get("description") or "").strip()
                amt_raw = line.get("amount") or "0.00"
                try:
                    amt_dec = Decimal(str(amt_raw))
                except Exception:
                    continue

                all_lines.append({
                    "invoice_id": inv_id,
                    "invoice_date": inv_date,
                    "line_index": line_idx,
                    "line_data": line,
                    "shipment_id": shp,
                    "charge_type": ctype,
                    "amount": amt_dec,
                    "source": line.get("source") or inv_src,
                    "quote": line.get("quote") or inv_quote,
                })
        except Exception:
            continue

    # Group duplicate lines by later invoice
    later_invoice_duplicates: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}

    for i in range(len(all_lines)):
        l1 = all_lines[i]
        if not l1["shipment_id"] or l1["amount"] <= 0:
            continue

        for j in range(i + 1, len(all_lines)):
            l2 = all_lines[j]
            if not l2["shipment_id"] or l2["amount"] <= 0:
                continue

            # Must be different invoices
            if l1["invoice_id"] == l2["invoice_id"]:
                continue

            # Check same shipment ID
            if l1["shipment_id"] != l2["shipment_id"]:
                continue

            # Check similar charge type
            sim = charge_type_similarity(l1["charge_type"], l2["charge_type"])
            if sim < 0.80:
                continue

            # Check similar amount (within 1%)
            if not amounts_match(l1["amount"], l2["amount"]):
                continue

            # Determine earlier vs later invoice
            d1 = l1["invoice_date"] or datetime.date.min
            d2 = l2["invoice_date"] or datetime.date.min

            if d2 > d1:
                earlier, later = l1, l2
            elif d1 > d2:
                earlier, later = l2, l1
            else:
                # Same date: break tie deterministically by invoice_id
                if l2["invoice_id"] > l1["invoice_id"]:
                    earlier, later = l1, l2
                else:
                    earlier, later = l2, l1

            later_inv_id = later["invoice_id"]
            if later_inv_id not in flagged_invoices:
                later_invoice_duplicates.setdefault(later_inv_id, []).append((earlier, later))

    # Build findings for cross-invoice duplicates
    for later_inv_id, match_pairs in later_invoice_duplicates.items():
        total_dup = Decimal("0.00")
        ev_list: list[dict[str, Any]] = []
        seen_quotes: set[str] = set()

        for earlier, later in match_pairs:
            total_dup += later["amount"]

            # Quote from earlier invoice line
            e_q = earlier["quote"]
            e_src = earlier["source"]
            if e_q and e_q not in seen_quotes:
                seen_quotes.add(e_q)
                ev_list.append({
                    "doc": e_src.get("doc_ref", ""),
                    "page": e_src.get("page", 1),
                    "quote": e_q,
                })

            # Quote from later invoice line
            l_q = later["quote"]
            l_src = later["source"]
            if l_q and l_q not in seen_quotes:
                seen_quotes.add(l_q)
                ev_list.append({
                    "doc": l_src.get("doc_ref", ""),
                    "page": l_src.get("page", 1),
                    "quote": l_q,
                })

        action = "human_review" if total_dup >= Decimal("5000.00") else (
            "execute:dispute" if mode == "autonomous" else "draft:dispute"
        )

        findings.append({
            "id": f"duplicate:{later_inv_id}",
            "class": "duplicate",
            "variant": "DU-02",
            "invoice_id": later_inv_id,
            "verdict": "finding",
            "amount_usd": f"{total_dup:.2f}",
            "amount_range": None,
            "deadline": None,
            "action": action,
            "confidence": 0.95,
            "evidence": ev_list,
            "missing_doc": None,
            "supersedes": [],
            "learned_at": None,
        })
        flagged_invoices.add(later_inv_id)

    # -------------------------------------------------------------------------
    # Decoys / Clean controls (if include_clean is True)
    # -------------------------------------------------------------------------
    if include_clean:
        for inv in invoices:
            inv_id = str(inv.get("invoice_id", "")).strip().upper()
            if inv_id not in flagged_invoices:
                decoy_ev = []
                inv_quote = inv.get("quote", "")
                inv_src = inv.get("source", {})
                if inv_quote and inv_src:
                    decoy_ev.append({
                        "doc": inv_src.get("doc_ref", ""),
                        "page": inv_src.get("page", 1),
                        "quote": inv_quote,
                    })
                findings.append({
                    "id": f"duplicate:{inv_id}",
                    "class": "duplicate",
                    "variant": "DU-05",
                    "invoice_id": inv_id,
                    "verdict": "no_finding",
                    "amount_usd": None,
                    "amount_range": None,
                    "deadline": None,
                    "action": "none",
                    "confidence": 0.9,
                    "evidence": decoy_ev,
                    "missing_doc": None,
                    "supersedes": [],
                    "learned_at": None,
                })

    return findings
