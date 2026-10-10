# AI Use

## Tools

I used:
- **Antigravity:** Code implementation, debugging, architecture (all Python code in harness/)
- **Claude (this conversation):** Planning phases, problem-solving, prompt design

## Prompt log

- Phase 1 & 2: "Help me set up the repository structure and parse the prompt guidelines"
- Phase 3.5: "Wire extraction into ingest command. We need a way to avoid burning API credits during logic tests."
- Phase 4: "Build the state storage mechanism and hashing for caching extractions."
- Phase 5: "Implement overcharge detector comparing invoice amount vs contract rate."
- Phase 6: "Add duplicate invoice detection logic based on shipment IDs and amounts."
- Phase 7: "Add checks for waived fees, late fees, cashback credits, and expiry warnings."
- Phase 8: "Implement actions.py with supervised vs autonomous modes and strict hard gates."
- Phase 9: "Add conflict resolution and cannot_determine logic with source authority precedence."
- Phase 10: "Add concurrent extraction, exponential backoff, timeout increase to fix batch_02 failures."
- Phase 11 & 12: "Fix eval script filtering and adjust test data mock documents so they satisfy the rigid conflict and late fee parsers."

## Key iterations

**Phase 3.5 (mock/real toggle):** 
Realized API credits would run out during testing. Antigravity designed auto-detection: if ANTHROPIC_API_KEY is missing/empty, extract.py generates deterministic mock facts; if present, it calls Claude API. This saved ~$4 and enabled testing Phases 1-9 without real API calls.

**Phase 10 (timeout + rate limiting):**
Batch_02 timed out at 600s. Antigravity diagnosed three issues without running code: (1) no exponential backoff (causing thundering herd on API rate limits), (2) max_workers=5 too aggressive, (3) 600s timeout too tight. Applied fixes: exponential backoff (2^attempt sleep), reduced to 2 workers, increased timeout to 1200s. Batch_02 then completed in 752s.

**Phase 11 (eval failures):**
All 9 scenarios initially failed. Root cause: eval script was counting cannot_determine items as overcharge false positives, and mock documents were missing contract IDs. Fixed: (1) filtered findings by verdict=='finding', (2) added explicit contract IDs to test data. All 9/9 then passed.

## Lessons

1. Mock data is great for logic testing; real data reveals schema mismatches
2. Detailed logging (START/END per document) is critical for debugging async code
3. AI can diagnose bugs without running code (Antigravity's analysis of timeout was spot-on)
4. Test data quality matters as much as detector logic
5. Specific prompts save iteration (e.g., "show me what values are being compared" vs. "fix the bug")
