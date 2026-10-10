"""Evaluate confidence calibration (Brier score and AUROC) over reported findings.

Usage:
    python harness/eval_confidence.py [--report path/to/report.json] [--answers data/scenarios/answers]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))
sys.path.insert(0, str(REPO_ROOT))

try:
    import harness.harness as harness
except ImportError:
    import harness


def dec(val: Any) -> Decimal | None:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None


def norm_invoice(s: Any) -> str:
    return re.sub(r"[\s#:]", "", str(s or "")).upper()


def item_key(item: dict[str, Any]) -> tuple:
    cls = str(item.get("class") or "")
    if cls == "cashback":
        return (
            "cashback",
            str(item.get("contract_id")),
            str(item.get("period")),
            str(item.get("credit_type")),
        )
    if cls == "expiry":
        return ("expiry", str(item.get("contract_id")))
    return (cls, norm_invoice(item.get("invoice_id")))


def load_expected_answers(answers_dir: Path) -> dict[tuple, dict[str, Any]]:
    """Load all scenario answer files from data/scenarios/answers/*.yaml."""
    answers = {}
    if not answers_dir.is_dir():
        return answers

    for p in sorted(answers_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            k_dict = data.get("key") or {}
            cls = data.get("class") or ("cashback" if "period" in k_dict else "expiry")
            if cls == "cashback":
                ktup = (
                    "cashback",
                    str(k_dict.get("contract_id")),
                    str(k_dict.get("period")),
                    str(k_dict.get("credit_type")),
                )
            elif cls == "expiry":
                ktup = ("expiry", str(k_dict.get("contract_id")))
            else:
                ktup = (cls, norm_invoice(k_dict.get("invoice_id")))
            answers[ktup] = data
        except Exception:
            pass
    return answers


def check_is_correct(rep: dict[str, Any], exp: dict[str, Any] | None) -> bool:
    """Check if reported item matches expected truth."""
    if exp is None:
        return False
    if rep.get("verdict") != exp.get("verdict"):
        return False

    v = rep.get("verdict")
    if v == "finding":
        amt_r = dec(rep.get("amount_usd"))
        amt_e = dec(exp.get("amount_usd"))
        if amt_r is not None and amt_e is not None:
            return abs(amt_r - amt_e) <= Decimal("0.01")
        return amt_r == amt_e
    return True


def auroc(scores: list[float], labels: list[int]) -> float | None:
    """Compute AUROC probability. Returns None if all 1s or all 0s."""
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def brier_score(scores: list[float], labels: list[int]) -> float | None:
    """Compute mean squared error between confidence and correctness label."""
    if not scores:
        return None
    return sum((s - l) ** 2 for s, l in zip(scores, labels)) / len(scores)


def eval_report(report_data: dict[str, Any], answers_dir: Path) -> dict[str, Any]:
    answers = load_expected_answers(answers_dir)

    all_items: list[dict[str, Any]] = []
    all_items.extend([x for x in report_data.get("findings", []) if isinstance(x, dict)])
    pf = report_data.get("portfolio") or {}
    if isinstance(pf, dict):
        for k in ("cashback", "expiry", "late_fee_risk"):
            all_items.extend([x for x in pf.get(k, []) if isinstance(x, dict)])

    scored_rows: list[dict[str, Any]] = []
    by_class: dict[str, list[dict[str, Any]]] = {}

    for it in all_items:
        k = item_key(it)
        cls = k[0]
        exp = answers.get(k)
        is_corr = 1 if check_is_correct(it, exp) else 0

        try:
            conf = float(it.get("confidence", 0.0))
        except (ValueError, TypeError):
            conf = 0.0

        brier_val = (conf - is_corr) ** 2

        entry = {
            "key": k,
            "class": cls,
            "confidence": conf,
            "is_correct": is_corr,
            "brier": brier_val,
        }
        scored_rows.append(entry)
        by_class.setdefault(cls, []).append(entry)

    # Compute overall metrics
    all_confs = [r["confidence"] for r in scored_rows]
    all_labels = [r["is_correct"] for r in scored_rows]
    avg_brier = brier_score(all_confs, all_labels)
    overall_auroc = auroc(all_confs, all_labels)

    class_metrics = {}
    for cls, rows in by_class.items():
        confs = [r["confidence"] for r in rows]
        labels = [r["is_correct"] for r in rows]
        class_metrics[cls] = {
            "count": len(rows),
            "correct": sum(labels),
            "brier": brier_score(confs, labels),
            "auroc": auroc(confs, labels),
        }

    return {
        "total_items": len(scored_rows),
        "total_correct": sum(all_labels),
        "avg_brier": avg_brier,
        "overall_auroc": overall_auroc,
        "class_metrics": class_metrics,
        "rows": scored_rows,
    }


def main():
    ap = argparse.ArgumentParser(description="Evaluate confidence calibration (Brier score & AUROC)")
    ap.add_argument("--report", help="Path to report JSON (default: generate for batch_02)")
    ap.add_argument("--answers", default=str(REPO_ROOT / "data" / "scenarios" / "answers"),
                    help="Directory containing expected answer YAMLs")
    ap.add_argument("--state-dir", default=harness.DEFAULT_STATE, help="State dir if report is generated")
    ap.add_argument("--as-of", default="2035-09-30", help="as_of date if generating report")
    ap.add_argument("--mode", default="supervised", choices=["supervised", "autonomous"])
    args = ap.parse_args()

    answers_path = Path(args.answers)
    if args.report:
        report_data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    else:
        report_data = harness.report(args.as_of, args.mode, args.state_dir)
        total_in_report = len(report_data.get("findings", [])) + sum(
            len(report_data.get("portfolio", {}).get(k, [])) for k in ("cashback", "expiry", "late_fee_risk")
        )
        if total_in_report == 0:
            # Ingest practice batch_01 and batch_02 into a temporary state to evaluate
            import tempfile, shutil
            temp_state = tempfile.mkdtemp(prefix="eval_conf_state_")
            try:
                b1 = REPO_ROOT / "data" / "scenarios" / "batch_01"
                b2 = REPO_ROOT / "data" / "scenarios" / "batch_02"
                if b1.is_dir():
                    harness.ingest(str(b1), temp_state)
                if b2.is_dir():
                    harness.ingest(str(b2), temp_state)
                report_data = harness.report(args.as_of, args.mode, temp_state)
            finally:
                shutil.rmtree(temp_state, ignore_errors=True)

    results = eval_report(report_data, answers_path)

    print("=" * 68)
    print("Confidence Calibration Evaluation (Brier Score & AUROC)")
    print("=" * 68)
    print(f"Total findings evaluated: {results['total_items']}")
    print(f"Total correct:            {results['total_correct']}")
    print("-" * 68)
    print(f"{'CLASS':<20} {'ITEMS':<8} {'CORRECT':<10} {'BRIER SCORE':<14} {'AUROC':<15}")
    print("-" * 68)

    for cls, m in sorted(results["class_metrics"].items()):
        br_str = f"{m['brier']:.4f}" if m["brier"] is not None else "N/A"
        au_str = f"{m['auroc']:.4f}" if m["auroc"] is not None else "not measurable"
        print(f"{cls:<20} {m['count']:<8} {m['correct']:<10} {br_str:<14} {au_str:<15}")

    print("-" * 68)
    tot_br = f"{results['avg_brier']:.4f}" if results['avg_brier'] is not None else "N/A"
    tot_au = f"{results['overall_auroc']:.4f}" if results['overall_auroc'] is not None else "not measurable"
    print(f"{'OVERALL':<20} {results['total_items']:<8} {results['total_correct']:<10} {tot_br:<14} {tot_au:<15}")
    print("=" * 68)
    print(f"Average Brier Score: {tot_br}  (lower is better, 0.0 is perfect)")
    print(f"Overall AUROC:       {tot_au}  (higher is better, 1.0 is perfect)")
    print("=" * 68)


if __name__ == "__main__":
    main()
