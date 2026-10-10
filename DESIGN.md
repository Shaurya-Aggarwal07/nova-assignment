# Design

## Fact model

Documents are ingested by extracting text using `grader/extract.py`. An LLM (Claude) parses each document into structured JSON facts: `Contract`, `Amendment`, `RateNotice`, `Invoice`, `Waiver`, `Email`, `TrackingEvent`. These facts are keyed by a unique document ID (e.g. `batch_01/001.eml#INV.pdf`) and stored in `--state-dir`. Each extracted fact explicitly cites its source document, page, and the exact extracted text for traceability.

## Conflicts

The `report` phase runs deterministically in Python. When multiple sources dictate a term (e.g. rate), they are sorted by precedence: signed amendment > signed MSA > schedule 1 email > records > other. If two sources have the same precedence level, overlap in effective dates, and dictate different terms (e.g. two signed amendments), they are flagged as a `conflict` and escalated for human review.

## Confidence

Confidence is calculated deterministically as a combination of:
1. Complete evidence: Are all required documents held (e.g., tracking export, contract)?
2. Source authority: Did a signed contract dictate the term, or was it a lower-authority email?
3. Agreement: Do the invoice, AWB, and tracking export agree on dates and weights?
These are mapped to probabilities (e.g. 0.95 for perfect match, 0.70 for missing non-critical records).

## People in the loop

In `supervised` mode, all outbound actions (`draft:dispute`, `draft:claim_credit`) wait for a person. In `autonomous` mode, outbound actions under $5,000, without conflicts, and with complete evidence are executed automatically. Any outbound action >= $5,000 or failing the unattended-action test is escalated (`human_review`). `escalate` and `internal:*` actions are automatic in both modes.

## Feedback

Feedback from `feedback.json` is stored in `--state-dir` alongside facts. During the deterministic `report` phase, the pipeline checks for standing scopes (which apply to all subsequent matching evaluations) and one-off scopes (which apply only to the specific key). This prevents feedback from spreading beyond its stated `scope`.

## New documents

When a new document (e.g. a backdated amendment) arrives in a new batch, it is ingested into `--state-dir`. The `report` phase re-runs on all facts received up to `--as-of`. It naturally discovers that the new amendment now overrides the MSA for that pickup date, changing the verdict. We track previous reports to output `supersedes` or `retro: true` when a finding changes due to new evidence.

## Cost

Assuming 1,000 documents (emails, PDFs, Excel), with an average of 5 pages each, using Claude 3.5 Sonnet or Haiku:
- Text extraction is local (free).
- Prompt tokens: ~5,000 per doc. Output tokens: ~500 per doc.
- Cost per 1,000 docs: ~5,000,000 input tokens ($15) + 500,000 output tokens ($7.50) = ~$22.50.
- Time per 1,000 docs: ~10 minutes with concurrent API calls.

## What I'd do next

With another week, I would implement robust table-extraction techniques specifically for complex tabular invoices, and build a more granular fact validation layer (e.g. using Pydantic) to catch LLM hallucinated fields before they enter the state directory. I would also write a comprehensive suite of negative test cases.
