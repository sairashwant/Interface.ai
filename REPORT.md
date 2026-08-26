# REPORT

## Architecture

The system has four stages that share two data contracts (`Action` and
`Artifact`/`Step`), so nothing downstream needs to know whether an action
came from an LLM or a replay engine, and nothing needs to know whether a
step came from discovery or from a later curation edit:

```
Discovery (LLM-driven)          Replay (deterministic)
─────────────────────           ──────────────────────
observe() ──► decide() ──► act()      resolve locator ──► act()
     ▲              │                       │
     └──────────────┘                 check expected_outcomes
                                             │
     record each act() as a Step     business_outcome / recoverable /
     into an Artifact                hard_failure / continue
```

`browser.py` is the only thing that talks to Playwright. It exposes exactly
two primitives to everything else: `observe()` (a compact JSON snapshot —
URL, title, visible interactive elements by role+accessible-name, visible
alert/status banners — deliberately not a screenshot or raw DOM dump) and
`resolve(locator)` (try a primary strategy, then each fallback, in order).
Both discovery and replay call these same two methods, so "the way we find
things" and "the way we execute things" is identical in both modes; the
only thing that differs is *where the next action comes from* (an LLM call
vs. a recorded step).

`llm_client.py` defines that seam explicitly: `LLMClient.decide(goal,
params, state, turn) -> Action`. `AnthropicLLMClient` implements it with a
real tool-calling loop against Claude. `ReactiveMockBrain` implements it
with page-state-reactive rules and no network calls — useful for
development, and also a legitimate design point: it's the same interface a
cheaper/faster non-LLM heuristic could occupy in production for very
high-volume, very stable capabilities, if an artifact were mature enough
that discovery itself became rare.

`discover.py` is the orchestration loop: observe → decide → check
allowlist → act → classify the resulting banner (if any) against a known
taxonomy → record a `Step`. It also owns escalation triggering
(`action.kind == "stuck"`) and the checkpoint-selection logic described
below.

`replay.py` re-executes a saved artifact's steps directly, with no decision
step at all — it resolves each step's locator, performs the action,
checks that step's recorded `expected_outcomes`, and either stops (success,
business outcome, or hard failure) or attempts the documented recovery and
retries.

## Artifact schema

An artifact is the production contract, not a transcript. The raw
observe/decide/act history from discovery is discarded once a run
succeeds; only this survives:

```
Artifact
├── entry_url, target_app, goal_template
├── input_params  : [{name, type, required, description}]
├── output_params : [{name, type, required}]
├── steps         : [Step]
└── checkpoint    : Checkpoint
```

Each `Step` carries:
- **`locator`**: a primary strategy (`role`, e.g. `"textbox::Member ID"`)
  plus an ordered list of fallbacks (typically a plain-text match). Every
  action resolves through the same fallback chain in both discovery and
  replay, so an artifact degrades gracefully if a minor layout change
  breaks the primary strategy but the element's text/role is unchanged.
- **`risk`** (`safe` | `risky`) and **`requires_confirmation`**: state-
  changing/irreversible actions (in this app, specifically "Confirm and
  Open Account") are flagged automatically by name-matching against a
  small `RISKY_ACTION_NAMES` set at record time. Every other recorded step
  in both capabilities here is `safe`.
- **`expected_outcomes`**: a list of `(detect_locator, outcome_class, code,
  message_template, retry)` tuples. `outcome_class` is one of
  `business_outcome` / `recoverable` / `hard_failure` — this is the whole
  error taxonomy, attached per-step rather than globally, because the same
  banner text means different things depending on which step just ran.

Credentials are never written into an artifact. A typed value that came
from a `username`/`password` field is stored as the template string
`{{env:BANK_USERNAME}}` / `{{env:BANK_PASSWORD}}`, resolved from the
environment only at replay time. Any other typed/selected value that
matches a discovery-time parameter is stored as `{{param_name}}`; anything
else is stored literally (in practice, this never happened in this app —
every meaningful value traced back to either credentials or a declared
input parameter).

**Discovery only records outcomes it actually observes.** The first
`balance_lookup` discovery run used a valid member ID, so it never saw the
"member not found" banner, and the artifact had no `expected_outcomes` on
the search step at all. Rather than treat this as a discovery-completeness
problem to solve by exhaustively enumerating every path in one run, I
added `merge_outcomes.py`: a small utility that runs a second, targeted
discovery pass (deliberately probing the error path) and merges any newly-
observed outcome onto the matching step of an existing artifact, by
locator identity. This mirrors a realistic production workflow — a
reviewer (or a scheduled probe) periodically extends an artifact's known-
outcomes coverage without re-recording the whole capability — and it's
also how the `session_expired` outcome got attached (via
`add_session_expiry_outcome.py`, since a real session timeout essentially
never occurs naturally inside a single discovery run and had to be
curated in directly, documented in that file as exactly what it is: a
human-added known error branch, not something discovery stumbled into).

## Determinism & error handling

Replay checks each step's `expected_outcomes` immediately after executing
it and classifies what it sees into exactly three buckets:

- **`business_outcome`** — a legitimate answer to the goal, not a failure.
  `member_not_found` and `validation_error_deposit_min` (deposit below the
  $25 minimum) both terminate replay with `ResultStatus.BUSINESS_OUTCOME`
  and the relevant human-readable message — the caller gets a clean,
  correct "no" instead of an exception.
- **`recoverable`** — a known, self-healing condition. `session_expired` is
  the one implemented here: on detection, replay re-authenticates using
  the same env-sourced credentials, then **rewinds** to redo any
  contiguous `TYPE`/`SELECT` steps immediately preceding the step that
  hit the redirect (a fresh login lands on an empty form, so re-clicking
  "Search" without re-typing the member ID would search for nothing) and
  retries. A recovery-attempt budget (3, across the whole run) guards
  against a persistently-recurring condition looping forever.
- **`hard_failure`** — stop, report exactly which step, what was expected,
  and what was actually observed. `invalid_credentials` is the only
  taxonomy entry classified this way in this app.

Anything **not** in the taxonomy (an exception resolving or executing a
step — the condition discovery never saw) is treated conservatively:
raise a real escalation, hand off to an operator once, retry, and only
then report failure with full context. This is deliberately the least
sophisticated path — an unknown failure should always be visible and
debuggable, never silently retried into a loop or silently swallowed.

**Four real bugs surfaced while building and testing this, each worth
documenting because they're representative of the actual failure modes,
not the ones I anticipated writing the code:**

1. *Checkpoint selection after a navigating step.* My first cut set an
   artifact's checkpoint to "the last recorded step's own locator." That's
   wrong whenever the last action *navigates away* — e.g. clicking
   "Continue" lands on the confirmation page, where "Continue" no longer
   exists, so replay would fail its own success case. Fixed by deriving
   the checkpoint from the page actually observed when the agent called
   `finish` (`pick_checkpoint_locator`): prefer the last extract step's
   locator, else a distinctive button on that final page, else a *known-
   stable* substring of a visible banner.
2. *Retrying a genuinely-invalid input forever.* The first validation-error
   handling logic retyped the same deposit value after seeing the "$25
   minimum" banner — correct if the value was stale, wrong (infinite loop,
   `max_turns` exceeded) if the *requested* value itself was below $25.
   Fixed by comparing the requested value against the known constraint
   before deciding to retry vs. treat it as the goal's legitimate answer.
3. *Stale banner text as a checkpoint.* The sub-account success banner
   embeds a freshly generated account ID (`SA-XXXXXXXX`, different every
   run) — using the raw banner text as a checkpoint would never match on
   replay. Fixed by matching against a fixed set of known-stable
   substrings first, only falling back to (truncated) raw banner text as
   a last resort.
4. *Stale URL vs. actual page content.* This app's confirm-and-create
   POST handler renders the success page directly without a redirect, so
   the URL still reads `.../sub-account/confirm` even after the account
   was created. A URL-first branch order re-clicked "Confirm and Open
   Account" on the success page, where it no longer exists. Fixed by
   checking for the success banner *before* any URL-pattern branch — the
   general lesson (also written into the mock brain's system-prompt
   equivalent instructions) being: trust observed page content over the
   URL when they disagree.

## Heterogeneity & multi-tenant design

Three things are deliberately factored to be per-target-app, so adding a
new bank/tenant/legacy surface means adding these, not touching the
engine:

- **`extract_targets.py`** — the mapping from natural-language "what to
  extract" names to concrete locators for non-interactive text (table
  cells, not role-bearing elements). This is the one piece of genuinely
  app-specific knowledge injected into discovery.
- **`guardrails.DEFAULT_ALLOWLIST`** — per-tenant domain/route/action-type
  policy. A multi-tenant deployment would key this by tenant ID and load
  the matching `Allowlist` before any session starts.
- **The `KNOWN_BANNER_OUTCOMES` taxonomy in `discover.py`** — the mapping
  from a banner substring to `(outcome_class, code, retry)`. Every other
  app will phrase its errors differently; this table is the adapter.

`Artifact.target_app` and `allowlist_tags` exist specifically so a fleet
of recorded capabilities can be filtered/routed by which tenant/app they
were recorded against, and so an artifact recorded under one policy
doesn't silently get replayed under a looser one.

What this project does **not** attempt (see Cuts): a generic "any legacy
desktop surface" abstraction. `browser.py` is Playwright-specific;
supporting a genuinely non-web legacy client (e.g. a Windows desktop app)
would need a different `observe()`/`resolve()` implementation behind the
same two-method interface, not a rewrite of discover.py/replay.py.

## Escalation & handoff

Detection: `discover.py` calls `EscalationManager.raise_intervention()`
whenever the LLM/mock brain returns `kind="stuck"` — in this app, that
happens when the agent lands on a page it can map to no known task for the
given goal (verified live: a goal asking for "mailing address," a
capability the app has no way to fulfill, triggered exactly this,
producing three clean escalation records before the run gave up). Each
call writes a JSON `InterventionRequest` (goal, current URL, reason,
request ID) plus a screenshot to `evidence/escalations/`.

Control transfer is real, not simulated: the automation and the "operator"
share the exact same `BrowserSession`/Playwright `page` object.
`EscalationManager.handoff()` hands that live page to an `operator_fn`,
logs every action it takes as `operator_action` events, then returns
control to the caller, which resumes from whatever state the operator left
the page in. `mock_operator_takes_over()` is a deliberately narrow stand-in
(it only knows how to fix the search-not-found scenario) — the point of
the demo evidence is proving the *pause → transfer → log → resume*
mechanism works even when the operator has no fix for the specific stuck
state, which is realistic (real operators don't solve every stuck state
either).

What's explicitly out of scope, and why: a real-time, multi-user co-
browsing UI. In a real deployment, an operator process would attach to the
same browser over its CDP endpoint (`BrowserSession.cdp_endpoint()` is left
as a documented, unimplemented seam for exactly this) rather than an
in-process function call. Building that UI wasn't where the interesting
design judgment lived for this exercise; the control-transfer *mechanism*
(same session, logged actions, clean resume) is real and was the part
worth getting right.

## Safety

- **Allowlist enforcement** (`guardrails.Allowlist`) is checked before
  every navigation and before every action type, in both discovery and
  replay — not just at the start of a run. A policy violation raises
  `PolicyError` and stops the run rather than being silently ignored.
- **Redaction**: every log line passes through `redact_dict()` before
  being written — field names like `password` are redacted regardless of
  content, and values matching SSN/card-number-shaped patterns are
  redacted regardless of field name. Verified directly:
  `redact_dict({'password': 'secret123', 'member_id': '10001'})` →
  `{'password': '[REDACTED]', 'member_id': '10001'}`.
- **No secrets in artifacts**: credentials are stored as
  `{{env:VAR}}` templates, never literal values, in every recorded
  artifact — verified by inspecting `balance_lookup.json` directly.
- **Risk-gated irreversible actions**: any action whose target name
  matches a small `RISKY_ACTION_NAMES` set (here, "Confirm and Open
  Account") is recorded with `risk=risky, requires_confirmation=True`.
  Replay logs a `risky_step_confirmation_gate` event at that step; a
  stricter deployment could pause for an explicit human/system approval
  token at exactly that point without changing anything else about the
  engine.

## Cuts

Given the scope of a take-home rather than a production system, these were
deliberately not built, and why:

- **Only one target app**, purpose-built to be legacy-flavored (table
  layout, no test IDs) rather than automating a real third-party site.
  This was a deliberate tradeoff: a purpose-built app let me *inject*
  specific runtime conditions (validation error, not-found, session
  timeout) on demand, which is essential for demonstrating the error
  taxonomy with real evidence rather than hoping a public demo site
  happens to exhibit the right failure at the right moment.
- **No real-time operator console UI.** See Escalation & handoff above —
  the control-transfer mechanism is real; the UI a human would actually
  click through is mocked.
- **No live-API discovery evidence in this repo's own test runs.** The
  development/testing environment used for this write-up had no outbound
  network access, so all evidence here was generated with
  `ReactiveMockBrain`, not `AnthropicLLMClient`. The two satisfy the exact
  same interface (`LLMClient.decide`), and `AnthropicLLMClient` is fully
  implemented (tool-calling loop, system prompt, action mapping) — it's
  untested against the live API specifically because of the environment
  constraint, not because it's incomplete. Swapping it in requires only
  `pip install anthropic` and `ANTHROPIC_API_KEY` set; no code changes.
- **Only one recovery routine** (`session_expired` → re-authenticate +
  rewind). The `recoverable` outcome class is generic; adding more
  recovery routines (e.g. a "stale server-side pending state" outcome)
  would be additive, not a redesign.
- **No multi-tenant credential/allowlist store.** `DEFAULT_ALLOWLIST` and
  `BANK_USERNAME`/`BANK_PASSWORD` are single-tenant, hardcoded-at-the-
  environment-variable level. A real fleet would key both by tenant ID
  against a secrets manager, which is a config/deployment concern layered
  on top of this engine, not a change to it.
- **No automated test suite.** Every behavior described in this report
  (success, business outcomes, recoverable session expiry, escalation,
  risky-step completion) was verified by real, manual end-to-end runs
  against the live target app, with logs and screenshots captured in
  `evidence/` — but there's no `pytest` suite that re-runs these
  automatically. Worth adding before this became anything closer to a
  real, continuously-maintained system.
