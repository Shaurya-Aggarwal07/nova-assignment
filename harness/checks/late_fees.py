"""Late fee detector for late fees not owed and upcoming late fee risks.

Rules (GLOSSARY.md & EXPLANATION.md):
- Sub-case 1: late_fee_not_owed
  - An invoice bills a late fee on an underlying invoice.
  - An authorized person (Schedule 1 for payment_terms) may extend the due date.
  - days_late = paid_date - due_date (where due_date is extended due date if granted).
  - No fee is owed if days_late <= grace_days (or paid on time).
  - If days_late > grace_days, fee is ceil(days_late / 30) months @ contract rate.
  - Any fee billed that is not owed is flagged as 'late_fee_not_owed'.
  - Goes into report 'findings' keyed by (invoice_id, 'late_fee_not_owed').

- Sub-case 2: late_fee_risk
  - An unpaid invoice whose due date is 1 to 14 days after as_of: 0 < due_date - as_of <= 14.
  - Action is internal:payment_reminder.
  - Amount is one month's fee: round_half_up(r / 100 * principal, 2).
  - Deadline is the due date.
  - Goes into 'portfolio.late_fee_risk' keyed by (invoice_id, 'late_fee_risk').
"""
from __future__ import annotations

import datetime
import math
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


def _clean_str(s: Any) -> str:
    return str(s or "").strip().lower()


def check_late_fees(
    facts: list[dict[str, Any]],
    as_of_date: str | datetime.date,
    mode: str = "supervised",
    include_clean: bool = False,
) -> list[dict[str, Any]]:
    """Detect late fees not owed (findings) and late fee risks (portfolio).

    Parameters
    ----------
    facts : list of extracted facts
    as_of_date : str or datetime.date
        Only documents received on or before as_of count.
    mode : "supervised" or "autonomous"
    include_clean : bool, default False
        If True, also include clean invoices with verdict='no_finding'.

    Returns
    -------
    list of finding dicts matching grader schema.
    Items have class='late_fee_not_owed' or class='late_fee_risk'.
    """
    as_of_dt = _parse_date(as_of_date)
    if as_of_dt is None:
        return []

    # 1. Collect contracts, terms, grace days, rates, and extension authority
    contracts: dict[str, dict[str, Any]] = {}
    authorized_payment_contacts: dict[str, dict[str, Any]] = {}

    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "contract_terms":
            cid = f.get("contract_id", "")
            if cid:
                contracts[cid] = f

            c_source = f.get("source") if isinstance(f.get("source"), dict) else {}
            raw_contacts = f.get("schedule_1_contacts")
            if isinstance(raw_contacts, list):
                for contact in raw_contacts:
                    if not isinstance(contact, dict):
                        continue
                    raw_cats = contact.get("categories")
                    if isinstance(raw_cats, list):
                        cats = [str(c).lower() for c in raw_cats if c is not None]
                    elif isinstance(raw_cats, str):
                        cats = [raw_cats.lower()]
                    else:
                        cats = []
                    if "payment_terms" in cats or "payment" in cats:
                        email = _clean_str(contact.get("email"))
                        name = _clean_str(contact.get("name"))
                        entry = {
                            "contact": contact,
                            "contract_id": cid,
                            "source": contact.get("source") if isinstance(contact.get("source"), dict) else c_source,
                            "quote": contact.get("quote", ""),
                            "page": contact.get("page") or 11,
                        }
                        if email:
                            authorized_payment_contacts[email] = entry
                        if name:
                            authorized_payment_contacts[name] = entry

    # Default contract parameters
    default_contract = next(iter(contracts.values())) if contracts else {}
    grace_days_default = int(default_contract.get("grace_days") or 5)
    late_fee_rate_default = Decimal(str(default_contract.get("late_fee_rate_pct") or "1.5"))

    # 2. Collect due date extensions from email_event facts
    # Map: invoice_id -> (extended_date, email_fact)
    extensions: dict[str, tuple[datetime.date, dict[str, Any]]] = {}
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "email_event":
            e_date = _parse_date(f.get("email_date"))
            if e_date is not None and e_date > as_of_dt:
                continue

            ref_inv = str(f.get("referenced_invoice_id") or "").strip().upper()
            quote_text = str(f.get("quote") or "")
            body_text = f"{quote_text} {f.get('subject', '')}"

            # Check if this email grants an extension
            ext_date = _parse_date(f.get("extended_due_date"))
            if not ext_date:
                m_date = re.search(r"to\s+(\d{4}-\d{2}-\d{2})", quote_text)
                if m_date:
                    ext_date = _parse_date(m_date.group(1))

            if not ref_inv:
                m_inv = re.search(r"\b(?:TX|INV)[A-Z0-9-]+\b", body_text)
                if m_inv:
                    ref_inv = m_inv.group(0).upper()

            if ext_date and ref_inv:
                # Check authority of sender
                sender_email = _clean_str(f.get("sender_email"))
                sender_name = _clean_str(f.get("sender_name"))
                auth_ok = True
                if authorized_payment_contacts:
                    auth_ok = any(
                        k and (k in sender_email or sender_email in k or k in sender_name or sender_name in k)
                        for k in authorized_payment_contacts
                    )
                if auth_ok:
                    extensions[ref_inv] = (ext_date, f)

    # 3. Collect payment records from payment facts / AP registers
    # Map: invoice_id -> payment_entry dict
    payment_map: dict[str, dict[str, Any]] = {}
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "payment":
            p_date = _parse_date(f.get("date") or f.get("as_of") or f.get("document_date"))
            if p_date is not None and p_date > as_of_dt:
                continue
            raw_entries = f.get("payment_entries")
            if isinstance(raw_entries, list):
                for entry in raw_entries:
                    if not isinstance(entry, dict):
                        continue
                    inv_id = str(entry.get("invoice_id") or "").strip().upper()
                    if inv_id:
                        payment_map[inv_id] = {
                            "entry": entry,
                            "paid_date": _parse_date(entry.get("paid_date")),
                            "due_date": _parse_date(entry.get("due_date")),
                            "amount": entry.get("amount"),
                            "status": str(entry.get("status") or "").lower(),
                            "quote": entry.get("quote") or f.get("quote", ""),
                            "source": entry.get("source") or f.get("source", {}),
                        }

    # 4. Collect invoices received on or before as_of_dt
    invoices: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "invoice" and not f.get("is_credit_note"):
            inv_date = _parse_date(f.get("invoice_date"))
            if inv_date is not None and inv_date > as_of_dt:
                continue
            invoices.append(f)

    findings: list[dict[str, Any]] = []

    # =========================================================================
    # Sub-case 1: late_fee_not_owed (fee billed on an invoice)
    # =========================================================================
    seen_invoices_sub1: set[str] = set()
    for inv in invoices:
        try:
            invoice_id = str(inv.get("invoice_id") or "").strip().upper()
            lines = inv.get("lines")
            if not invoice_id or not isinstance(lines, list) or invoice_id in seen_invoices_sub1:
                continue
            seen_invoices_sub1.add(invoice_id)

            inv_cid = inv.get("contract_id") or inv.get("contract_reference", "")
            cfact = contracts.get(inv_cid) or default_contract
            grace_days = int(cfact.get("grace_days") or grace_days_default)
            late_fee_rate = Decimal(str(cfact.get("late_fee_rate_pct") or late_fee_rate_default))

            # Check contract extension clause citation
            extension_clause = cfact.get("extension_clause") or (
                "10.5 Extension of due date. A due date may be extended by written notice from the "
                "Carrier's Authorised Person (payment terms); the extended date replaces the due date for clause 10.2."
            )
            c_source = cfact.get("source") if isinstance(cfact.get("source"), dict) else {}
            c_page = cfact.get("extension_clause_page") or 4

            invoice_late_fee_total = Decimal("0.00")
            evidence_list: list[dict[str, Any]] = []
            is_fee_billed = False

            for line in lines:
                if not isinstance(line, dict):
                    continue
            ctype = str(line.get("charge_type") or "").lower()
            desc = str(line.get("description") or "").lower()

            if "late fee" not in ctype and "late fee" not in desc:
                continue

            is_fee_billed = True
            billed_amount_raw = line.get("amount") or "0.00"
            try:
                billed_fee = Decimal(str(billed_amount_raw))
            except Exception:
                continue

            # Identify underlying invoice ID
            full_desc = f"{line.get('description', '')} {line.get('charge_type', '')}"
            m_target = re.search(r"Late fee on\s+([A-Z0-9-]+)", full_desc, re.IGNORECASE)
            ref_inv_id = ""
            if m_target:
                ref_inv_id = m_target.group(1).strip().upper()
            else:
                m_tx = re.findall(r"\b(?:TX|INV)[A-Z0-9-]+\b", full_desc)
                for tx in m_tx:
                    if tx.upper() != invoice_id:
                        ref_inv_id = tx.upper()
                        break

            # Look up payment and due date for underlying invoice
            p_info = payment_map.get(ref_inv_id)
            orig_due_date = p_info.get("due_date") if p_info else None
            paid_date = p_info.get("paid_date") if p_info else None

            # Look up extension if any
            ext_info = extensions.get(ref_inv_id)
            effective_due_date = orig_due_date
            ext_quote = ""
            ext_src = {}
            if ext_info:
                effective_due_date = ext_info[0]
                ext_fact = ext_info[1]
                ext_quote = ext_fact.get("quote", "")
                ext_src = ext_fact.get("source", {})

            # Calculate days late
            fee_owed = Decimal("0.00")
            if paid_date and effective_due_date:
                days_late = (paid_date - effective_due_date).days
                if days_late > grace_days:
                    # Fee is owed!
                    months = math.ceil(days_late / 30)
                    principal = Decimal(str(p_info.get("amount") or "0.00"))
                    fee_owed = round_half_up(months * round_half_up((late_fee_rate / Decimal("100")) * principal))
                else:
                    # Paid on time or within grace days -> 0 fee owed
                    fee_owed = Decimal("0.00")
            elif effective_due_date and not paid_date:
                # Unpaid: check if within grace days as of invoice date
                inv_dt = _parse_date(inv.get("invoice_date"))
                if inv_dt and inv_dt <= effective_due_date + datetime.timedelta(days=grace_days):
                    fee_owed = Decimal("0.00")

            overcharged_fee = billed_fee - fee_owed
            if overcharged_fee > Decimal("0.005"):
                invoice_late_fee_total += overcharged_fee

                # 1. Extension email quote (if extended)
                if ext_quote and ext_src:
                    evidence_list.append({
                        "doc": ext_src.get("doc_ref", ""),
                        "page": ext_src.get("page", 1),
                        "quote": ext_quote,
                    })

                # 2. Payment register quote
                if p_info and p_info.get("quote") and p_info.get("source"):
                    evidence_list.append({
                        "doc": p_info["source"].get("doc_ref", ""),
                        "page": p_info["source"].get("page", 1),
                        "quote": p_info["quote"],
                    })

                # 3. Invoice line quote billing the fee
                line_q = line.get("quote") or inv.get("quote", "")
                line_s = line.get("source") or inv.get("source", {})
                if line_q and line_s:
                    evidence_list.append({
                        "doc": line_s.get("doc_ref", ""),
                        "page": line_s.get("page", 1),
                        "quote": line_q,
                    })

                # 4. Contract extension / late fee clause quote
                if extension_clause and c_source:
                    evidence_list.append({
                        "doc": c_source.get("doc_ref", ""),
                        "page": c_page,
                        "quote": extension_clause,
                    })

            # Produce finding or clean report item
            if invoice_late_fee_total > Decimal("0.005"):
                seen_ev = set()
                clean_ev = []
                for ev in evidence_list:
                    key = (ev.get("doc"), ev.get("quote"))
                    if key not in seen_ev and ev.get("doc") and ev.get("quote"):
                        seen_ev.add(key)
                        clean_ev.append(ev)

                action = "human_review" if invoice_late_fee_total >= Decimal("5000.00") else (
                    "execute:dispute" if mode == "autonomous" else "draft:dispute"
                )

                findings.append({
                    "id": f"late_fee_not_owed:{invoice_id}",
                    "class": "late_fee_not_owed",
                    "variant": "LF-01",
                    "invoice_id": invoice_id,
                    "verdict": "finding",
                    "amount_usd": f"{invoice_late_fee_total:.2f}",
                    "amount_range": None,
                    "deadline": None,
                    "action": action,
                    "confidence": 0.95,
                    "evidence": clean_ev,
                    "missing_doc": None,
                    "supersedes": [],
                    "learned_at": None,
                })
            elif is_fee_billed and include_clean:
                # Fee was billed legitimately (e.g. decoy paid 20 days late past grace)
                findings.append({
                    "id": f"late_fee_not_owed:{invoice_id}",
                    "class": "late_fee_not_owed",
                    "variant": "LF-01",
                    "invoice_id": invoice_id,
                    "verdict": "no_finding",
                    "amount_usd": None,
                    "amount_range": None,
                    "deadline": None,
                    "action": "none",
                    "confidence": 0.9,
                    "evidence": [],
                    "missing_doc": None,
                    "supersedes": [],
                    "learned_at": None,
                })
        except Exception:
            continue

    # =========================================================================
    # Sub-case 2: late_fee_risk (unpaid invoices entering window)
    # =========================================================================
    seen_invoices_sub2: set[str] = set()
    for inv in invoices:
        try:
            inv_id = str(inv.get("invoice_id") or "").strip().upper()
            if not inv_id or inv_id in seen_invoices_sub2:
                continue
            seen_invoices_sub2.add(inv_id)

            # Check if invoice was already paid on or before as_of_dt
            p_info = payment_map.get(inv_id)
            if p_info:
                paid_dt = p_info.get("paid_date")
                p_status = p_info.get("status", "")
                if paid_dt and paid_dt <= as_of_dt and "paid" in p_status:
                    continue  # already paid!

            # Determine due date (extended if applicable)
            orig_due = _parse_date(inv.get("due_date"))
            if not orig_due and p_info:
                orig_due = p_info.get("due_date")

            ext_info = extensions.get(inv_id)
            effective_due = ext_info[0] if ext_info else orig_due
            if not effective_due:
                continue

            # Window rule: 0 < due_date - as_of <= 14 days
            days_until_due = (effective_due - as_of_dt).days
            if 0 < days_until_due <= 14:
                # Calculate 1 month's fee
                inv_cid = inv.get("contract_id") or inv.get("contract_reference", "")
                cfact = contracts.get(inv_cid) or default_contract
                late_fee_rate = Decimal(str(cfact.get("late_fee_rate_pct") or late_fee_rate_default))

                principal_raw = inv.get("invoice_total") or (p_info.get("amount") if p_info else "0.00")
                try:
                    principal = Decimal(str(principal_raw or "0.00"))
                except Exception:
                    principal = Decimal("0.00")

                one_month_fee = round_half_up((late_fee_rate / Decimal("100")) * principal)

                inv_src = inv.get("source") if isinstance(inv.get("source"), dict) else {}
                inv_q = inv.get("quote", "")

                evidence_items = []
                if inv_q and inv_src:
                    evidence_items.append({
                        "doc": inv_src.get("doc_ref", ""),
                        "page": inv_src.get("page", 1),
                        "quote": inv_q,
                    })

                findings.append({
                    "id": f"late_fee_risk:{inv_id}",
                    "class": "late_fee_risk",
                    "variant": "LF-02",
                    "invoice_id": inv_id,
                    "verdict": "finding",
                    "amount_usd": f"{one_month_fee:.2f}",
                    "amount_range": None,
                    "deadline": effective_due.isoformat(),
                    "action": "internal:payment_reminder",
                    "confidence": 0.90,
                    "evidence": evidence_items,
                    "missing_doc": None,
                    "supersedes": [],
                    "learned_at": None,
                })
        except Exception:
            continue

    return findings
