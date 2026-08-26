# Computer-Use Automation System — Core Banking Lite

An AI-agent automation system that drives a legacy-style, server-rendered
back-office banking UI (no API, no test IDs) the way a human operator would:
an LLM-driven agent discovers how to complete a task, that run is distilled
into a typed, replayable **artifact**, and production traffic replays that
artifact deterministically — with no LLM in the loop — until it hits a
condition it doesn't recognize, at which point a human can take over the
same live session and hand control back.

## What's here

- **`target_app/`** — "core-banking-lite": a small Flask app standing in for
  a legacy back-office terminal. Table-based layout, no `data-testid`s,
  session-based auth with a real inactivity timeout, and deliberately
  real failure conditions (member not found, a $25 minimum-deposit
  validation rule, session expiry).
- **`agent/`** — the automation system:
  - `schema.py` — the artifact contract (locators with fallback chains,
    steps, an expected-outcome taxonomy, checkpoints)
  - `browser.py` — Playwright wrapper: observes the page as a compact
    accessibility-tree-style state, resolves locators via role+name first
    with text/css fallbacks
  - `llm_client.py` — the LLM seam: a real `AnthropicLLMClient`
    (tool-calling) and a `ReactiveMockBrain` that satisfies the same
    interface without any API calls, for offline development/testing
  - `discover.py` — the observe→decide→act loop; records a typed artifact
    from a successful run
  - `replay.py` — deterministic replay of a saved artifact; classifies
    runtime conditions as business outcomes, recoverable conditions, or
    hard failures, and recovers/retries where it knows how
  - `guardrails.py` — allowlist enforcement + secret/PII redaction
  - `escalation.py` — pause/handoff/resume on the live session when the
    agent (or replay) gets stuck
  - `merge_outcomes.py` / `add_session_expiry_outcome.py` — small
    artifact-curation utilities (see REPORT.md, "Determinism & error
    handling")
- **`artifacts/`** — recorded capabilities (JSON)
- **`evidence/`** — structured JSONL logs, screenshots, and escalation
  records from real runs (see below for which files map to which scenario)
- **`REPORT.md`** — the design write-up

## Setup

```powershell
python -m venv venv
venv\Scripts\activate
pip install flask playwright anthropic
playwright install chromium
```

(`anthropic` is only required if you want to use the real Claude client;
the mock brain works without it.)

## Running it

**1. Start the target app** (leave running in its own terminal):
```powershell
cd target_app
python app.py
```

**2. Set credentials** the agent will use (never persisted in artifacts):
```powershell
$env:BANK_USERNAME = "operator1"
$env:BANK_PASSWORD = "correcthorse"
```

**3. Run discovery** to record a capability. On Windows, use `--params-file`
to avoid shell-quoting problems:
```powershell
python -m agent.discover `
  --goal "look up member 10001 and read their current savings balance" `
  --target "http://127.0.0.1:5001/login" `
  --params-file params_balance.json `
  --name balance_lookup
```

**4. Replay it deterministically** (no LLM calls):
```powershell
python -m agent.replay --artifact artifacts\balance_lookup.json --params-file params_balance.json
```

**5. Use the real Claude client instead of the mock brain**, set:
```powershell
$env:ANTHROPIC_API_KEY = "sk-..."
```
`make_llm_client()` in `llm_client.py` automatically switches to
`AnthropicLLMClient` when this is set.

## Demo scenarios reproduced in `evidence/`

| Scenario | How to reproduce |
|---|---|
| Discovery + replay success (balance lookup) | `discover.py` then `replay.py` on `balance_lookup.json`, `member_id=10001` |
| Business outcome: member not found | same artifact, `member_id=99999` |
| Recoverable: session expiry mid-replay | `replay.py --inject-delay-after-step s4 --inject-delay-seconds 8` with a shortened `SESSION_TIMEOUT_SECONDS` in `target_app/app.py` (demo-only flag; see REPORT.md) |
| Escalation/handoff: agent genuinely stuck | `discover.py` with a goal outside the app's known capabilities (e.g. "print their mailing address") |
| Business outcome: validation error (deposit < $25) | `open_subaccount_to_confirm.json`, `initial_deposit=10` |
| Risky/irreversible step completed | `open_subaccount_and_confirm.json` — goal phrase includes "and confirm it" |

See `REPORT.md` for the full design rationale, including several real bugs
found and fixed while building this (checkpoint selection after a
navigating step, an infinite retry loop on a genuinely-invalid input, and a
stale-URL/stale-banner ordering issue) — each documented because they're
instructive about the actual failure modes this kind of system has to
handle, not just the ones anticipated up front.
