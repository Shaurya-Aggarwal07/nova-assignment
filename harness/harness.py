"""Your harness. The grader calls exactly these three commands, in a fresh process each time:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

- ingest: read every .eml in <batch_dir> (attachments inside) and store what you learn under --state-dir. The process
  is restarted between batches, so everything you need later must be on disk.
- report: print one report.json (grader/schema/report.schema.json) to stdout. Only documents received on or before
  --as-of count. --mode defaults to supervised.
- feedback: take in a person's decisions on what you escalated (feedback.json) and let them change later reports.

The environment gives ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL. Keep the argument names as they are: grader/validate.py
checks them. Replace the NotImplementedError bodies with your code.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_STATE = ".state"


def ingest(batch_dir: str, state_dir: str) -> None:
    """Read every .eml in *batch_dir*, extract structured facts via Claude,
    and persist them under *state_dir*.  Cached documents are skipped.
    """
    # Lazy imports so the top-level parse is fast and other subcommands
    # don't pay the import cost.
    # NOTE: reader.py puts grader/ on sys.path, so a bare `import extract`
    # would pick up grader/extract.py.  We load harness/extract.py explicitly.
    import importlib.util
    _harness_dir = Path(__file__).resolve().parent

    # Import harness/reader.py
    sys.path.insert(0, str(_harness_dir))
    from reader import read_batch                        # noqa: E402

    # Import harness/extract.py by file path to avoid the grader collision
    _ext_spec = importlib.util.spec_from_file_location(
        "harness_extract", str(_harness_dir / "extract.py")
    )
    _ext_mod = importlib.util.module_from_spec(_ext_spec)
    _ext_spec.loader.exec_module(_ext_mod)
    extract_facts_from_doc = _ext_mod.extract_facts_from_doc
    reader_results_to_extract_inputs = _ext_mod.reader_results_to_extract_inputs
    extract_batch = _ext_mod.extract_batch
    _get_client = _ext_mod._get_client

    # Load API key from .env.local if not already in the environment
    _repo_root = _harness_dir.parent
    _env_file = _repo_root / ".env.local"
    if not os.environ.get("ANTHROPIC_API_KEY") and _env_file.exists():
        for line in _env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Support both "VAR=value" and bare "value" (just the key)
            if "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
            elif line.startswith("sk-"):
                # Bare API key on its own line
                os.environ.setdefault("ANTHROPIC_API_KEY", line)

    batch_path = Path(batch_dir)
    if not batch_path.is_dir():
        print(f"Error: {batch_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Ensure state dirs exist
    facts_dir = Path(state_dir) / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)

    # 1. Read all .eml files → ReadResults
    print(f"Reading .eml files from {batch_dir} ...")
    read_results_map = read_batch(batch_path, state_dir=state_dir)

    # Flatten into a single list of ReadResults
    all_read_results: list = []
    for rrs in read_results_map.values():
        all_read_results.extend(rrs)

    # 2. Convert to extract inputs: (doc_ref, [(page, text), ...])
    extract_inputs = reader_results_to_extract_inputs(all_read_results)
    total_docs = len(extract_inputs)

    # 3. Extract facts for each document, skipping those already on disk
    #    We use the doc_ref as the on-disk key (sanitised to a filename).
    def _fact_path(doc_ref: str) -> Path:
        safe = doc_ref.replace("/", "__").replace("#", "__").replace("\\", "__")
        return facts_dir / f"{safe}.json"

    newly_extracted = 0
    cached = 0
    model = os.environ.get("HARNESS_MODEL", None)
    client = None  # lazy

    docs_to_extract = []
    for doc_ref, page_texts in extract_inputs:
        fp = _fact_path(doc_ref)
        if fp.exists():
            cached += 1
        else:
            docs_to_extract.append((doc_ref, page_texts))

    if docs_to_extract:
        extracted_map = extract_batch(
            docs_to_extract,
            state_dir=state_dir,
            model=model,
            max_workers=5,
        )
        for doc_ref, facts in extracted_map.items():
            fp = _fact_path(doc_ref)
            fp.write_text(
                json.dumps(facts, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            newly_extracted += 1

    print(
        f"\nDone: {total_docs} document(s) total, "
        f"{newly_extracted} newly extracted, "
        f"{cached} already cached."
    )


def report(as_of: str, mode: str, state_dir: str) -> dict:
    """Return the report.json dict for --as-of *as_of* in *mode* mode.
    Loads ALL stored facts and feedback under *state_dir* and recomputes from
    scratch each time (no caching of findings).
    """
    _harness_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(_harness_dir))
    import store

    # Load all stored facts and feedback from scratch each time
    all_facts = store.load_all_facts(state_dir)
    all_feedback = store.load_all_feedback(state_dir)

    # Recomputation logic
    from checks.expiry import check_expiry
    from checks.overcharge import check_overcharge
    from checks.waived_but_billed import check_waived_but_billed
    from checks.duplicate import check_duplicate
    from checks.late_fees import check_late_fees
    from checks.cashback_credits import check_cashback_credits

    late_fee_items = check_late_fees(all_facts, as_of, mode=mode)
    cashback_items = check_cashback_credits(all_facts, as_of, mode=mode)

    findings: list[dict] = []
    findings.extend(check_overcharge(all_facts, as_of, mode=mode))
    findings.extend(check_waived_but_billed(all_facts, as_of, mode=mode))
    findings.extend(check_duplicate(all_facts, as_of, mode=mode))
    findings.extend([it for it in late_fee_items if it.get("class") == "late_fee_not_owed"])

    from conflicts import detect_conflicts, detect_cannot_determine
    from feedback import process_feedback_on_findings

    findings = detect_conflicts(findings, all_facts, as_of_date=as_of, mode=mode)
    findings = detect_cannot_determine(findings, all_facts, as_of_date=as_of, mode=mode)
    findings = process_feedback_on_findings(findings, state_dir=state_dir, all_facts=all_facts)

    # Deduplicate findings so each finding has unique id and unique (class, item_id)
    seen_ids: set[str] = set()
    seen_class_keys: set[tuple[Any, Any]] = set()
    deduped_findings: list[dict] = []
    for f in findings:
        fid = f.get("id")
        fkey = (f.get("class"), f.get("invoice_id") or fid)
        if fid not in seen_ids and fkey not in seen_class_keys:
            seen_ids.add(fid)
            seen_class_keys.add(fkey)
            deduped_findings.append(f)
    findings = deduped_findings

    findings.sort(key=lambda x: (x.get("class", ""), x.get("id", "")))
    cashback: list[dict] = sorted([it for it in cashback_items if it.get("verdict") == "finding"], key=lambda x: x.get("id", ""))
    expiry: list[dict] = sorted(check_expiry(all_facts, as_of), key=lambda x: x.get("id", ""))
    late_fee_risk: list[dict] = sorted([it for it in late_fee_items if it.get("class") == "late_fee_risk"], key=lambda x: x.get("id", ""))

    from actions import generate_action

    actions_list: list[dict] = []
    for item in findings:
        act = generate_action(item, mode=mode, state_dir=state_dir)
        if act:
            actions_list.append(act)

    for item in cashback:
        act = generate_action(item, mode=mode, state_dir=state_dir)
        if act:
            actions_list.append(act)

    for item in expiry:
        act = generate_action(item, mode=mode, state_dir=state_dir)
        if act:
            actions_list.append(act)

    for item in late_fee_risk:
        act = generate_action(item, mode=mode, state_dir=state_dir)
        if act:
            actions_list.append(act)

    return {
        "schema_version": "1.0",
        "as_of": as_of,
        "mode": mode,
        "findings": findings,
        "portfolio": {
            "cashback": cashback,
            "expiry": expiry,
            "late_fee_risk": late_fee_risk,
        },
        "actions": actions_list,
    }


def feedback(feedback_file: str, state_dir: str) -> None:
    """Store decisions from *feedback_file* under *state_dir* so later reports use them."""
    _harness_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(_harness_dir))
    import store

    saved = store.save_feedback(state_dir, feedback_file)
    print(f"Stored feedback in {saved}", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ingest":
            ingest(a.batch_dir, a.state_dir)
        elif a.cmd == "report":
            import json
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
