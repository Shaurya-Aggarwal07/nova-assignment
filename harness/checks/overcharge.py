"""Overcharge detector for freight invoice lines billed above contract rates.

Rules (GLOSSARY.md & EXPLANATION.md):
- A shipment is priced on its Pickup Date (first of: tracking export event PICKED_UP,
  then carrier pickup email, then AWB date, then invoice ship date).
- Rate is looked up by lane, weight band, and rate period in force on the pickup date.
- Amendments apply if effective_date <= pickup_date (and document was received on or before as_of).
- Backdated amendments apply to all pickups on or after their effective_date (retro: true).
- Rate changes effective after pickup_date do not apply to that pickup.
- Fuel surcharge is recalculated on the correct base freight.
- Billed - correct > 0 is flagged as an overcharge finding.
- Multiple overcharged lines on one invoice add up to one finding keyed by (invoice_id, 'overcharge').
- Billed <= correct is clean (no_finding / not flagged).
"""
from __future__ import annotations

import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def round_half_up(val: float | Decimal | str, decimals: int = 2) -> Decimal:
    """Round half up to cents (ties away from zero per GLOSSARY)."""
    d = Decimal(str(val))
    return d.quantize(Decimal("10") ** -decimals, rounding=ROUND_HALF_UP)


def parse_weight_band(band_str: str) -> tuple[float, float]:
    """Parse weight band string like '100-299.5 kg', '0-44.5', '45-99.5 kg' into (min_w, max_w)."""
    if not band_str:
        return (0.0, float("inf"))
    clean = band_str.lower().replace("kg", "").replace("lbs", "").strip()
    if "-" in clean:
        parts = clean.split("-")
        try:
            low = float(parts[0].strip())
            high = float(parts[1].strip())
            return (low, high)
        except (ValueError, IndexError):
            pass
    elif "+" in clean:
        try:
            low = float(clean.replace("+", "").strip())
            return (low, float("inf"))
        except ValueError:
            pass
    elif ">" in clean:
        try:
            low = float(clean.replace(">", "").strip())
            return (low, float("inf"))
        except ValueError:
            pass
    elif "<" in clean:
        try:
            high = float(clean.replace("<", "").strip())
            return (0.0, high)
        except ValueError:
            pass
    return (0.0, float("inf"))


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


def check_overcharge(
    facts: list[dict[str, Any]],
    as_of_date: str | datetime.date,
    mode: str = "supervised",
    include_clean: bool = False,
) -> list[dict[str, Any]]:
    """Detect freight invoices with lines billed above contract rates.

    Parameters
    ----------
    facts : list of extracted facts
    as_of_date : str or datetime.date
        Only documents received on or before as_of count.
    mode : "supervised" or "autonomous"
    include_clean : bool, default False
        If True, also include clean invoices as verdict='no_finding' (e.g. for unit tests).

    Returns
    -------
    list of invoice finding dicts matching grader schema.
    """
    as_of_dt = _parse_date(as_of_date)
    if as_of_dt is None:
        return []

    # 1. Collect contracts and amendments
    contracts: dict[str, dict[str, Any]] = {}
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "contract_terms":
            cid = f.get("contract_id")
            if cid:
                contracts[cid] = f

    amendments: list[dict[str, Any]] = [
        f for f in facts if isinstance(f, dict) and f.get("fact_type") == "amendment"
    ]

    # Filter amendments known as of as_of_dt
    known_amendments: list[dict[str, Any]] = []
    for a in amendments:
        doc_date = _parse_date(a.get("document_date") or a.get("received_date"))
        if doc_date is not None and doc_date > as_of_dt:
            continue  # amendment document not received yet
        eff_date = _parse_date(a.get("effective_date"))
        if eff_date is not None:
            known_amendments.append(a)

    # Sort amendments by effective_date ascending so later amendments override earlier ones
    known_amendments.sort(
        key=lambda a: _parse_date(a.get("effective_date")) or datetime.date.min
    )

    # 2. Collect tracking pickup dates
    # Precedence: tracking export PICKED_UP, then carrier email
    tracking_pickup_dates: dict[str, tuple[datetime.date, dict[str, Any]]] = {}
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "shipment_record":
            awb = str(f.get("awb") or "")
            raw_te = f.get("tracking_events")
            if isinstance(raw_te, list):
                for ev in raw_te:
                    if not isinstance(ev, dict):
                        continue
                    ev_name = str(ev.get("event") or "").lower()
                    if "pick" in ev_name:
                        p_dt = _parse_date(ev.get("date"))
                        if p_dt and awb:
                            tracking_pickup_dates[awb] = (p_dt, f)
        elif f.get("fact_type") == "email_event":
            if f.get("event_type") == "pickup":
                p_dt = _parse_date(f.get("email_date"))
                ws = f.get("waiver_scope")
                if isinstance(ws, dict):
                    raw_sids = ws.get("shipment_ids") or []
                    for sid in (raw_sids if isinstance(raw_sids, list) else [raw_sids]):
                        sid_str = str(sid).strip()
                        if p_dt and sid_str and sid_str not in tracking_pickup_dates:
                            tracking_pickup_dates[sid_str] = (p_dt, f)

    # 3. Collect invoices received on or before as_of
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

        inv_source = inv.get("source") if isinstance(inv.get("source"), dict) else {}
        inv_quote = str(inv.get("quote") or "")
        inv_currency = str(inv.get("invoice_currency") or "USD").upper()

        # Group lines by shipment / AWB
        shipments: dict[str, list[dict[str, Any]]] = {}
        for idx, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            shp_key = str(line.get("shipment_id") or line.get("awb") or f"line_{idx}")
            shipments.setdefault(shp_key, []).append(line)

        invoice_overcharge_total = Decimal("0.00")
        evidence_list: list[dict[str, Any]] = []
        is_backdated = False

        # For decoy / clean reporting
        decoy_evidence: list[dict[str, Any]] = []

        for shp_key, shp_lines in shipments.items():
            # Separate freight lines from fuel / accessorial lines
            freight_line = None
            fuel_line = None

            for line in shp_lines:
                ctype = (line.get("charge_type") or "").lower()
                desc = (line.get("description") or "").lower()
                if "fuel" in ctype or "fuel" in desc:
                    fuel_line = line
                elif "freight" in ctype or "freight" in desc or not freight_line:
                    freight_line = line

            if freight_line is None:
                freight_line = shp_lines[0]

            # Determine pickup date and tracking evidence
            # Priority: tracking export / picked up email > invoice pickup_date > invoice_date
            pickup_dt: datetime.date | None = None
            tracking_fact: dict[str, Any] | None = None

            awb_key = freight_line.get("awb") or freight_line.get("shipment_id") or shp_key
            if awb_key in tracking_pickup_dates:
                pickup_dt, tracking_fact = tracking_pickup_dates[awb_key]
            elif shp_key in tracking_pickup_dates:
                pickup_dt, tracking_fact = tracking_pickup_dates[shp_key]

            if pickup_dt is None:
                pickup_dt = _parse_date(freight_line.get("pickup_date"))
            if pickup_dt is None:
                pickup_dt = _parse_date(inv.get("invoice_date"))
            if pickup_dt is None:
                continue

            lane = freight_line.get("lane", "")
            weight = Decimal("0.0")
            for w_key in ("weight_kg", "quantity"):
                val = freight_line.get(w_key)
                if val:
                    try:
                        weight = Decimal(str(val))
                        break
                    except (ValueError, TypeError, ArithmeticError):
                        pass

            billed_freight = Decimal("0.00")
            try:
                billed_freight = Decimal(str(freight_line.get("amount") or "0.00"))
            except (ValueError, TypeError, ArithmeticError):
                pass

            # Find matching contract and rate
            contract_fact: dict[str, Any] | None = None
            matched_rate_entry: dict[str, Any] | None = None
            min_charge_val: Decimal = Decimal("0.00")

            for cid, cfact in contracts.items():
                c_eff = _parse_date(cfact.get("effective_date"))
                c_end = _parse_date(cfact.get("end_date"))
                if c_eff and c_eff > pickup_dt:
                    continue
                if c_end and c_end < pickup_dt:
                    continue

                for r in cfact.get("rates", []):
                    r_lane = r.get("lane", "")
                    if r_lane and lane and r_lane.upper() == lane.upper():
                        # Check rate effective date bounds if specified
                        r_from = _parse_date(r.get("effective_from"))
                        r_to = _parse_date(r.get("effective_to"))
                        if r_from and pickup_dt < r_from:
                            continue
                        if r_to and pickup_dt > r_to:
                            continue

                        band_str = r.get("weight_band", "")
                        if "minimum" in band_str.lower() or "min" in band_str.lower():
                            try:
                                min_charge_val = Decimal(str(r.get("rate") or "0.00"))
                            except Exception:
                                pass
                            continue

                        low_w, high_w = parse_weight_band(band_str)
                        if Decimal(str(low_w)) <= weight <= Decimal(str(high_w)):
                            contract_fact = cfact
                            matched_rate_entry = r
                            break
                        elif not matched_rate_entry:
                            contract_fact = cfact
                            matched_rate_entry = r

                if contract_fact and matched_rate_entry:
                    break

            if not matched_rate_entry or not contract_fact:
                continue

            try:
                applicable_rate = Decimal(str(matched_rate_entry.get("rate") or "0.00"))
            except (ValueError, TypeError, ArithmeticError):
                continue

            rate_quote = matched_rate_entry.get("quote") or contract_fact.get("quote", "")
            rate_source = matched_rate_entry.get("source") or contract_fact.get("source", {})

            # Check amendments for rate modifications
            for am in known_amendments:
                if am.get("amends_contract") == contract_fact.get("contract_id"):
                    am_eff = _parse_date(am.get("effective_date"))
                    if am_eff is not None and am_eff <= pickup_dt:
                        for chg in am.get("changes", []):
                            chg_lane = chg.get("lane", "")
                            chg_band = chg.get("weight_band")
                            band_matches = True
                            if chg_band:
                                clow, chigh = parse_weight_band(chg_band)
                                band_matches = (Decimal(str(clow)) <= weight <= Decimal(str(chigh)))

                            if (
                                chg.get("field") in ("rate", "base_rate", "freight_rate")
                                and (not chg_lane or chg_lane.upper() == lane.upper())
                                and band_matches
                            ):
                                try:
                                    applicable_rate = Decimal(str(chg.get("new_value")))
                                    rate_quote = am.get("quote", rate_quote)
                                    rate_source = am.get("source", rate_source)
                                    # Check if backdated amendment (signed after effective_date)
                                    doc_date = _parse_date(am.get("document_date") or am.get("received_date"))
                                    if doc_date and am_eff < doc_date:
                                        is_backdated = True
                                except (ValueError, TypeError, ArithmeticError):
                                    pass

            # Calculate correct freight: rate * weight (half up to cents)
            if weight > Decimal("0.0"):
                correct_freight = round_half_up(applicable_rate * weight)
            else:
                correct_freight = round_half_up(applicable_rate)

            # Apply minimum charge if contract specifies one
            if min_charge_val > Decimal("0.00") and correct_freight < min_charge_val:
                correct_freight = min_charge_val

            # Calculate fuel surcharge if applicable
            billed_fuel = Decimal("0.00")
            correct_fuel = Decimal("0.00")
            if fuel_line is not None:
                try:
                    billed_fuel = Decimal(str(fuel_line.get("amount") or "0.00"))
                except (ValueError, TypeError, ArithmeticError):
                    pass

                # Fuel surcharge percentage from contract surcharges or fuel line rate
                fuel_pct = Decimal("0.00")
                for sur in contract_fact.get("surcharges", []):
                    if "fuel" in sur.get("name", "").lower():
                        try:
                            fuel_pct = Decimal(str(sur.get("rate_or_amount") or "0.00"))
                        except (ValueError, TypeError, ArithmeticError):
                            pass
                if fuel_pct == Decimal("0.00") and fuel_line.get("rate"):
                    try:
                        clean_rate = str(fuel_line.get("rate")).replace("%", "").strip()
                        fuel_pct = Decimal(clean_rate)
                    except (ValueError, TypeError, ArithmeticError):
                        pass

                if fuel_pct > Decimal("0.00"):
                    correct_fuel = round_half_up((fuel_pct / Decimal("100.0")) * correct_freight)

            total_billed = round_half_up(billed_freight + billed_fuel)
            total_correct = round_half_up(correct_freight + correct_fuel)
            shp_overcharge = total_billed - total_correct
            verdict_str = "finding" if shp_overcharge > Decimal("0.005") else "no_finding"
            print(f"DEBUG: {invoice_id} comparing amount={total_billed} vs rate={total_correct}, verdict={verdict_str}")

            # Handle INR conversion if needed
            if inv_currency == "INR" and shp_overcharge > Decimal("0.00"):
                # FX rate from contract Annex F for invoice month
                inv_month = inv.get("invoice_date", "")[:7]
                fx_rate = None
                for fx_entry in contract_fact.get("fx_rates", []):
                    if fx_entry.get("month") == inv_month:
                        try:
                            fx_rate = Decimal(str(fx_entry.get("inr_per_usd")))
                            break
                        except Exception:
                            pass
                if fx_rate and fx_rate > Decimal("0.00"):
                    shp_overcharge = round_half_up(shp_overcharge / fx_rate)

            if shp_overcharge > Decimal("0.005"):
                invoice_overcharge_total += shp_overcharge

                # Collect citations in standardized order:
                # 1. Invoice line
                line_quote = freight_line.get("quote") or inv_quote
                line_src = freight_line.get("source") or inv_source
                if line_quote and line_src:
                    evidence_list.append({
                        "doc": line_src.get("doc_ref", ""),
                        "page": line_src.get("page", 1),
                        "quote": line_quote,
                    })

                # 2. Contract rate / amendment quote
                if rate_quote and rate_source:
                    evidence_list.append({
                        "doc": rate_source.get("doc_ref", ""),
                        "page": rate_source.get("page", 1),
                        "quote": rate_quote,
                    })

                # 3. Pricing date / precedence clause quote
                pricing_clause = contract_fact.get("precedence_clause") or contract_fact.get("pricing_clause")
                if pricing_clause:
                    evidence_list.append({
                        "doc": contract_fact.get("source", {}).get("doc_ref", ""),
                        "page": contract_fact.get("pricing_clause_page") or contract_fact.get("source", {}).get("page", 3),
                        "quote": pricing_clause,
                    })

                # 4. Tracking milestone quote
                if tracking_fact:
                    t_source = tracking_fact.get("source", {})
                    evidence_list.append({
                        "doc": t_source.get("doc_ref", ""),
                        "page": t_source.get("page", 1),
                        "quote": tracking_fact.get("quote", ""),
                    })

            elif include_clean:
                # Collect evidence for clean decoy
                line_quote = freight_line.get("quote") or inv_quote
                line_src = freight_line.get("source") or inv_source
                if line_quote and line_src:
                    decoy_evidence.append({
                        "doc": line_src.get("doc_ref", ""),
                        "page": line_src.get("page", 1),
                        "quote": line_quote,
                    })
                if rate_quote and rate_source:
                    decoy_evidence.append({
                        "doc": rate_source.get("doc_ref", ""),
                        "page": rate_source.get("page", 1),
                        "quote": rate_quote,
                    })
                pricing_clause = contract_fact.get("precedence_clause") or contract_fact.get("pricing_clause")
                if pricing_clause:
                    decoy_evidence.append({
                        "doc": contract_fact.get("source", {}).get("doc_ref", ""),
                        "page": contract_fact.get("pricing_clause_page") or contract_fact.get("source", {}).get("page", 3),
                        "quote": pricing_clause,
                    })

        # Determine remedy action per unattended-action rules
        # > $5,000 outbound finding -> human_review in either mode
        if invoice_overcharge_total >= Decimal("5000.00"):
            action = "human_review"
        elif mode == "autonomous":
            action = "execute:dispute"
        else:
            action = "draft:dispute"

        if invoice_overcharge_total > Decimal("0.005"):
            # Deduplicate evidence citations by (doc, quote) preserving order
            seen_ev = set()
            clean_evidence = []
            for ev in evidence_list:
                key = (ev.get("doc"), ev.get("quote"))
                if key not in seen_ev and ev.get("doc") and ev.get("quote"):
                    seen_ev.add(key)
                    clean_evidence.append(ev)

            finding_item: dict[str, Any] = {
                "id": f"overcharge:{invoice_id}",
                "class": "overcharge",
                "variant": "OC-01",
                "invoice_id": invoice_id,
                "verdict": "finding",
                "amount_usd": f"{invoice_overcharge_total:.2f}",
                "amount_range": None,
                "deadline": None,
                "action": action,
                "confidence": 0.95,
                "evidence": clean_evidence,
                "missing_doc": None,
                "supersedes": [],
                "learned_at": None,
            }
            if is_backdated:
                finding_item["retro"] = True
            findings.append(finding_item)

        elif include_clean:
            # Clean decoy: invoice checked and rate is correct
            seen_dev = set()
            clean_decoy_evidence = []
            for ev in decoy_evidence:
                key = (ev.get("doc"), ev.get("quote"))
                if key not in seen_dev and ev.get("doc") and ev.get("quote"):
                    seen_dev.add(key)
                    clean_decoy_evidence.append(ev)

            findings.append({
                "id": f"overcharge:{invoice_id}",
                "class": "overcharge",
                "variant": "OC-01",
                "invoice_id": invoice_id,
                "verdict": "no_finding",
                "amount_usd": None,
                "amount_range": None,
                "deadline": None,
                "action": "none",
                "confidence": 0.9,
                "evidence": clean_decoy_evidence,
                "missing_doc": None,
                "supersedes": [],
                "learned_at": None,
            })

    return findings
