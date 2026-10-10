"""Waived but billed detector for charges waived by the carrier but billed anyway.

Rules (GLOSSARY.md & EXPLANATION.md):
- A charge may be waived only in writing by a person listed in Schedule 1 for waivers (or feedback).
- A waiver applies only to the charges and shipments it names (one-off vs standing).
- A one-off waiver for shipment X does NOT apply to shipment Y.
- Treating a one-off waiver as permanent triggers the permanent_waiver hard gate.
- The waiver date must be on or before the invoice date.
- The waiver document must be received on or before as_of_date.
- Multiple waived lines on one invoice add up to one finding keyed by (invoice_id, 'waived_but_billed').
- Clean invoices / non-matching waivers are no_finding (not flagged).
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


def _clean_str(s: Any) -> str:
    return str(s or "").strip().lower()


def check_waived_but_billed(
    facts: list[dict[str, Any]],
    as_of_date: str | datetime.date,
    mode: str = "supervised",
    include_clean: bool = False,
) -> list[dict[str, Any]]:
    """Detect invoices containing charges that were previously waived in writing.

    Parameters
    ----------
    facts : list of extracted facts
    as_of_date : str or datetime.date
        Only documents received on or before as_of count.
    mode : "supervised" or "autonomous"
    include_clean : bool, default False
        If True, also return clean invoices with verdict='no_finding'.

    Returns
    -------
    list of invoice finding dicts matching grader schema.
    """
    as_of_dt = _parse_date(as_of_date)
    if as_of_dt is None:
        return []

    # 1. Collect contracts, Schedule 1 contacts, and authority clauses
    contracts: dict[str, dict[str, Any]] = {}
    authorized_senders: dict[str, dict[str, Any]] = {}
    waiver_clauses: dict[str, tuple[str, dict[str, Any], int]] = {}

    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "contract_terms":
            cid = f.get("contract_id", "")
            if cid:
                contracts[cid] = f

            c_source = f.get("source") if isinstance(f.get("source"), dict) else {}
            w_clause = f.get("waiver_clause")
            if w_clause:
                w_page = f.get("waiver_clause_page") or c_source.get("page", 4)
                waiver_clauses[cid] = (w_clause, c_source, w_page)

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
                    role = str(contact.get("role") or "").lower()
                    if "waivers" in cats or "waiver" in cats or "waiver" in role:
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
                            authorized_senders[email] = entry
                        if name:
                            authorized_senders[name] = entry

    # Also check feedback facts for confirmed sender authority
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "feedback_fact" or f.get("type") == "sender_authority":
            val = f.get("value")
            if not isinstance(val, dict):
                continue
            raw_cats = val.get("categories")
            if isinstance(raw_cats, list):
                cats = [str(c).lower() for c in raw_cats if c is not None]
            elif isinstance(raw_cats, str):
                cats = [raw_cats.lower()]
            else:
                cats = []
            if "waivers" in cats or "waiver" in cats:
                subj = f.get("subject") if isinstance(f.get("subject"), dict) else {}
                p_name = _clean_str(subj.get("person") or subj.get("name"))
                p_email = _clean_str(subj.get("email"))
                scope_dict = f.get("scope") if isinstance(f.get("scope"), dict) else {}
                applies_to = scope_dict.get("applies_to") if isinstance(scope_dict.get("applies_to"), dict) else {}
                fb_entry = {
                    "contact": subj,
                    "contract_id": applies_to.get("contract_id", ""),
                    "source": {"doc_ref": f.get("evidence", "feedback.json"), "page": 1},
                    "quote": f.get("evidence", "Carrier delegation letter"),
                    "page": 1,
                }
                if p_name:
                    authorized_senders[p_name] = fb_entry
                if p_email:
                    authorized_senders[p_email] = fb_entry

    # 2. Collect waiver email facts received on or before as_of_dt
    waivers: list[dict[str, Any]] = []
    for f in facts:
        if f.get("fact_type") == "email_event":
            is_waiver = (f.get("event_type") == "waiver") or ("waive" in (f.get("quote") or "").lower())
            if not is_waiver:
                continue

            doc_date = _parse_date(f.get("email_date") or f.get("document_date"))
            if doc_date is not None and doc_date > as_of_dt:
                continue  # not received yet as of report date

            waivers.append(f)

    # 3. Collect invoices received on or before as_of_dt
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
    seen_invoices: set[str] = set()

    for inv in invoices:
        invoice_id = str(inv.get("invoice_id") or "")
        lines = inv.get("lines")
        if not invoice_id or not isinstance(lines, list) or invoice_id in seen_invoices:
            continue
        seen_invoices.add(invoice_id)

        inv_date = _parse_date(inv.get("invoice_date"))
        inv_contract_id = inv.get("contract_id") or inv.get("contract_reference", "")
        inv_currency = str(inv.get("invoice_currency") or "USD").upper()

        invoice_waived_total = Decimal("0.00")
        evidence_list: list[dict[str, Any]] = []
        matched_any_waiver = False

        # Evidence tracking for clean decoy
        decoy_evidence: list[dict[str, Any]] = []

        for line in lines:
            if not isinstance(line, dict):
                continue
            line_amount_raw = line.get("amount") or "0.00"
            try:
                line_amt = Decimal(str(line_amount_raw))
            except (ValueError, TypeError, ArithmeticError):
                continue

            line_ctype = str(line.get("charge_type") or line.get("description") or "").lower()
            line_shp = str(line.get("shipment_id") or line.get("awb") or "").strip()
            line_quote = line.get("quote") or inv.get("quote", "")
            line_source = line.get("source") if isinstance(line.get("source"), dict) else (inv.get("source", {}) if isinstance(inv.get("source"), dict) else {})

            # Search for a matching waiver
            matched_waiver: dict[str, Any] | None = None
            auth_entry: dict[str, Any] | None = None

            for w in waivers:
                w_date = _parse_date(w.get("email_date") or w.get("document_date"))
                # Rule: waiver date must be before or on invoice date
                if inv_date is not None and w_date is not None and w_date > inv_date:
                    continue

                # Check sender authority
                sender_email = _clean_str(w.get("sender_email"))
                sender_name = _clean_str(w.get("sender_name"))
                w_auth = None

                for k, a_data in authorized_senders.items():
                    if k and (k in sender_email or sender_email in k or k in sender_name or sender_name in k):
                        w_auth = a_data
                        break

                if not w_auth:
                    # If sender is not authorized per Schedule 1, waiver does not bind
                    continue

                # Check waiver scope and charge matching
                w_scope = w.get("waiver_scope")
                if not isinstance(w_scope, dict):
                    w_scope = {}
                scope_type = str(w_scope.get("type") or "single_shipment")
                raw_sids = w_scope.get("shipment_ids") or []
                w_shipment_ids = [str(sid).strip().upper() for sid in (raw_sids if isinstance(raw_sids, list) else [raw_sids])]
                raw_cts = w_scope.get("charge_types") or []
                w_charge_types = [str(ct).strip().lower() for ct in (raw_cts if isinstance(raw_cts, list) else [raw_cts])]
                w_quote = str(w.get("quote") or "").lower()

                # Also extract any shipment IDs mentioned in waiver quote
                quote_shps = re.findall(r"\b(?:TE|AWB|SHP)[A-Z0-9-]+\b", str(w.get("quote") or "").upper())
                quote_shps_clean = [s.replace("AWB", "").strip() for s in quote_shps]
                all_target_shps = set(w_shipment_ids) | set(quote_shps) | set(quote_shps_clean)

                # Check charge type match
                charge_type_matches = False
                if w_charge_types:
                    charge_type_matches = any(ct in line_ctype for ct in w_charge_types)
                else:
                    # Check if line charge type words appear in waiver quote
                    clean_words = [wrd for wrd in re.split(r"\s+", line_ctype) if len(wrd) > 3]
                    charge_type_matches = any(wrd in w_quote for wrd in clean_words) or ("waive" in w_quote)

                if not charge_type_matches:
                    continue

                # Scope evaluation:
                # If waiver targets specific shipment(s): MUST match this line's shipment
                if all_target_shps:
                    line_shp_upper = line_shp.upper()
                    if line_shp_upper not in all_target_shps and not any(sid in line_shp_upper for sid in all_target_shps):
                        # Named a different shipment -> cannot waive this shipment!
                        continue

                elif scope_type == "single_shipment":
                    # Single shipment waiver with no matching ID cannot apply
                    continue

                elif scope_type in ("standing", "month"):
                    # Standing or monthly waiver: matches charge type
                    pass

                # If we get here, valid waiver matches this line!
                matched_waiver = w
                auth_entry = w_auth
                break

            if matched_waiver is not None and auth_entry is not None:
                matched_any_waiver = True
                invoice_waived_total += line_amt

                # 1. Quote from waiver email
                w_source = matched_waiver.get("source", {})
                evidence_list.append({
                    "doc": w_source.get("doc_ref", ""),
                    "page": w_source.get("page", 1),
                    "quote": matched_waiver.get("quote", ""),
                })

                # 2. Quote from Schedule 1 contact showing authority
                auth_source = auth_entry.get("source", {})
                evidence_list.append({
                    "doc": auth_source.get("doc_ref", ""),
                    "page": auth_entry.get("page", 11),
                    "quote": auth_entry.get("quote", ""),
                })

                # 3. Quote from invoice line
                if line_quote and line_source:
                    evidence_list.append({
                        "doc": line_source.get("doc_ref", ""),
                        "page": line_source.get("page", 1),
                        "quote": line_quote,
                    })

                # 4. Waiver authority clause from contract
                cid = auth_entry.get("contract_id") or inv_contract_id
                clause_info = waiver_clauses.get(cid)
                if not clause_info and waiver_clauses:
                    clause_info = next(iter(waiver_clauses.values()))

                if clause_info:
                    c_clause, c_src, c_page = clause_info
                    evidence_list.append({
                        "doc": c_src.get("doc_ref", ""),
                        "page": c_page,
                        "quote": c_clause,
                    })

            elif include_clean:
                # Capture evidence for look-alike decoy
                if line_quote and line_source:
                    decoy_evidence.append({
                        "doc": line_source.get("doc_ref", ""),
                        "page": line_source.get("page", 1),
                        "quote": line_quote,
                    })

        # Determine remedy action
        if invoice_waived_total >= Decimal("5000.00"):
            action = "human_review"
        elif mode == "autonomous":
            action = "execute:dispute"
        else:
            action = "draft:dispute"

        if invoice_waived_total > Decimal("0.005"):
            # Deduplicate evidence citations by (doc, quote) preserving order
            seen_ev = set()
            clean_evidence = []
            for ev in evidence_list:
                key = (ev.get("doc"), ev.get("quote"))
                if key not in seen_ev and ev.get("doc") and ev.get("quote"):
                    seen_ev.add(key)
                    clean_evidence.append(ev)

            # FX conversion if INR
            amount_final = invoice_waived_total
            if inv_currency == "INR":
                inv_month = inv.get("invoice_date", "")[:7]
                fx_rate = None
                cfact = contracts.get(inv_contract_id) or (next(iter(contracts.values())) if contracts else None)
                if cfact:
                    for fx_entry in cfact.get("fx_rates", []):
                        if fx_entry.get("month") == inv_month:
                            try:
                                fx_rate = Decimal(str(fx_entry.get("inr_per_usd")))
                                break
                            except Exception:
                                pass
                if fx_rate and fx_rate > Decimal("0.00"):
                    amount_final = round_half_up(amount_final / fx_rate)

            findings.append({
                "id": f"waived_but_billed:{invoice_id}",
                "class": "waived_but_billed",
                "variant": "WB-01",
                "invoice_id": invoice_id,
                "verdict": "finding",
                "amount_usd": f"{amount_final:.2f}",
                "amount_range": None,
                "deadline": None,
                "action": action,
                "confidence": 0.95,
                "evidence": clean_evidence,
                "missing_doc": None,
                "supersedes": [],
                "learned_at": None,
            })

        elif include_clean:
            seen_dev = set()
            clean_decoy = []
            for ev in decoy_evidence:
                key = (ev.get("doc"), ev.get("quote"))
                if key not in seen_dev and ev.get("doc") and ev.get("quote"):
                    seen_dev.add(key)
                    clean_decoy.append(ev)

            findings.append({
                "id": f"waived_but_billed:{invoice_id}",
                "class": "waived_but_billed",
                "variant": "WB-01",
                "invoice_id": invoice_id,
                "verdict": "no_finding",
                "amount_usd": None,
                "amount_range": None,
                "deadline": None,
                "action": "none",
                "confidence": 0.9,
                "evidence": clean_decoy,
                "missing_doc": None,
                "supersedes": [],
                "learned_at": None,
            })

    return findings
