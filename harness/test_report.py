#!/usr/bin/env python
"""Test harness report and feedback CLI commands against grader schema."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Paths
REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "harness" / "harness.py"
SCHEMA_PATH = REPO_ROOT / "grader" / "schema" / "report.schema.json"
VALIDATE_PATH = REPO_ROOT / "grader" / "validate.py"
FEEDBACK_SAMPLE = REPO_ROOT / "data" / "scenarios" / "feedback" / "W1_batch_01.json"
PYTHON = sys.executable


def test_schema_direct():
    """Test report() function directly in Python and validate against schema."""
    import jsonschema

    sys.path.insert(0, str(REPO_ROOT / "harness"))
    import harness

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    for mode in ("supervised", "autonomous"):
        rep = harness.report(as_of="2035-06-01", mode=mode, state_dir=".state/s1")
        errors = list(validator.iter_errors(rep))
        assert not errors, f"Schema validation errors for mode={mode}: {errors}"
        assert rep["as_of"] == "2035-06-01"
        assert rep["mode"] == mode
        assert "findings" in rep
        assert "portfolio" in rep
        assert "cashback" in rep["portfolio"]
        assert "expiry" in rep["portfolio"]
        assert "late_fee_risk" in rep["portfolio"]

    print("PASS: Direct report() schema validation passed for both modes")


def test_report_cli(tmp_state: Path):
    """Test `python harness.py report ... > report.json` CLI execution."""
    out_file = tmp_state / "test_report.json"
    cmd = [
        PYTHON,
        str(HARNESS),
        "report",
        "--as-of",
        "2035-06-01",
        "--mode",
        "supervised",
        "--state-dir",
        str(tmp_state),
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out_file.write_text(r.stdout, encoding="utf-8")

    # Validate output file using grader/validate.py
    val_cmd = [
        PYTHON,
        str(VALIDATE_PATH),
        "--report",
        str(out_file),
    ]
    r_val = subprocess.run(val_cmd, capture_output=True, text=True)
    print("Grader validate output:")
    print(r_val.stdout)
    assert r_val.returncode == 0, f"validate.py failed: {r_val.stderr}"
    print("PASS: CLI report command produced valid schema output")


def test_feedback_cli(tmp_state: Path):
    """Test `python harness.py feedback <file> --state-dir <dir>` CLI execution."""
    cmd = [
        PYTHON,
        str(HARNESS),
        "feedback",
        str(FEEDBACK_SAMPLE),
        "--state-dir",
        str(tmp_state),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)

    # Check file exists in tmp_state/feedback/
    dest = tmp_state / "feedback" / FEEDBACK_SAMPLE.name
    assert dest.is_file(), f"Expected feedback file at {dest}"

    stored_data = json.loads(dest.read_text(encoding="utf-8"))
    assert stored_data["after_batch"] == "batch_01"
    assert len(stored_data["items"]) == 1
    print(f"PASS: Feedback successfully stored in {dest}")


def main():
    print("=" * 60)
    print("Running harness report and feedback tests")
    print("=" * 60)

    # 1. Test against existing .state/s1
    test_schema_direct()

    # 2. Test CLI in a temporary state dir
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_state = Path(tmp_dir) / "test_state"
        tmp_state.mkdir()

        # Copy over facts to test state
        facts_src = REPO_ROOT / ".state" / "s1" / "facts"
        if facts_src.is_dir():
            import shutil
            shutil.copytree(facts_src, tmp_state / "facts")

        test_feedback_cli(tmp_state)
        test_report_cli(tmp_state)

    print("\nALL REPORT & FEEDBACK TESTS PASSED!")


if __name__ == "__main__":
    main()
