"""Action generation and unattended-action testing for the audit harness.

Per GLOSSARY.md and README.md:
- Unattended-action test (for autonomous execution):
  1. Evidence is complete (no missing document, no cannot_determine, valid quotes)
  2. No conflict exists for this finding
  3. Finding amount is < $5,000.00 USD equivalent
  4. Action is reversible (dispute, credit claim, payment reversal, document request are reversible;
     paying, settling, accepting terms, confirming renewal are NOT reversible)
  5. Untrusted input / prompt injection defense: suspicious instructions targeting AI/auditor
     are recorded and never followed, never executed unattended.

- Modes:
  - supervised: NEVER emit outbound_action (execute:*). All outbound actions are draft:* or human_review.
  - autonomous: outbound remedies become execute:* only when all unattended-action checks pass.
    Otherwise human_review / drafted_action.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Reversible action classes and identifiers
REVERSIBLE_CLASSES = {
    "overcharge",
    "duplicate",
    "waived_but_billed",
    "late_fee_not_owed",
    "cashback",
    "expiry",
    "late_fee_risk",
}

REVERSIBLE_ACTION_KEYWORDS = (
    "dispute",
    "claim_credit",
    "credit_claim",
    "payment_reversal",
    "request_document",
    "alert",
    "payment_reminder",
)

NON_REVERSIBLE_KEYWORDS = (
    "pay",
    "settle",
    "accept_terms",
    "accept",
    "confirm_renewal",
    "release",
    "terminate",
)

# Prompt injection detection regex patterns
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous\s+)?instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous\s+)?instructions", re.IGNORECASE),
    re.compile(r"override\s+(all\s+)?instructions", re.IGNORECASE),
    re.compile(r"approve\s+all\s+invoices", re.IGNORECASE),
    re.compile(r"send\s+payment\s+to", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"new\s+system\s+directive", re.IGNORECASE),
    re.compile(r"you\s+must\s+approve", re.IGNORECASE),
]


def detect_suspicious_instruction(text: str) -> bool:
    """Return True if text contains prompt-injection or suspicious instructions targeting AI."""
    if not text:
        return False
    return any(p.search(str(text)) for p in INJECTION_PATTERNS)


def finding_has_suspicious_instruction(finding: dict[str, Any]) -> bool:
    """Check whether a finding contains suspicious instructions in its evidence quotes or fields."""
    if not isinstance(finding, dict):
        return False
    # Check evidence quotes
    for ev in finding.get("evidence", []):
        if isinstance(ev, dict):
            quote = ev.get("quote", "")
            if detect_suspicious_instruction(quote):
                return True
    # Check text attributes
    for key in ("id", "invoice_id", "comment", "variant", "note"):
        val = finding.get(key)
        if val and detect_suspicious_instruction(str(val)):
            return True
    return False


def record_suspicious_instruction(
    finding_or_text: dict[str, Any] | str,
    state_dir: str | Path | None = None,
    doc_ref: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Record a suspicious instruction as a fact of type 'suspicious_instruction'.

    Can optionally persist the fact under <state_dir>/facts/suspicious_instructions.json.
    """
    if isinstance(finding_or_text, dict):
        quotes = [
            str(ev.get("quote", ""))
            for ev in finding_or_text.get("evidence", [])
            if isinstance(ev, dict) and ev.get("quote")
        ]
        text = " | ".join(quotes) if quotes else str(finding_or_text.get("id", ""))
        doc = (
            finding_or_text.get("evidence", [{}])[0].get("doc", doc_ref or "unknown")
            if finding_or_text.get("evidence")
            else (doc_ref or "unknown")
        )
        pg = (
            finding_or_text.get("evidence", [{}])[0].get("page", page or 1)
            if finding_or_text.get("evidence")
            else (page or 1)
        )
    else:
        text = str(finding_or_text)
        doc = doc_ref or "unknown"
        pg = page or 1

    fact = {
        "fact_type": "suspicious_instruction",
        "instruction_text": text[:500],
        "target_audience": "auditor/ai",
        "source": {"doc_ref": doc, "page": pg},
        "quote": text[:200],
    }

    if state_dir:
        try:
            facts_dir = Path(state_dir) / "facts"
            facts_dir.mkdir(parents=True, exist_ok=True)
            suspicious_path = facts_dir / "suspicious_instructions.json"
            existing = []
            if suspicious_path.exists():
                try:
                    loaded = json.loads(suspicious_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and "facts" in loaded:
                        existing = loaded["facts"]
                except Exception:
                    existing = []
            existing.append(fact)
            suspicious_path.write_text(
                json.dumps({"facts": existing}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception:
            pass

    return fact


def unattended_ok(finding: dict[str, Any]) -> bool:
    """Return True ONLY if ALL of:
    - Evidence is complete (all required documents exist, no cannot_determine, valid quotes)
    - No conflict exists for this finding
    - Finding amount is < $5,000.00 USD equivalent
    - Action is reversible (dispute, credit claim, payment reversal are reversible; others may not be)
    - Return False if ANY condition fails.
    """
    if not isinstance(finding, dict):
        return False

    # Condition 0: Safety / Prompt injection defense
    if finding_has_suspicious_instruction(finding):
        return False

    # Condition 1: Evidence is complete (all required documents exist, no cannot_determine)
    if finding.get("verdict") == "cannot_determine":
        return False
    if finding.get("missing_doc") is not None:
        return False
    evidence = finding.get("evidence")
    if not evidence or not isinstance(evidence, list) or len(evidence) == 0:
        return False
    for ev in evidence:
        if not isinstance(ev, dict):
            return False
        quote = str(ev.get("quote", ""))
        doc = str(ev.get("doc", ""))
        if not doc or not quote:
            return False
        # GLOSSARY: quote must have at least 12 non-space characters
        if len(re.sub(r"\s", "", quote)) < 12:
            return False

    # Condition 2: No conflict exists for this finding
    if finding.get("verdict") == "conflict":
        return False
    if finding.get("amount_range") is not None:
        return False
    if finding.get("has_conflict") is True:
        return False

    # Condition 3: Finding amount is < $5,000 USD equivalent
    amt_usd = finding.get("amount_usd")
    if amt_usd is not None:
        try:
            amt = Decimal(str(amt_usd))
            if amt >= Decimal("5000.00"):
                return False
        except (InvalidOperation, ValueError):
            return False

    # Condition 4: Action is reversible
    # (dispute, credit claim, payment reversal are reversible; others like paying, settling may not be)
    if finding.get("reversible") is False:
        return False

    act_str = str(finding.get("action") or "").lower()
    act_type = str(finding.get("action_type") or "").lower()
    combined_act = f"{act_str} {act_type}"

    # Non-reversible checks
    if any(nkw in combined_act for nkw in NON_REVERSIBLE_KEYWORDS):
        return False

    cls = str(finding.get("class") or "").lower()
    if cls in REVERSIBLE_CLASSES:
        return True
    if any(rakw in combined_act for rakw in REVERSIBLE_ACTION_KEYWORDS):
        return True

    return False


# ---- Action Templates (Clean parameterised text, no hardcoded carrier IDs) ----

TEMPLATES: dict[str, dict[str, str]] = {
    "dispute_email": {
        "subject": "Billing Dispute - Invoice {invoice_id} ({finding_class})",
        "body": (
            "Dear Billing Disputes Department,\n\n"
            "We are disputing invoice {invoice_id} in the amount of ${amount_usd} "
            "due to an identified {finding_class} discrepancy.\n\n"
            "Summary:\n{summary}\n\n"
            "Please review the attached evidence and issue a credit note for ${amount_usd}.\n\n"
            "Regards,\nFreight Audit Automation"
        ),
    },
    "credit_claim": {
        "subject": "Credit Claim - Contract {contract_id} ({period})",
        "body": (
            "Dear Carrier Account Representative,\n\n"
            "Under Contract {contract_id}, we claim a credit of ${amount_usd} for {period} "
            "({credit_type}).\n\n"
            "Summary:\n{summary}\n\n"
            "Please apply this credit note to our account.\n\n"
            "Regards,\nFreight Audit Automation"
        ),
    },
    "expiry_alert": {
        "subject": "Contract Deadline Alert - {contract_id}",
        "body": (
            "INTERNAL NOTICE:\n\n"
            "Contract {contract_id} operative date / notice deadline is {deadline}.\n"
            "Detail:\n{summary}\n\n"
            "Please review for renewal or non-renewal notice."
        ),
    },
    "payment_reminder": {
        "subject": "Payment Reminder - Invoice {invoice_id}",
        "body": (
            "INTERNAL NOTICE:\n\n"
            "Invoice {invoice_id} is pending payment with due date {deadline}.\n"
            "Potential late fee exposure: ${amount_usd}.\n"
            "Please include on the next payment run."
        ),
    },
    "payment_reversal": {
        "subject": "Payment Reversal Request - Invoice {invoice_id}",
        "body": (
            "Dear Accounts Department,\n\n"
            "We request a payment reversal in the amount of ${amount_usd} for invoice {invoice_id}.\n"
            "Reason: {summary}\n\n"
            "Regards,\nFreight Audit Automation"
        ),
    },
    "escalation_to_approver": {
        "subject": "Audit Escalation - Finding {finding_id}",
        "body": (
            "ATTENTION APPROVER:\n\n"
            "Finding {finding_id} requires human review and sign-off.\n"
            "Amount: ${amount_usd}\n"
            "Reason for escalation: {summary}\n\n"
            "Please inspect the findings and confirm or reject."
        ),
    },
    "document_request": {
        "subject": "Document Request - {missing_doc_name}",
        "body": (
            "Dear {holder_name},\n\n"
            "To complete audit verification for invoice {invoice_id}, we request a copy of:\n"
            "- Document: {missing_doc_name}\n\n"
            "Please provide this document at your earliest convenience."
        ),
    },
}


def _determine_action_type(finding: dict[str, Any]) -> str:
    """Determine the primary action type for a finding."""
    if finding.get("action_type"):
        return str(finding["action_type"])

    cls = str(finding.get("class") or "").lower()
    verdict = str(finding.get("verdict") or "").lower()

    if verdict == "conflict":
        return "escalation_to_approver"
    if verdict == "cannot_determine":
        return "document_request"

    if cls == "expiry":
        return "expiry_alert"
    if cls == "late_fee_risk":
        return "payment_reminder"
    if cls == "cashback":
        return "credit_claim"
    if cls in ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed"):
        return "dispute_email"

    return "dispute_email"


def generate_action(
    finding: dict[str, Any],
    mode: str = "supervised",
    state_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Generate an action object for a finding according to mode and unattended-action rules.

    - For supervised mode: NEVER emit outbound_action. Return only internal actions (alert, log)
      or drafted_action (template text for a person to review/send).
    - For autonomous mode:
      - If unattended_ok(finding) == True: emit outbound_action (mark it as "sent": True).
      - If unattended_ok(finding) == False: emit drafted_action (mark it as "sent": False, "escalated": True).
    - If prompt injection / suspicious instruction detected:
      - The instruction is NOT followed.
      - A suspicious_instruction fact is recorded.
      - The finding action is marked escalated, never sent.
    """
    if not isinstance(finding, dict):
        return None

    verdict = str(finding.get("verdict") or "")
    if verdict == "no_finding":
        finding["action"] = "none"
        return None

    # Check for suspicious instructions / prompt injection
    if finding_has_suspicious_instruction(finding):
        suspicious_fact = record_suspicious_instruction(finding, state_dir=state_dir)
        finding["suspicious_instruction"] = suspicious_fact
        finding["action"] = "escalate"

        # Return draft escalation action that is NEVER sent
        finding_id = str(finding.get("id") or "unknown")
        amount_usd = str(finding.get("amount_usd") or "0.00")
        tmpl = TEMPLATES["escalation_to_approver"]
        subj = tmpl["subject"].format(finding_id=finding_id)
        body = tmpl["body"].format(
            finding_id=finding_id,
            amount_usd=amount_usd,
            summary="Suspicious instruction / potential prompt injection detected in source evidence.",
        )
        action_obj = {
            "action_type": "escalation_to_approver",
            "action_name": "escalate",
            "kind": "draft",
            "sent": False,
            "escalated": True,
            "finding_id": finding_id,
            "recipient": "internal_security_review",
            "subject": subj,
            "body": body,
            "amount_usd": amount_usd,
            "reversible": False,
        }
        finding["action_detail"] = action_obj
        return action_obj

    passes_unattended = unattended_ok(finding)
    raw_action_type = _determine_action_type(finding)
    cls = str(finding.get("class") or "").lower()

    # Determine schema-compliant finding["action"] string
    amt_usd = finding.get("amount_usd")
    is_high_value = False
    if amt_usd is not None:
        try:
            is_high_value = Decimal(str(amt_usd)) >= Decimal("5000.00")
        except (InvalidOperation, ValueError):
            is_high_value = False

    if verdict == "conflict":
        schema_action = "escalate"
    elif verdict == "cannot_determine":
        schema_action = "execute:request_document" if (mode == "autonomous" and passes_unattended) else "draft:request_document"
    elif cls == "expiry":
        schema_action = "internal:alert"
    elif cls == "late_fee_risk":
        schema_action = "internal:payment_reminder"
    elif cls == "cashback":
        if mode == "autonomous":
            schema_action = "execute:claim_credit" if passes_unattended else "human_review"
        else:
            schema_action = "human_review" if is_high_value else "draft:claim_credit"
    else:  # overcharge, duplicate, waived_but_billed, late_fee_not_owed
        if mode == "autonomous":
            schema_action = "execute:dispute" if passes_unattended else "human_review"
        else:
            schema_action = "human_review" if is_high_value else "draft:dispute"

    finding["action"] = schema_action

    # Classify whether this action is internal vs outbound
    is_internal = raw_action_type in ("expiry_alert", "payment_reminder")

    if is_internal:
        action_type = raw_action_type
        kind = "internal"
        sent = False
        escalated = False
    elif mode == "supervised":
        # SUPERVISED: NEVER emit outbound_action (sent: True). Always draft.
        kind = "draft"
        sent = False
        escalated = (not passes_unattended) or (schema_action in ("escalate", "human_review"))
        action_type = "escalation_to_approver" if schema_action == "escalate" else raw_action_type
    else:
        # AUTONOMOUS:
        if passes_unattended:
            kind = "outbound"
            sent = True
            escalated = False
            action_type = raw_action_type
        else:
            kind = "draft"
            sent = False
            escalated = True
            action_type = "escalation_to_approver" if schema_action in ("escalate", "human_review") else raw_action_type

    # Format template fields
    finding_id = str(finding.get("id") or "N/A")
    invoice_id = str(finding.get("invoice_id") or "N/A")
    contract_id = str(finding.get("contract_id") or "N/A")
    period = str(finding.get("period") or "N/A")
    credit_type = str(finding.get("credit_type") or "rebate")
    amount_usd_str = str(finding.get("amount_usd") or "0.00")
    deadline = str(finding.get("deadline") or "N/A")
    missing_doc = finding.get("missing_doc") or {}
    missing_doc_name = str(missing_doc.get("name") or "Document")
    holder = missing_doc.get("holder") or {}
    holder_name = str(holder.get("person") or holder.get("party") or "Document Custodian")

    # Build concise evidence summary for template body
    ev_quotes = [
        str(ev.get("quote", ""))
        for ev in finding.get("evidence", [])
        if isinstance(ev, dict) and ev.get("quote")
    ]
    summary = "\n".join(f"- {q}" for q in ev_quotes[:3]) if ev_quotes else f"Finding {finding_id}"

    tmpl = TEMPLATES.get(action_type, TEMPLATES["dispute_email"])
    subject = tmpl["subject"].format(
        invoice_id=invoice_id,
        finding_class=cls,
        contract_id=contract_id,
        period=period,
        deadline=deadline,
        missing_doc_name=missing_doc_name,
        finding_id=finding_id,
    )
    body = tmpl["body"].format(
        invoice_id=invoice_id,
        finding_class=cls,
        contract_id=contract_id,
        period=period,
        credit_type=credit_type,
        amount_usd=amount_usd_str,
        deadline=deadline,
        missing_doc_name=missing_doc_name,
        holder_name=holder_name,
        finding_id=finding_id,
        summary=summary,
    )

    action_obj: dict[str, Any] = {
        "action_type": action_type,
        "action_name": schema_action,
        "kind": kind,
        "sent": sent,
        "escalated": escalated,
        "finding_id": finding_id,
        "recipient": holder_name if action_type == "document_request" else (
            "internal_accounts" if is_internal else "carrier_billing"
        ),
        "subject": subject,
        "body": body,
        "amount_usd": amount_usd_str,
        "reversible": raw_action_type in ("dispute_email", "credit_claim", "payment_reversal", "document_request"),
    }

    finding["action_detail"] = action_obj
    return action_obj
