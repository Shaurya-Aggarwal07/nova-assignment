param (
    [string]$target = "help",
    [string]$K = ""
)

$PY = ".\.venv\Scripts\python.exe"
$HARNESS = "harness\harness.py"

function help {
    Write-Host ".\make.ps1 setup      create .venv and install harness\requirements.txt"
    Write-Host ".\make.ps1 grade      replay the practice mailbox through your harness; writes report_card.txt and .json"
    Write-Host ".\make.ps1 sheet      run the exam mailbox; writes out\exam_report.json and answer_sheet.json"
    Write-Host ".\make.ps1 eval       run your cases in evals\cases (-K <text> runs only matching files)"
    Write-Host ".\make.ps1 validate   check the repo is ready to send (SUBMISSION.md)"
    Write-Host ".\make.ps1 run        run the practice mailbox once; writes out\practice_report.json"
}

function venv {
    if (-Not (Test-Path $PY)) {
        Write-Host "no $PY yet: run .\make.ps1 setup first"
        exit 2
    }
}

switch ($target) {
    "setup" {
        # Check python version
        py -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "need Python 3.11 or newer"
            exit 2
        }
        Write-Host "Creating virtual environment..."
        py -m venv .venv
        Write-Host "Installing requirements..."
        .\.venv\Scripts\pip.exe install -q -r harness\requirements.txt
        & $PY -c "import pymupdf, openpyxl, jsonschema, yaml"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "setup done. Next: write harness\harness.py, then .\make.ps1 grade"
        }
    }
    "run" {
        venv
        & $PY tools\run_mailbox.py --data data\scenarios --harness $HARNESS --out out\practice_report.json
    }
    "grade" {
        venv
        & $PY grader\run_grader.py --harness "python $HARNESS" --data data\scenarios --key data\scenarios\answers --mode both --out report_card.json | Tee-Object -FilePath report_card.txt
    }
    "sheet" {
        venv
        & $PY tools\run_mailbox.py --data data\exam --harness $HARNESS --out out\exam_report.json --sheet answer_sheet.json
    }
    "eval" {
        venv
        if ($K) {
            & $PY evals\run_evals.py --harness $HARNESS -k $K
        } else {
            & $PY evals\run_evals.py --harness $HARNESS
        }
    }
    "validate" {
        venv
        & $PY tools\check_submission.py --harness $HARNESS
    }
    "clean" {
        if (Test-Path out\state_*) { Remove-Item -Recurse -Force out\state_* }
        if (Test-Path .state) { Remove-Item -Recurse -Force .state }
    }
    "help" {
        help
    }
    default {
        Write-Host "Unknown target: $target"
        help
    }
}
