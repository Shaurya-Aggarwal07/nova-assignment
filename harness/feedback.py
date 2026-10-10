"""Feedback handling module for Nova harness.

Rules (GLOSSARY.md & README):
- apply_feedback(finding_key, feedback_answer, state_dir):
  Stores human feedback under <state_dir>/feedback.json.
- On subsequent report runs:
  - "confirmed" / "confirm": keep finding, raise confidence to 0.95
  - "rejected" / "reject" / "close": suppress finding entirely (drop from report)
  - "waiver_was_one_off" / "one_off_approval": marks waiver as single-invoice only
  - Standing feedback (e.g. rate_accepted): attaches learned_at: {feedback_item_id, batch}
    to affected findings in scope.
  - Unrecognized feedback: safely stored, ignored without crashing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_STATE = ".state"


def _norm_inv(s: Any) -> str:
    """Normalize invoice number by stripping spaces, colons, hashes and uppercase."""
    return re.sub(r"[\s#:]", "", str(s or "")).upper()


def match_finding_key(finding: dict[str, Any], stored_key: Any) -> bool:
    """Check if a finding matches a stored feedback finding_key."""
    inv_id = _norm_inv(finding.get("invoice_id"))
    cls = str(finding.get("class") or "").strip().lower()
    f_id = str(finding.get("id") or "").strip().lower()

    if isinstance(stored_key, dict):
        k_inv = _norm_inv(stored_key.get("invoice_id"))
        k_cls = str(stored_key.get("class") or "").strip().lower()
        if k_inv and k_inv == inv_id:
            return not k_cls or k_cls == cls
        return False

    if isinstance(stored_key, (list, tuple)) and len(stored_key) >= 2:
        s0, s1 = str(stored_key[0]), str(stored_key[1])
        if _norm_inv(s0) == inv_id and s1.lower() == cls:
            return True
        if _norm_inv(s1) == inv_id and s0.lower() == cls:
            return True
        return False

    if isinstance(stored_key, str):
        sk = stored_key.strip()
        # Shape: "invoice_<ID>_<CLASS>" e.g. "invoice_TX-US-2032-5394_overcharge"
        m = re.match(r"^invoice_([A-Za-z0-9-]+)(?:_([a-z_]+))?$", sk, re.IGNORECASE)
        if m:
            k_inv = _norm_inv(m.group(1))
            k_cls = (m.group(2) or "").lower()
            if k_inv == inv_id:
                return not k_cls or k_cls == cls

        # Shape: "<CLASS>:<ID>" e.g. "overcharge:TX-US-2035-0210"
        if ":" in sk:
            parts = sk.split(":", 1)
            if parts[0].lower() == cls and _norm_inv(parts[1]) == inv_id:
                return True

        # Shape: plain invoice ID
        if _norm_inv(sk) == inv_id:
            return True

        # Shape: exact id match
        if sk.lower() == f_id:
            return True

    return False


def load_feedback_items(state_dir: str | Path = DEFAULT_STATE) -> list[dict[str, Any]]:
    """Load all feedback items from state_dir/feedback.json and state_dir/feedback/*.json."""
    items: list[dict[str, Any]] = []
    s_path = Path(state_dir)

    # 1. Load from <state_dir>/feedback.json
    single_file = s_path / "feedback.json"
    if single_file.is_file():
        try:
            data = json.loads(single_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                if "items" in data and isinstance(data["items"], list):
                    items.extend(data["items"])
                else:
                    for k, v in data.items():
                        if isinstance(v, dict):
                            entry = dict(v)
                            entry.setdefault("finding_key", k)
                            items.append(entry)
                        else:
                            items.append({"finding_key": k, "decision": v, "answer": v})
        except Exception:
            pass

    # 2. Load from <state_dir>/feedback/*.json
    fb_dir = s_path / "feedback"
    if fb_dir.is_dir():
        for fp in sorted(fb_dir.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                batch_name = data.get("after_batch", fp.stem)
                for it in data.get("items", []):
                    if isinstance(it, dict):
                        it_copy = dict(it)
                        it_copy.setdefault("batch", batch_name)
                        items.append(it_copy)
            except Exception:
                pass

    return items


def apply_feedback(
    finding_key: Any,
    feedback_answer: Any,
    state_dir: str | Path = DEFAULT_STATE,
) -> None:
    """Store feedback for a finding key under <state_dir>/feedback.json."""
    s_path = Path(state_dir)
    s_path.mkdir(parents=True, exist_ok=True)
    fb_file = s_path / "feedback.json"

    current_data: dict[str, Any] = {"items": []}
    if fb_file.is_file():
        try:
            loaded = json.loads(fb_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and "items" in loaded:
                current_data = loaded
            elif isinstance(loaded, list):
                current_data = {"items": loaded}
        except Exception:
            pass

    # Determine decision / answer string
    if isinstance(feedback_answer, dict):
        decision = feedback_answer.get("decision") or feedback_answer.get("answer", "")
        fact = feedback_answer.get("fact")
        scope = feedback_answer.get("scope")
        item_id = feedback_answer.get("item_id", f"fb-{len(current_data['items']) + 1}")
    else:
        decision = str(feedback_answer).strip().lower()
        fact = None
        scope = None
        item_id = f"fb-{len(current_data['items']) + 1}"

    new_item: dict[str, Any] = {
        "item_id": item_id,
        "finding_key": finding_key,
        "decision": decision,
        "answer": str(feedback_answer),
    }
    if fact:
        new_item["fact"] = fact
    if scope:
        new_item["scope"] = scope

    # Update if key already exists, else append
    replaced = False
    for i, it in enumerate(current_data["items"]):
        if it.get("finding_key") == finding_key:
            current_data["items"][i] = new_item
            replaced = True
            break
    if not replaced:
        current_data["items"].append(new_item)

    fb_file.write_text(json.dumps(current_data, ensure_ascii=False, indent=2), encoding="utf-8")


def process_feedback_on_findings(
    findings: list[dict[str, Any]],
    state_dir: str | Path = DEFAULT_STATE,
    all_facts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Apply all stored feedback to findings, returning the modified findings list."""
    fb_items = load_feedback_items(state_dir)
    if not fb_items:
        return findings

    suppressed_indices: set[int] = set()
    standing_rules: list[dict[str, Any]] = []

    # 1. Collect standing rules
    for item in fb_items:
        fact = item.get("fact") or {}
        scope = item.get("scope") or {}
        if scope.get("kind") == "standing":
            standing_rules.append(item)

    # 2. Process each finding
    modified_findings: list[dict[str, Any]] = []
    for f_idx, finding in enumerate(findings):
        inv_id = _norm_inv(finding.get("invoice_id"))
        finding_copy = dict(finding)
        is_suppressed = False

        # Check direct key match
        for item in fb_items:
            fkey = item.get("finding_key")
            if match_finding_key(finding, fkey):
                dec = str(item.get("decision") or item.get("answer") or "").lower()

                if dec in ("reject", "rejected", "close", "suppress"):
                    is_suppressed = True
                    break
                elif dec in ("confirm", "confirmed"):
                    finding_copy["confidence"] = 0.95
                    if item.get("item_id"):
                        finding_copy["learned_at"] = {
                            "feedback_item_id": str(item["item_id"]),
                            "batch": str(item.get("batch") or "batch_01"),
                        }
                elif dec in ("waiver_was_one_off", "one_off_approval"):
                    finding_copy["one_off"] = True
                    if item.get("item_id"):
                        finding_copy["learned_at"] = {
                            "feedback_item_id": str(item["item_id"]),
                            "batch": str(item.get("batch") or "batch_01"),
                        }
                elif dec == "override_amount":
                    amt = item.get("fact", {}).get("value", {}).get("amount_usd")
                    if amt:
                        finding_copy["amount_usd"] = f"{float(amt):.2f}"
                    if item.get("item_id"):
                        finding_copy["learned_at"] = {
                            "feedback_item_id": str(item["item_id"]),
                            "batch": str(item.get("batch") or "batch_01"),
                        }

        if is_suppressed:
            continue

        # Check standing rules (e.g. rate_accepted for lane)
        if not finding_copy.get("learned_at"):
            for s_rule in standing_rules:
                fact = s_rule.get("fact") or {}
                scope = s_rule.get("scope") or {}
                applies_to = scope.get("applies_to") or {}
                req_cid = applies_to.get("contract_id")
                req_lane = applies_to.get("lane")

                # Match against finding's evidence or invoice facts
                matches_standing = False
                ev_text = " ".join(str(e.get("quote", "")) for e in finding_copy.get("evidence", []))
                if req_lane and req_lane in ev_text:
                    matches_standing = True

                if matches_standing:
                    finding_copy["learned_at"] = {
                        "feedback_item_id": str(s_rule.get("item_id") or "fb-01-p1"),
                        "batch": str(s_rule.get("batch") or "batch_01"),
                    }
                    break

        modified_findings.append(finding_copy)

    return modified_findings
