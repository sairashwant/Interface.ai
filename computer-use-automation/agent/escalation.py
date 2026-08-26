"""
Human-in-the-loop escalation & handoff.

Scope: a full real-time co-browsing operator console is out of scope for
this project. What's real here:

  - Detection: discover.py (and later replay.py) calls raise_intervention()
    whenever the agent can't safely proceed. This writes an
    InterventionRequest (JSON) plus a screenshot, carrying the goal, the
    current step/turn, current URL, and the reason.

  - Control transfer: the automation and the "operator" share the exact
    same BrowserSession / Playwright page object -- there is no second
    browser, no fresh session. handoff() hands the live page to an
    operator_fn (a stand-in for a human clicking around in a real console),
    logs every action the operator takes, then returns control to the
    caller, who resumes from the current (now operator-modified) state.

  - In a real deployment, an operator process would attach to the same
    browser over its CDP endpoint instead of this in-process function call
    -- documented as the seam, not built, given scope.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Callable

from .browser import BrowserSession


@dataclass
class InterventionRequest:
    request_id: str
    run_id: str
    capability: str
    goal: str
    step_or_turn: str
    url: str
    reason: str
    screenshot_path: str
    created_at: str
    control: str = "automation"  # "automation" | "operator"
    resolved: bool = False
    operator_actions: list = field(default_factory=list)


class EscalationManager:
    def __init__(self, evidence_dir: str, logger=None):
        self.evidence_dir = evidence_dir
        self.logger = logger
        os.makedirs(os.path.join(evidence_dir, "escalations"), exist_ok=True)

    def raise_intervention(
        self,
        session: BrowserSession,
        run_id: str,
        capability: str,
        goal: str,
        step_or_turn: str,
        reason: str,
    ) -> InterventionRequest:
        request_id = "esc-" + uuid.uuid4().hex[:8]
        shot_path = os.path.join(self.evidence_dir, "escalations", f"{request_id}.png")
        try:
            session.screenshot(shot_path)
        except Exception:
            shot_path = ""
        req = InterventionRequest(
            request_id=request_id,
            run_id=run_id,
            capability=capability,
            goal=goal,
            step_or_turn=str(step_or_turn),
            url=session.page.url,
            reason=reason,
            screenshot_path=shot_path,
            created_at=datetime.utcnow().isoformat() + "Z",
        )
        self._persist(req)
        if self.logger:
            self.logger.log("escalation_raised", request_id=request_id, reason=reason,
                             url=req.url, screenshot=shot_path)
        return req

    def handoff(
        self,
        session: BrowserSession,
        req: InterventionRequest,
        operator_fn: Callable[[BrowserSession], list],
    ) -> InterventionRequest:
        req.control = "operator"
        if self.logger:
            self.logger.log("control_transferred_to_operator", request_id=req.request_id, url=session.page.url)

        actions_taken = operator_fn(session) or []
        req.operator_actions.extend(actions_taken)

        if self.logger:
            for a in actions_taken:
                self.logger.log("operator_action", request_id=req.request_id, action=a)

        req.control = "automation"
        req.resolved = True
        self._persist(req)
        if self.logger:
            self.logger.log("control_returned_to_automation", request_id=req.request_id,
                             resulting_url=session.page.url)
        return req

    def _persist(self, req: InterventionRequest) -> None:
        path = os.path.join(self.evidence_dir, "escalations", f"{req.request_id}.json")
        with open(path, "w") as f:
            json.dump(asdict(req), f, indent=2)


def mock_operator_takes_over(target_member_id: str) -> Callable[[BrowserSession], list]:
    """Stand-in for a real operator console: given control of the live
    session, performs the manual steps a human would (here: correctly
    searching for a valid member), and returns a human-readable action log."""
    def _do(session: BrowserSession) -> list:
        actions = []
        page = session.page
        try:
            if "/members/search" in page.url:
                page.fill("input[name=member_id]", target_member_id)
                actions.append(f"operator filled Member ID with {target_member_id}")
                page.click("input[type=submit]")
                actions.append("operator clicked Search")
        except Exception as e:
            actions.append(f"operator action failed: {e}")
        return actions

    return _do