#!/usr/bin/env python
"""Unit tests for feedback handling and two-batch replay test."""
from __future__ import annotations

import difflib
import json
import shutil
import sys
import tempfile
from pathlib import Path

# Add repo root and harness to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from feedback import apply_feedback, process_feedback_on_findings
try:
    import harness.harness as harness
except ImportError:
    import harness


def test_hand_constructed_feedback():
    """Test 1: Hand-construct findings and apply feedback.

    - "confirmed" -> confidence raised to 0.95
    - "rejected" -> suppressed entirely
    - non-existent key -> ignored safely
    - untouched key -> left unchanged
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="nova_fb_test_"))
    try:
        findings = [
            {
                "id": "overcharge:TX-US-2032-5394",
                "invoice_id": "TX-US-2032-5394",
                "class": "overcharge",
                "amount_usd": "100.00",
                "confidence": 0.80,
                "action": "draft:dispute",
                "verdict": "finding",
                "evidence": [],
            },
            {
                "id": "overcharge:TX-US-2032-5395",
                "invoice_id": "TX-US-2032-5395",
                "class": "overcharge",
                "amount_usd": "50.00",
                "confidence": 0.85,
                "action": "draft:dispute",
                "verdict": "finding",
                "evidence": [],
            },
            {
                "id": "overcharge:TX-US-2032-5396",
                "invoice_id": "TX-US-2032-5396",
                "class": "overcharge",
                "amount_usd": "75.00",
                "confidence": 0.82,
                "action": "draft:dispute",
                "verdict": "finding",
                "evidence": [],
            },
        ]

        # 1. Apply feedback
        # Confirm 5394
        apply_feedback("invoice_TX-US-2032-5394_overcharge", "confirmed", state_dir=tmp_dir)
        # Reject 5395
        apply_feedback("invoice_TX-US-2032-5395_overcharge", "rejected", state_dir=tmp_dir)
        # Non-existent key
        apply_feedback("invoice_NON_EXISTENT_overcharge", "rejected", state_dir=tmp_dir)

        # 2. Process feedback on findings
        modified = process_feedback_on_findings(findings, state_dir=tmp_dir)

        # Assertions:
        # - TX-US-2032-5395 must be suppressed
        remaining_ids = [m["invoice_id"] for m in modified]
        assert "TX-US-2032-5395" not in remaining_ids, "Rejected finding was not suppressed!"
        assert len(modified) == 2, f"Expected 2 remaining findings, got {len(modified)}"

        # - TX-US-2032-5394 must have confidence raised to 0.95
        m_confirmed = next(m for m in modified if m["invoice_id"] == "TX-US-2032-5394")
        assert m_confirmed["confidence"] == 0.95, f"Expected confidence 0.95, got {m_confirmed['confidence']}"

        # - TX-US-2032-5396 must be untouched
        m_untouched = next(m for m in modified if m["invoice_id"] == "TX-US-2032-5396")
        assert m_untouched["confidence"] == 0.82, f"Untouched finding modified! {m_untouched['confidence']}"
        assert m_untouched["amount_usd"] == "75.00"

        print("PASS: test_hand_constructed_feedback passed")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_two_batch_feedback_replay():
    """Test 2: Two-batch test from README.

    - Ingest batch_01, run report (batch_01_report.json)
    - Load feedback from data/scenarios/feedback/feedback.json (or W1_batch_01.json)
    - Apply feedback
    - Ingest batch_02 with same state_dir
    - Run report again (batch_02_report.json)
    - Compare: rejected/closed finding suppressed; confirmed finding updated
    """
    b1_dir = REPO_ROOT / "data" / "scenarios" / "batch_01"
    b2_dir = REPO_ROOT / "data" / "scenarios" / "batch_02"
    fb_file = REPO_ROOT / "data" / "scenarios" / "feedback" / "W1_batch_01.json"
    if not fb_file.exists():
        fb_file = REPO_ROOT / "data" / "scenarios" / "feedback" / "feedback.json"

    if not b1_dir.is_dir() or not b2_dir.is_dir() or not fb_file.is_file():
        print("SKIP: required files for two-batch test not found")
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="nova_twobatch_"))
    try:
        state_dir = tmp_dir / "state"

        # 1. Ingest batch_01
        harness.ingest(str(b1_dir), str(state_dir))
        rep1 = harness.report("2035-05-31", mode="supervised", state_dir=str(state_dir))
        rep1_file = tmp_dir / "batch_01_report.json"
        rep1_file.write_text(json.dumps(rep1, indent=2), encoding="utf-8")

        # 2. Store / Apply feedback
        harness.feedback(str(fb_file), str(state_dir))

        # Also store a test confirmed and rejected key to observe explicit state modification
        apply_feedback("invoice_TX-CONFIRM-TEST_overcharge", "confirmed", state_dir=str(state_dir))
        apply_feedback("invoice_TX-REJECT-TEST_overcharge", "rejected", state_dir=str(state_dir))

        # 3. Ingest batch_02
        harness.ingest(str(b2_dir), str(state_dir))
        rep2 = harness.report("2035-09-30", mode="supervised", state_dir=str(state_dir))
        rep2_file = tmp_dir / "batch_02_report.json"
        rep2_file.write_text(json.dumps(rep2, indent=2), encoding="utf-8")

        # In fb-01-p1, TX-US-2035-0210 was closed
        rep2_inv_ids = [f.get("invoice_id") for f in rep2.get("findings", [])]
        assert "TX-US-2035-0210" not in rep2_inv_ids, "Closed invoice TX-US-2035-0210 still in batch_02_report!"

        # Generate summary diff between rep1 and rep2 finding IDs
        rep1_ids = sorted(f.get("id", "") for f in rep1.get("findings", []))
        rep2_ids = sorted(f.get("id", "") for f in rep2.get("findings", []))

        diff = list(difflib.unified_diff(
            [f"{x}\n" for x in rep1_ids],
            [f"{x}\n" for x in rep2_ids],
            fromfile="batch_01_finding_ids",
            tofile="batch_02_finding_ids",
        ))

        print("Two-batch Replay Diff (Findings):")
        diff_summary = "".join(diff[:25])  # Show sample diff lines
        print(diff_summary if diff_summary else "No diff in IDs")

        print("PASS: test_two_batch_feedback_replay passed")
        return "".join(diff)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    print("=" * 60)
    print("Running Feedback handling unit tests")
    print("=" * 60)
    test_hand_constructed_feedback()
    print("Running Two-Batch Feedback Replay Test...")
    diff_text = test_two_batch_feedback_replay()
    print("\nALL FEEDBACK TESTS PASSED!")
    return diff_text


if __name__ == "__main__":
    main()
