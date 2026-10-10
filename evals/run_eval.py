import json
import os
import shutil
import sys
from pathlib import Path

# Add the root directory to sys.path so we can import harness
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.harness import ingest, report, feedback

def run_evals():
    test_cases_path = Path(__file__).parent / "test_cases.json"
    checkpoint_path = Path(__file__).parent / "eval_checkpoint.json"
    
    with open(test_cases_path, "r", encoding="utf-8") as f:
        scenarios = json.load(f)
        
    checkpoint_data = {}
    if checkpoint_path.exists():
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                checkpoint_data = json.load(f)
        except json.JSONDecodeError:
            pass

    print(f"Running {len(scenarios)} evaluation scenarios...\n")
    
    passes = 0
    total_run = 0
    
    for idx, scenario in enumerate(scenarios, 1):
        name = scenario["name"]
        print(f"=== Scenario {idx}: {name} ===")
        
        # Check if already processed
        if name in checkpoint_data and checkpoint_data[name].get("status") in ("PASS", "FAIL"):
            status = checkpoint_data[name]["status"]
            print(f"(Skipping, already completed with status: {status})")
            if status == "PASS":
                passes += 1
            print()
            continue
            
        print(f"Description: {scenario['description']}")
        
        # Setup temp directories
        state_dir = Path(f"out/eval_state_{name}")
        batch_dir = Path(f"out/eval_batch_{name}")
        
        if state_dir.exists():
            shutil.rmtree(state_dir)
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
            
        batch_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)
        
        # Write documents
        for doc_name, content in scenario["documents"].items():
            (batch_dir / doc_name).write_text(content, encoding="utf-8")
            
        # Run Ingest
        try:
            ingest(str(batch_dir), str(state_dir))
        except Exception as e:
            print(f"FAIL: Error during ingest - {e}\n")
            # We don't save a crash as a permanent FAIL so it can be retried on next run (e.g., API limits)
            break 
            
        # Handle Feedback
        if "feedback" in scenario:
            fb_path = state_dir / "feedback.json"
            fb_path.write_text(json.dumps(scenario["feedback"]), encoding="utf-8")
            try:
                feedback(str(fb_path), str(state_dir))
            except Exception as e:
                print(f"Error applying feedback: {e}")
        
        # Run Report
        as_of = scenario.get("as_of", "2024-12-31")
        try:
            out_report = report(as_of, "supervised", str(state_dir))
        except Exception as e:
            print(f"FAIL: Error during report - {e}\n")
            break
            
        # Compare
        expected = scenario["expected"]
        exp_findings = expected.get("findings", [])
        exp_conflicts = expected.get("conflicts", 0)
        exp_cd = expected.get("cannot_determine", 0)
        
        all_out_findings = out_report.get("findings", [])
        act_findings = [f for f in all_out_findings if f.get("verdict") == "finding"]
        
        portfolio = out_report.get("portfolio", {})
        act_findings.extend(portfolio.get("late_fee_risk", []))
        act_findings.extend(portfolio.get("cashback", []))
        act_findings.extend(portfolio.get("expiry", []))
        
        act_conflicts = len([f for f in all_out_findings if f.get("verdict") == "conflict"])
        act_cd = len([f for f in all_out_findings if f.get("verdict") == "cannot_determine"])
        
        passed = True
        
        # Check conflicts and cannot determine
        if act_conflicts != exp_conflicts:
            print(f"  -> Conflicts mismatch: Expected {exp_conflicts}, Got {act_conflicts}")
            passed = False
            
        if act_cd != exp_cd:
            print(f"  -> Cannot Determine mismatch: Expected {exp_cd}, Got {act_cd}")
            passed = False
            
        # Check findings (False Positives / False Negatives)
        matched_expected = []
        false_positives = []
        
        for act_f in act_findings:
            act_cls = act_f.get("class")
            act_amt = float(act_f.get("amount_usd") or 0)
            
            match = None
            for exp_f in exp_findings:
                if exp_f in matched_expected:
                    continue
                if exp_f.get("class") == act_cls:
                    if "amount_usd" in exp_f:
                        if abs(float(exp_f["amount_usd"]) - act_amt) < 1.0:
                            match = exp_f
                            break
                    else:
                        match = exp_f
                        break
                        
            if match:
                matched_expected.append(match)
            else:
                false_positives.append(act_f)
                
        false_negatives = [e for e in exp_findings if e not in matched_expected]
        
        if false_positives:
            passed = False
            print("  -> False Positives:")
            for fp in false_positives:
                print(f"       Class: {fp.get('class')}, Amount: {fp.get('amount_usd')}")
                
        if false_negatives:
            passed = False
            print("  -> False Negatives:")
            for fn in false_negatives:
                print(f"       Class: {fn.get('class')}, Amount: {fn.get('amount_usd')}")
                
        status = "PASS" if passed else "FAIL"
        print(f"  -> {status}\n")
        
        if passed:
            passes += 1
            
        total_run += 1
        
        # Update checkpoint and save
        checkpoint_data[name] = {
            "status": status,
            "false_positives": len(false_positives),
            "false_negatives": len(false_negatives)
        }
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(checkpoint_data, f, indent=2)
            
    print(f"Evaluation Summary: {passes}/{len(scenarios)} total passed.")

if __name__ == "__main__":
    run_evals()
