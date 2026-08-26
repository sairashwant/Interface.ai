"""
Deterministic replay: the production execution path an AI agent would
actually trigger. No LLM in the decision loop -- every action comes
straight from the artifact's recorded steps, resolved via the same
role-first/fallback-chain locator strategy discovery used.

Usage:
  python -m agent.replay --artifact artifacts/balance_lookup.json \
      --params '{"member_id":"10001"}' --run-id replay1

  # On Windows, --params-file avoids shell-quoting problems entirely:
  python -m agent.replay --artifact artifacts/balance_lookup.json \
      --params-file params.json --run-id replay1

Error/outcome handling:
  - Each step's `expected_outcomes` (recorded during discovery) are checked
    right after the step executes. A match short-circuits the run with the
    matching classification:
      * BUSINESS_OUTCOME -> ResultStatus.BUSINESS_OUTCOME (not a failure)
      * RECOVERABLE       -> engine attempts the documented recovery
                              (currently: re-authenticate on session expiry,
                              rewinding to redo any form-fill steps a fresh
                              login page reset) then retries, up to
                              a recovery-attempt budget
      * HARD_FAILURE       -> ResultStatus.FAILURE, stop immediately
  - Any *unclassified* exception while resolving/executing a step (a
    condition discovery never saw) is treated conservatively: raise a real
    escalation (screenshot + context), attempt one operator handoff, retry
    once, and if it still fails, report FAILURE with enough detail (which
    step, what was expected, what was observed) to debug -- never a silent
    crash.

Note: --inject-delay-after-step / --inject-delay-seconds are DEMO-ONLY
flags used to deterministically exercise the RECOVERABLE session-expiry
path in evidence generation (a real replay run has no artificial delays).
The delay fires at most once per run, and the RECOVERABLE branch overall
is capped at MAX_RECOVERY_ATTEMPTS, to avoid an infinite delay/expire/retry
loop if a recoverable condition keeps recurring.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from datetime import datetime

from .browser import BrowserSession
from .schema import Artifact, ActionType, OutcomeClass, ReplayResult, ResultStatus, Step, Locator
from .guardrails import DEFAULT_ALLOWLIST, PolicyError
from .logging_utils import RunLogger
from .escalation import EscalationManager, mock_operator_takes_over

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE_DIR = os.path.join(REPO_ROOT, "evidence")

_TEMPLATE_RE = re.compile(r"^\{\{(.+)\}\}$")


def resolve_value(template, params: dict):
    if template is None:
        return None
    m = _TEMPLATE_RE.match(template)
    if not m:
        return template
    key = m.group(1)
    if key.startswith("env:"):
        env_var = key.split(":", 1)[1]
        val = os.environ.get(env_var)
        if val is None:
            raise RuntimeError(f"Required environment variable {env_var} is not set.")
        return val
    if key not in params:
        raise RuntimeError(f"Missing required input parameter: {key}")
    return str(params[key])


def _reauthenticate(session: BrowserSession, logger: RunLogger) -> None:
    """Recovery routine for a RECOVERABLE session_expired outcome: re-run the
    login mini-flow using the same env-sourced credentials, then let the
    caller retry the step that triggered the redirect."""
    username = os.environ.get("BANK_USERNAME")
    password = os.environ.get("BANK_PASSWORD")
    if not username or not password:
        raise RuntimeError("Session expired and BANK_USERNAME/BANK_PASSWORD are not set for recovery.")
    logger.log("recovery_reauthenticate_start", url=session.page.url)
    session.fill(Locator(strategy="role", value="textbox::Username"), username)
    session.fill(Locator(strategy="role", value="textbox::Password"), password)
    session.click(Locator(strategy="role", value="button::Sign In"))
    logger.log("recovery_reauthenticate_done", url=session.page.url)


def run_replay(artifact: Artifact, params: dict, run_id: str, headless: bool = True,
                inject_delay_after_step: str = None, inject_delay_seconds: int = 0) -> ReplayResult:
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    log_path = os.path.join(EVIDENCE_DIR, f"replay_{run_id}.jsonl")
    logger = RunLogger(log_path, run_id)
    escalation = EscalationManager(EVIDENCE_DIR, logger=logger)
    started_at = datetime.utcnow().isoformat() + "Z"
    logger.log("replay_started", artifact_id=artifact.artifact_id, artifact_name=artifact.name, params=params)

    for p in artifact.input_params:
        if p.required and p.name not in params:
            result = ReplayResult(
                status=ResultStatus.FAILURE, artifact_id=artifact.artifact_id, run_id=run_id,
                message=f"Missing required input parameter: {p.name}",
                started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
            )
            logger.log("replay_finished", **result.to_dict())
            logger.close()
            return result

    session = BrowserSession(headless=headless)
    outputs = {}
    result = None
    try:
        DEFAULT_ALLOWLIST.check_navigate(artifact.entry_url)
        session.goto(artifact.entry_url)
        logger.log("navigate", url=artifact.entry_url)

        i = 0
        delay_already_injected = False
        recovery_attempts = 0
        MAX_RECOVERY_ATTEMPTS = 3
        while i < len(artifact.steps):
            step: Step = artifact.steps[i]
            DEFAULT_ALLOWLIST.check_navigate(session.page.url)
            logger.log("step_start", step_id=step.step_id, action=step.action.value,
                       description=step.description)

            if step.risk.value == "risky" and step.requires_confirmation:
                logger.log("risky_step_confirmation_gate", step_id=step.step_id)

            try:
                value = resolve_value(step.value, params) if step.value else None
                if step.action == ActionType.CLICK:
                    session.click(step.locator, timeout_ms=step.timeout_ms)
                elif step.action == ActionType.TYPE:
                    session.fill(step.locator, value, timeout_ms=step.timeout_ms)
                elif step.action == ActionType.SELECT:
                    session.select(step.locator, value, timeout_ms=step.timeout_ms)
                elif step.action == ActionType.EXTRACT:
                    text = session.extract_text(step.locator, timeout_ms=step.timeout_ms)
                    outputs[step.extract_as] = text
                elif step.action == ActionType.WAIT_FOR:
                    session.wait_for(step.locator, timeout_ms=step.timeout_ms)
                elif step.action == ActionType.NAVIGATE:
                    session.goto(value or artifact.entry_url)

            except Exception as exc:
                logger.log("unclassified_step_error", step_id=step.step_id, error=str(exc))
                req = escalation.raise_intervention(
                    session, run_id, artifact.name, artifact.goal_template, step.step_id,
                    reason=f"Unclassified error executing step {step.step_id}: {exc}",
                )
                escalation.handoff(session, req, mock_operator_takes_over(params.get("member_id", "")))
                try:
                    if step.action == ActionType.CLICK:
                        session.click(step.locator, timeout_ms=step.timeout_ms)
                    elif step.action == ActionType.TYPE:
                        session.fill(step.locator, value, timeout_ms=step.timeout_ms)
                except Exception as exc2:
                    result = ReplayResult(
                        status=ResultStatus.FAILURE, artifact_id=artifact.artifact_id, run_id=run_id,
                        failed_step_id=step.step_id,
                        expected=f"step {step.step_id} ({step.description}) to succeed",
                        observed=f"{exc2}",
                        message="Step failed even after human handoff; stopping.",
                        started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
                    )
                    break

            if inject_delay_after_step and step.step_id == inject_delay_after_step and inject_delay_seconds > 0 \
               and not delay_already_injected:
                logger.log("demo_injected_delay", step_id=step.step_id, seconds=inject_delay_seconds)
                time.sleep(inject_delay_seconds)
                delay_already_injected = True

            post_state = session.observe()
            matched = None
            for eo in step.expected_outcomes:
                try:
                    if session.wait_for(eo.detect, timeout_ms=800):
                        matched = eo
                        break
                except Exception:
                    continue

            if matched:
                logger.log("outcome_matched", step_id=step.step_id, code=matched.code,
                           outcome_class=matched.outcome_class.value)
                if matched.outcome_class == OutcomeClass.BUSINESS_OUTCOME:
                    result = ReplayResult(
                        status=ResultStatus.BUSINESS_OUTCOME, artifact_id=artifact.artifact_id, run_id=run_id,
                        business_outcome_code=matched.code, outputs=outputs,
                        message=matched.message_template,
                        started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
                    )
                    break
                if matched.outcome_class == OutcomeClass.HARD_FAILURE:
                    result = ReplayResult(
                        status=ResultStatus.FAILURE, artifact_id=artifact.artifact_id, run_id=run_id,
                        failed_step_id=step.step_id,
                        expected="step to complete without a hard-failure banner",
                        observed=matched.message_template,
                        message=f"Hard failure classified at step {step.step_id}: {matched.code}",
                        started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
                    )
                    break
                if matched.outcome_class == OutcomeClass.RECOVERABLE:
                    recovery_attempts += 1
                    if recovery_attempts > MAX_RECOVERY_ATTEMPTS:
                        result = ReplayResult(
                            status=ResultStatus.FAILURE, artifact_id=artifact.artifact_id, run_id=run_id,
                            failed_step_id=step.step_id,
                            expected="recoverable condition to resolve within retry budget",
                            observed=f"still matching {matched.code} after {recovery_attempts - 1} recovery attempts",
                            message="Exceeded recovery-retry budget; stopping to avoid an infinite loop.",
                            started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
                        )
                        break
                    if matched.code == "session_expired":
                        try:
                            _reauthenticate(session, logger)
                        except Exception as exc:
                            result = ReplayResult(
                                status=ResultStatus.FAILURE, artifact_id=artifact.artifact_id, run_id=run_id,
                                failed_step_id=step.step_id,
                                expected="recoverable session expiry to be resolved by re-login",
                                observed=str(exc),
                                message="Recovery routine failed.",
                                started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
                            )
                            break
                        # Re-authenticating lands on a fresh page: any form
                        # fields the current step depends on were reset, so
                        # rewind to redo the contiguous run of TYPE/SELECT
                        # steps immediately preceding this one before retrying.
                        j = i
                        while j > 0 and artifact.steps[j - 1].action in (ActionType.TYPE, ActionType.SELECT):
                            j -= 1
                        logger.log("rewinding_for_recovery", from_step=step.step_id,
                                   to_step=artifact.steps[j].step_id)
                        i = j
                        continue
                    logger.log("retrying_step_after_recovery", step_id=step.step_id)
                    continue

            i += 1

        if result is None:
            checkpoint_ok = session.wait_for(artifact.checkpoint.detect, timeout_ms=3000)
            logger.log("checkpoint_check", ok=checkpoint_ok)
            if checkpoint_ok:
                result = ReplayResult(
                    status=ResultStatus.SUCCESS, artifact_id=artifact.artifact_id, run_id=run_id,
                    outputs=outputs, message="Checkpoint satisfied.",
                    started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
                )
            else:
                result = ReplayResult(
                    status=ResultStatus.FAILURE, artifact_id=artifact.artifact_id, run_id=run_id,
                    expected="checkpoint element present at end of run",
                    observed=f"not found at url={session.page.url}",
                    message="All steps ran but checkpoint was not satisfied.",
                    started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
                )

    except PolicyError as e:
        result = ReplayResult(
            status=ResultStatus.FAILURE, artifact_id=artifact.artifact_id, run_id=run_id,
            message=f"Policy violation: {e}",
            started_at=started_at, finished_at=datetime.utcnow().isoformat() + "Z",
        )
    finally:
        try:
            shot = os.path.join(EVIDENCE_DIR, "screenshots", f"replay_{run_id}_final.png")
            session.screenshot(shot)
        except Exception:
            pass
        session.close()

    logger.log("replay_finished", **result.to_dict())
    logger.close()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--params", default="{}")
    ap.add_argument("--params-file", default=None,
                     help="Path to a JSON file containing params (avoids shell-quoting issues on Windows).")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--inject-delay-after-step", default=None,
                     help="DEMO ONLY: sleep after this step_id to simulate slowness (e.g. session timeout).")
    ap.add_argument("--inject-delay-seconds", type=int, default=0)
    args = ap.parse_args()

    artifact = Artifact.load(args.artifact)
    if args.params_file:
        with open(args.params_file) as f:
            params = json.load(f)
    else:
        params = json.loads(args.params)
    run_id = args.run_id or ("replay-" + uuid.uuid4().hex[:8])

    result = run_replay(artifact, params, run_id, headless=args.headless,
                         inject_delay_after_step=args.inject_delay_after_step,
                         inject_delay_seconds=args.inject_delay_seconds)
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()