# Design Notes

## (a) The 6 Classes

1. **Overcharge**
   - **Formula**: `amount_usd = billed - correct`. `correct = round_half_up(rate * quantity, 2)` evaluated on the pickup date.
   - **Example**: An invoice charges $10/kg for 100kg, but the effective amendment on the pickup date specifies $8/kg. Amount = $1000 - $800 = $200.

2. **Duplicate**
   - **Formula**: `amount_usd = duplicated lines on the invoice with the later date`. If credited in part, `amount = overlap - credit`.
   - **Example**: Invoice A (Jan 1) bills $500 for AWB 123. Invoice B (Feb 1) bills $500 again for AWB 123. Finding on Invoice B for $500.

3. **Waived but billed**
   - **Formula**: `amount_usd = billed - correct (0)`. Condition: A Schedule 1 waivers contact sent an email naming the shipment/month waiving the charge.
   - **Example**: Carrier's Schedule 1 contact emails "We waive the Delivery fee on AWB 123." The invoice later charges a Delivery fee of $50 on AWB 123. Finding for $50.

4. **Late fee not owed**
   - **Formula**: `days_late = paid_date - extended_due_date`. Fee runs for `ceil(days_late / 30)` months. Each month is `round_half_up(r / 100 * base, 2)`. If `days_late <= grace_days`, fee is not owed.
   - **Example**: Invoice paid 5 days late, but contract allows 7 grace days. Late fee charged on subsequent invoice is a finding.

5. **Cashback / Credits (Portfolio)**
   - **Formula**: Earned and not yet credited. Qualifying spend = freight on invoices dated in period net of credit notes.
   - **Window**: Period must be closed (`as_of` > period end).
   - **Example**: Q1 rebate of 2% on $100,000 spend = $2,000. If carrier hasn't issued a credit note by `as_of`, it's a finding.

6. **Expiry and Late Fee Risk (Portfolio)**
   - **Formula/Window**: 
     - Expiry: `0 <= operative_date - as_of <= 60` days. Action is `internal:alert`.
     - Late Fee Risk: Unpaid invoice with `0 < due_date - as_of <= 14` days. Amount = one month's fee. Action is `internal:payment_reminder`.
   - **Example**: Contract ends in 30 days. Finding class: `expiry`, action: `internal:alert`.

## (b) Definitions

- **Conflict**: Two sources you hold, both with enough authority and both in force on the pickup date, give different correct amounts, and the precedence clause doesn't rank them (e.g., two signed amendments).
- **Cannot_determine**: The answer depends on a document you don't hold (named in a held document), and what you hold cannot settle the answer. Action is to request it.
- **Unattended-action test** (For autonomous mode execution):
  1. Evidence is complete.
  2. No conflict on this key or same lane.
  3. Finding is under $5,000.
  4. Action can be undone (dispute, credit claim, document request).
  5. Enough authority (terms from level 1/2, facts from level 3, sent to Schedule 1 recipient).
- **One-off vs permanent waiver**: A waiver applies *only* to the charges and shipments it explicitly names. No waiver implies a waiver of any later charge.
- **Source Authority Order**:
  1. Signed MSA, signed amendment (later beats earlier).
  2. Email from Schedule 1 person for that category.
  3. Records (tracking export, milestone emails, AWBs, ledgers, invoices).
  4. Anyone else's email, drafts.
  5. Quoted text, automated text.

## (c) JSON Schema & CLI Contract

**CLI Contract**:
- `python harness.py ingest <batch_dir> --state-dir <dir>`
- `python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] --state-dir <dir> > report.json`
- `python harness.py feedback <feedback.json> --state-dir <dir>`

**Report JSON Fields**:
- `as_of`, `mode`
- `findings`: Array of objects containing: `id`, `class`, `variant`, `invoice_id`, `verdict`, `amount_usd`, `amount_range`, `deadline`, `action`, `confidence`, `evidence` (`doc`, `page`, `quote`), `missing_doc` (`name`, `holder`), `supersedes`, `retro`, `learned_at`.
- `portfolio`: Object containing arrays for `cashback`, `expiry`, and `late_fee_risk`.

## (d) Proposed Module Layout & Data Shape

**Module Layout**:
- `harness.py`: CLI entry point.
- `reader.py`: Wraps `grader/extract.py` to parse documents into raw text.
- `extract.py`: Calls Anthropic API to parse text into structured JSON facts.
- `store.py`: Loads and saves facts and feedback in `--state-dir`.
- `conflicts.py`: Evaluates overlapping facts based on precedence rules.
- `actions.py`: Applies the unattended-action test and assigns actions (`draft:*`, `execute:*`, `escalate`, `human_review`).
- `feedback.py`: Applies standing/one-off scopes from `feedback.json` to override facts.
- `confidence.py`: Computes 0-1 confidence based on completeness, agreement, and authority.
- `checks/`:
  - `overcharge.py`, `duplicate.py`, `waiver.py`, `late_fee.py`, `cashback.py`, `expiry.py`.

**Data Shape of Extracted "Facts"**:
```json
{
  "type": "RateNotice",
  "contract_id": "TBX-US-EXP-E49",
  "lane": "USE2-INE2",
  "effective_from": "2032-01-01",
  "rate": "6.01",
  "currency": "USD",
  "weight_band": "100-299.5 kg",
  "evidence": {
    "doc": "batch_01/0002.eml#MSA.pdf",
    "page": 8,
    "quote": "USE2-INE2 100-299.5 kg 6.01"
  }
}
```
