"""Expiry detector for contracts approaching termination or notice deadlines.

Rule (GLOSSARY.md & EXPLANATION.md):
Report a contract when its operative date is 0 to 60 days after as_of:
    0 <= deadline - as_of <= 60
The operative date is the end date, or the non-renewal notice deadline
when the contract renews itself.
Action is internal:alert. Expiry carries no money (amount_usd is null).
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any


def check_expiry(
    facts: list[dict[str, Any]],
    as_of_date: str | datetime.date,
) -> list[dict[str, Any]]:
    """Detect contracts whose expiry or notice deadline is inside the 60-day window.

    Parameters
    ----------
    facts : list of fact dicts (e.g. loaded from state_dir)
    as_of_date : str or datetime.date
        The cutoff date for evaluation (YYYY-MM-DD).

    Returns
    -------
    list of portfolio.expiry items matching grader/schema/report.schema.json.
    """
    if isinstance(as_of_date, str):
        as_of_dt = datetime.date.fromisoformat(as_of_date)
    elif isinstance(as_of_date, datetime.datetime):
        as_of_dt = as_of_date.date()
    else:
        as_of_dt = as_of_date

    window_end = as_of_dt + datetime.timedelta(days=60)

    # 1. Collect contract_terms facts
    contracts: dict[str, dict[str, Any]] = {}
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("fact_type") == "contract_terms":
            cid = f.get("contract_id")
            if cid:
                contracts[cid] = f

    # 2. Collect amendment facts that took effect on or before as_of
    amendments = [
        f for f in facts
        if isinstance(f, dict) and f.get("fact_type") == "amendment"
    ]

    findings: list[dict[str, Any]] = []

    for contract_id, cfact in sorted(contracts.items()):
        try:
            source = cfact.get("source") if isinstance(cfact.get("source"), dict) else {}
            quote = str(cfact.get("quote") or "")
            auto_renew = bool(cfact.get("auto_renew", False))
            notice_days = cfact.get("notice_days")
            end_date_str = cfact.get("end_date")

            # Check for amendments extending the term
            for afact in amendments:
                if afact.get("amends_contract") == contract_id:
                    eff_str = afact.get("effective_date")
                    if eff_str:
                        try:
                            eff_dt = datetime.date.fromisoformat(str(eff_str)[:10])
                            if eff_dt <= as_of_dt:
                                raw_changes = afact.get("changes")
                                if isinstance(raw_changes, list):
                                    for chg in raw_changes:
                                        if not isinstance(chg, dict):
                                            continue
                                        if chg.get("field") in ("end_date", "term"):
                                            new_val = chg.get("new_value")
                                            if new_val:
                                                end_date_str = new_val
                                                quote = afact.get("quote", quote)
                                                source = afact.get("source", source)
                        except ValueError:
                            pass

            # Handle auto_renew: if auto_renew=true and no explicit end_date, no flag
            if not end_date_str:
                continue

            try:
                expiry_date = datetime.date.fromisoformat(str(end_date_str)[:10])
            except ValueError:
                continue

            # Calculate notice deadline if notice_days is specified
            notice_date: datetime.date | None = None
            if notice_days is not None:
                try:
                    nd_int = int(notice_days)
                    if nd_int > 0:
                        notice_date = expiry_date - datetime.timedelta(days=nd_int)
                except (ValueError, TypeError):
                    pass

            # Determine if expiry_date or notice_date falls in [as_of, as_of + 60 days]
            is_expiry_in_window = (as_of_dt <= expiry_date <= window_end)
            is_notice_in_window = (notice_date is not None) and (as_of_dt <= notice_date <= window_end)

            if not (is_expiry_in_window or is_notice_in_window):
                continue

            # Operative deadline rule:
            # If the contract renews automatically, the operative date is the notice deadline.
            # Otherwise, it is the contract end date.
            if auto_renew and is_notice_in_window and notice_date is not None:
                deadline = notice_date.isoformat()
            elif is_expiry_in_window:
                deadline = expiry_date.isoformat()
            elif is_notice_in_window and notice_date is not None:
                deadline = notice_date.isoformat()
            else:
                deadline = expiry_date.isoformat()

            # Build evidence
            evidence_doc = source.get("doc_ref", "")
            evidence_page = source.get("page", 1)
            evidence_quote = quote or f"Initial term ending on {expiry_date.isoformat()}."

            finding_item = {
                "id": f"expiry:{contract_id}",
                "class": "expiry",
                "variant": "EX-01",
                "contract_id": contract_id,
                "verdict": "finding",
                "amount_usd": None,
                "amount_range": None,
                "deadline": deadline,
                "action": "internal:alert",
                "confidence": 0.95,
                "evidence": [
                    {
                        "doc": evidence_doc,
                        "page": evidence_page,
                        "quote": evidence_quote,
                    }
                ],
                "missing_doc": None,
                "supersedes": [],
                "learned_at": None,
            }
            findings.append(finding_item)
        except Exception:
            continue

    return findings
