"""
Discovery: the LLM-driven observe -> decide -> act loop, which also records
a structured artifact of the successful run.

Usage:
  python -m agent.discover --goal "look up member 10001 and read their current savings balance" \
      --target http://127.0.0.1:5001/login \
      --params '{"username":"operator1","password":"correcthorse","member_id":"10001"}' \
      --name balance_lookup --run-id demo1

Design notes:
  - Every action is checked against the allowlist BEFORE execution.
  - Every action becomes a recorded Step with a robustness-fallback Locator.
  - Known banner text (validation errors, "not found", session-expiry,
    invalid credentials) is classified into the artifact's per-step
    ExpectedOutcome list as it's observed.
  - Login credentials are never written into the artifact; they're
    referenced as {{env:BANK_USERNAME}} / {{env:BANK_PASSWORD}} templates,
    resolved from the environment at replay time.
  - "stuck" decisions trigger a real escalation + handoff before the loop
    gives up.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime

from .browser import BrowserSession, build_locator_for_ref
from .extract_targets import resolve_extract_target
from .guardrails import DEFAULT_ALLOWLIST, PolicyError
from .llm_client import make_llm_client, Action
from .logging_utils import RunLogger
from .schema import (
    Artifact, Step, ActionType, Locator, ExpectedOutcome, OutcomeClass,
    RiskLevel, ParamSpec, Checkpoint,
)
from .escalation import EscalationManager, mock_operator_takes_over

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE_DIR = os.path.join(REPO_ROOT, "evidence")
ARTIFACTS_DIR = os.path.join(REPO_ROOT, "artifacts")

# banner substring -> (OutcomeClass, code, recoverable-retry?)
KNOWN_BANNER_OUTCOMES = [
    ("no member found", OutcomeClass.BUSINESS_OUTCOME, "member_not_found", False),
    ("must be at least", OutcomeClass.BUSINESS_OUTCOME, "validation_error_deposit_min", False),
    ("session has expired", OutcomeClass.RECOVERABLE, "session_expired", True),
    ("invalid username or password", OutcomeClass.HARD_FAILURE, "invalid_credentials", False),
]

RISKY_ACTION_NAMES = {"confirm and open account"}


def classify_banners(banners: list):
    for b in banners:
        bl = b.lower()
        for substr, cls, code, retry in KNOWN_BANNER_OUTCOMES:
            if substr in bl:
                return cls, code, retry, b
    return None


def templatize_value(target_name: str, value: str, params: dict) -> str:
    tnl = (target_name or "").lower()
    if tnl == "password":
        return "{{env:BANK_PASSWORD}}"
    if tnl == "username":
        return "{{env:BANK_USERNAME}}"
    for k, v in params.items():
        if str(v) == value:
            return "{{" + k + "}}"
    return value


def run_discovery(goal: str, target_url: str, params: dict, name: str, run_id: str,
                   headless: bool = True, max_turns: int = 25):
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    log_path = os.path.join(EVIDENCE_DIR, f"discovery_{run_id}.jsonl")
    logger = RunLogger(log_path, run_id)
    escalation = EscalationManager(EVIDENCE_DIR, logger=logger)
    llm = make_llm_client()
    logger.log("run_started", goal=goal, target=target_url, llm=type(llm).__name__)

    session = BrowserSession(headless=headless)
    steps = []
    input_params_used = set()
    output_params = {}
    final_status = "failure"
    final_message = ""
    business_outcome_code = None

    try:
        DEFAULT_ALLOWLIST.check_navigate(target_url)
        session.goto(target_url)
        logger.log("navigate", url=target_url)

        turn = 0
        stuck_retries = 0
        while turn < max_turns:
            turn += 1
            state = session.observe()
            DEFAULT_ALLOWLIST.check_navigate(state["url"])
            logger.log("observe", turn=turn, url=state["url"], elements=len(state["elements"]),
                       banners=state["banners"])

            action: Action = llm.decide(goal, params, state, turn)
            logger.log("decision", turn=turn, kind=action.kind, target_role=action.target_role,
                       target_name=action.target_name, reasoning=action.reasoning)

            if action.kind == "finish":
                final_status = "business_outcome" if action.business_outcome_code else "success"
                business_outcome_code = action.business_outcome_code
                output_params.update(action.outputs)
                final_message = action.reasoning
                logger.log("run_finished", status=final_status, outputs=action.outputs,
                           business_outcome_code=action.business_outcome_code)
                break

            if action.kind == "stuck":
                stuck_retries += 1
                logger.log("agent_stuck", turn=turn, reasoning=action.reasoning)
                req = escalation.raise_intervention(
                    session, run_id, name, goal, str(turn),
                    reason=action.reasoning,
                )
                member_id = params.get("member_id", "")
                escalation.handoff(session, req, mock_operator_takes_over(member_id))
                if stuck_retries > 2:
                    final_status = "failure"
                    final_message = "Exceeded stuck-retry budget after escalation."
                    logger.log("run_finished", status=final_status, message=final_message)
                    break
                continue

            DEFAULT_ALLOWLIST.check_action_type(
                {"click": "click", "type": "type", "select": "select", "extract": "extract"}[action.kind]
            )

            step_id = f"s{len(steps) + 1}"
            risk = RiskLevel.RISKY if (action.target_name or "").lower() in RISKY_ACTION_NAMES else RiskLevel.SAFE
            requires_confirmation = risk == RiskLevel.RISKY

            if action.kind == "click":
                loc = build_locator_for_ref(action.target_role, action.target_name)
                session.click(loc)
                step = Step(step_id=step_id, action=ActionType.CLICK, locator=loc,
                            risk=risk, requires_confirmation=requires_confirmation,
                            description=action.reasoning)
            elif action.kind == "type":
                loc = build_locator_for_ref(action.target_role, action.target_name)
                session.fill(loc, action.value)
                tmpl_value = templatize_value(action.target_name, action.value, params)
                if tmpl_value.startswith("{{") and tmpl_value.strip("{}").split(":")[0] not in ("env",):
                    input_params_used.add(tmpl_value.strip("{}"))
                step = Step(step_id=step_id, action=ActionType.TYPE, locator=loc, value=tmpl_value,
                            description=action.reasoning)
            elif action.kind == "select":
                loc = build_locator_for_ref(action.target_role, action.target_name)
                session.select(loc, action.value)
                tmpl_value = templatize_value(action.target_name, action.value, params)
                if tmpl_value.startswith("{{") and ":" not in tmpl_value:
                    input_params_used.add(tmpl_value.strip("{}"))
                step = Step(step_id=step_id, action=ActionType.SELECT, locator=loc, value=tmpl_value,
                            description=action.reasoning)
            elif action.kind == "extract":
                loc = resolve_extract_target(action.target_name)
                extracted = session.extract_text(loc)
                output_params[action.extract_as] = extracted
                step = Step(step_id=step_id, action=ActionType.EXTRACT, locator=loc,
                            extract_as=action.extract_as, description=action.reasoning)
            else:
                logger.log("unknown_action", kind=action.kind)
                continue

            post_state = session.observe()
            classified = classify_banners(post_state["banners"])
            if classified:
                cls, code, retry, banner_text = classified
                matched_substr = next(s for s, c, cd, r in KNOWN_BANNER_OUTCOMES if cd == code)
                step.expected_outcomes.append(ExpectedOutcome(
                    detect=Locator(strategy="text", value=matched_substr),
                    outcome_class=cls, code=code, message_template=banner_text, retry=retry,
                ))
                logger.log("banner_classified", step_id=step_id, code=code, outcome_class=cls.value)

            steps.append(step)
            logger.log("step_recorded", **step.to_dict())

        else:
            final_status = "failure"
            final_message = f"Exceeded max_turns={max_turns} without finishing."
            logger.log("run_finished", status=final_status, message=final_message)

    except PolicyError as e:
        final_status = "failure"
        final_message = f"Policy violation: {e}"
        logger.log("policy_violation", message=str(e))
    finally:
        try:
            shot = os.path.join(EVIDENCE_DIR, "screenshots", f"discovery_{run_id}_final.png")
            session.screenshot(shot)
        except Exception:
            pass
        session.close()
        logger.close()

    checkpoint_locator = steps[-1].locator if steps and steps[-1].locator else Locator(
        strategy="css", value="body")
    artifact = Artifact(
        artifact_id=str(uuid.uuid4()),
        name=name,
        version="1.0.0",
        target_app="core-banking-lite",
        entry_url=target_url,
        description=f"Recorded capability for goal: {goal}",
        goal_template=goal,
        input_params=[ParamSpec(name=p, type="string", description="captured from discovery run")
                      for p in sorted(input_params_used)],
        output_params=[ParamSpec(name=k, type="string", required=False) for k in output_params],
        steps=steps,
        checkpoint=Checkpoint(detect=checkpoint_locator, description="Final element touched in the successful run."),
        created_at=datetime.utcnow().isoformat() + "Z",
        source_run_id=run_id,
        allowlist_tags=["core-banking-lite:v1"],
    )

    summary = {
        "status": final_status,
        "message": final_message,
        "business_outcome_code": business_outcome_code,
        "outputs": output_params,
        "log_path": log_path,
    }
    return artifact, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--params", default="{}")
    ap.add_argument("--name", required=True)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--max-turns", type=int, default=25)
    args = ap.parse_args()

    run_id = args.run_id or ("disc-" + uuid.uuid4().hex[:8])
    params = json.loads(args.params)

    artifact, summary = run_discovery(
        goal=args.goal, target_url=args.target, params=params,
        name=args.name, run_id=run_id, headless=args.headless, max_turns=args.max_turns,
    )

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    artifact_path = os.path.join(ARTIFACTS_DIR, f"{args.name}.json")
    artifact.save(artifact_path)

    print(json.dumps({"artifact_path": artifact_path, **summary}, indent=2))
    if summary["status"] == "failure":
        sys.exit(1)


if __name__ == "__main__":
    main()