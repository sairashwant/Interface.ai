"""
LLMClient is the contract. Two implementations satisfy it:

  - `AnthropicLLMClient`: real Claude, via tool-calling. Requires
    `pip install anthropic` and `ANTHROPIC_API_KEY` set.

  - `ReactiveMockBrain`: a rule-based stand-in for testing without API
    calls. It receives the *current observed page state* each turn and
    reacts to it -- no memory of "step 3 of 7" -- which is why it
    transparently survives things like a session-timeout redirect back to
    /login mid-goal, without special-case code for that situation.

Both return an `Action`, the single per-turn decision the agent loop
(discover.py) executes, observes the result of, and feeds back in next turn.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Action:
    kind: str  # "click" | "type" | "select" | "extract" | "finish" | "stuck"
    target_role: Optional[str] = None
    target_name: Optional[str] = None
    value: Optional[str] = None
    extract_as: Optional[str] = None
    outputs: dict = field(default_factory=dict)
    business_outcome_code: Optional[str] = None
    reasoning: str = ""


class LLMClient:
    def decide(self, goal: str, params: dict, state: dict, turn: int) -> Action:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Real Claude client (tool-calling agent loop)
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "click",
        "description": "Click an interactive element identified by its accessibility role and name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into a textbox element identified by role and name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "value": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "value"],
        },
    },
    {
        "name": "select_option",
        "description": "Select an option in a combobox/select element identified by role and name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "value": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "value"],
        },
    },
    {
        "name": "extract",
        "description": "Read the text of an element and store it under an output field name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "extract_as": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "extract_as"],
        },
    },
    {
        "name": "finish",
        "description": "Declare the goal complete (or a legitimate business outcome reached) and stop.",
        "input_schema": {
            "type": "object",
            "properties": {
                "outputs": {"type": "object"},
                "business_outcome_code": {"type": "string"},
                "reasoning": {"type": "string"},
            },
        },
    },
    {
        "name": "stuck",
        "description": "Declare that you cannot safely proceed and need a human to intervene.",
        "input_schema": {
            "type": "object",
            "properties": {"reasoning": {"type": "string"}},
            "required": ["reasoning"],
        },
    },
]

_SYSTEM_PROMPT = """You are driving a real web application, one action per turn, to accomplish a goal.
You will be shown the current page's URL, title, visible interactive elements (role + accessible
name), and any visible alert/status banners. Call exactly one tool per turn.
Rules:
- Prefer the most specific element name available.
- If a banner reports a legitimate business result (e.g. "no member found", a validation error),
  decide whether that IS the goal's answer (call finish with the business_outcome_code) or whether
  you should correct course (e.g. retype a field) and continue.
- If you do not recognize the situation or cannot find a safe path forward, call `stuck` and explain why.
- Never invent data; use only the parameters you were given.
"""


class AnthropicLLMClient(LLMClient):
    def __init__(self, model: str = "claude-sonnet-4-6"):
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "The 'anthropic' package is required for AnthropicLLMClient. "
                "pip install anthropic and set ANTHROPIC_API_KEY."
            ) from e
        import anthropic
        self._client = anthropic.Anthropic()
        self.model = model

    def decide(self, goal: str, params: dict, state: dict, turn: int) -> Action:
        import json as _json

        user_msg = (
            f"GOAL: {goal}\nPARAMETERS: {_json.dumps(params)}\nTURN: {turn}\n\n"
            f"CURRENT PAGE STATE:\n{_json.dumps(state, indent=2)}"
        )
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=1000,
            system=_SYSTEM_PROMPT,
            tools=_TOOLS,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_msg}],
        )
        for block in resp.content:
            if block.type == "tool_use":
                return _action_from_tool_call(block.name, block.input)
        return Action(kind="stuck", reasoning="Model did not call a tool.")


def _action_from_tool_call(name: str, inp: dict) -> Action:
    if name == "click":
        return Action(kind="click", target_role=inp["role"], target_name=inp["name"],
                       reasoning=inp.get("reasoning", ""))
    if name == "type_text":
        return Action(kind="type", target_role=inp["role"], target_name=inp["name"],
                       value=inp["value"], reasoning=inp.get("reasoning", ""))
    if name == "select_option":
        return Action(kind="select", target_role=inp["role"], target_name=inp["name"],
                       value=inp["value"], reasoning=inp.get("reasoning", ""))
    if name == "extract":
        return Action(kind="extract", target_role=inp["role"], target_name=inp["name"],
                       extract_as=inp["extract_as"], reasoning=inp.get("reasoning", ""))
    if name == "finish":
        return Action(kind="finish", outputs=inp.get("outputs", {}),
                       business_outcome_code=inp.get("business_outcome_code"),
                       reasoning=inp.get("reasoning", ""))
    if name == "stuck":
        return Action(kind="stuck", reasoning=inp.get("reasoning", ""))
    return Action(kind="stuck", reasoning=f"Unknown tool call: {name}")


# ---------------------------------------------------------------------------
# Reactive mock brain (no API calls required)
# ---------------------------------------------------------------------------

def _find(state: dict, role: str, name_substr: str) -> Optional[dict]:
    name_substr = name_substr.lower()
    for el in state["elements"]:
        if el["role"] == role and name_substr in el["name"].lower():
            return el
    return None


def _has_banner(state: dict, substr: str) -> bool:
    return any(substr.lower() in b.lower() for b in state.get("banners", []))


class ReactiveMockBrain(LLMClient):
    """Understands the two example goals this repo's target app supports:
    a balance lookup, and opening a sub-account up to (and optionally
    through) the confirmation screen."""

    def __init__(self):
        self._memory: dict = {}

    def decide(self, goal: str, params: dict, state: dict, turn: int) -> Action:
        url = state["url"]
        goal_l = goal.lower()

        if "/login" in url:
            user_field = _find(state, "textbox", "username")
            pass_field = _find(state, "textbox", "password")
            if user_field and user_field.get("value") != params.get("username"):
                return Action(kind="type", target_role="textbox", target_name="Username",
                               value=params.get("username", "operator1"),
                               reasoning="Login form needs a username.")
            if pass_field and pass_field.get("value") != params.get("password"):
                return Action(kind="type", target_role="textbox", target_name="Password",
                               value=params.get("password", "correcthorse"),
                               reasoning="Login form needs a password.")
            return Action(kind="click", target_role="button", target_name="Sign In",
                           reasoning="Credentials entered, submit login.")

        if "/members/search" in url:
            member_id = params.get("member_id", "")
            search_field = _find(state, "textbox", "member id")
            if _has_banner(state, "no member found"):
                return Action(kind="finish", business_outcome_code="member_not_found",
                               reasoning="Search returned no matching member; this is a valid outcome.")
            if search_field and search_field.get("value") == member_id and _find(state, "link", "open record"):
                return Action(kind="click", target_role="link", target_name="Open Record",
                               reasoning="Result found, open the member record.")
            if search_field and search_field.get("value") != member_id:
                return Action(kind="type", target_role="textbox", target_name="Member ID",
                               value=member_id, reasoning="Enter the target member id.")
            return Action(kind="click", target_role="button", target_name="Search",
                           reasoning="Submit the search.")

        if re.search(r"/members/[^/]+$", url):
            if "balance" in goal_l:
                if self._memory.get("extracted_balance"):
                    return Action(kind="finish", outputs={},
                                   reasoning="Savings balance already extracted; goal complete.")
                self._memory["extracted_balance"] = True
                return Action(kind="extract", target_role="text", target_name="savings balance",
                               extract_as="savings_balance",
                               reasoning="Goal asks for the current savings balance.")
            if "sub-account" in goal_l or "sub account" in goal_l:
                return Action(kind="click", target_role="link", target_name="Open New Sub-Account",
                               reasoning="Goal requires opening a new sub-account.")
            return Action(kind="stuck", reasoning="On member detail page but goal doesn't match a known task.")

        if "/sub-account/new" in url:
            if _has_banner(state, "must be at least"):
                return Action(kind="type", target_role="textbox", target_name="Initial Deposit",
                               value=str(params.get("initial_deposit", 50)),
                               reasoning="Prior deposit value failed validation; retype a valid amount.")
            acct_field = _find(state, "combobox", "account type")
            deposit_field = _find(state, "textbox", "initial deposit")
            desired_type = params.get("acct_type", "Holiday Club")
            desired_deposit = str(params.get("initial_deposit", 50))
            if acct_field and acct_field.get("value") != desired_type:
                return Action(kind="select", target_role="combobox", target_name="Account Type",
                               value=desired_type, reasoning="Choose the requested sub-account type.")
            if deposit_field and deposit_field.get("value") != desired_deposit:
                return Action(kind="type", target_role="textbox", target_name="Initial Deposit",
                               value=desired_deposit, reasoning="Enter the requested initial deposit.")
            return Action(kind="click", target_role="button", target_name="Continue",
                           reasoning="Form is filled in; proceed to confirmation.")

        if "/sub-account/confirm" in url:
            if "and confirm" in goal_l or "complete" in goal_l:
                return Action(kind="click", target_role="button", target_name="Confirm and Open Account",
                               reasoning="Goal asks to complete the sub-account opening.")
            return Action(kind="finish", outputs={"reached": "confirmation_screen"},
                          reasoning="Goal only asks to reach the confirmation screen.")

        if _has_banner(state, "was created successfully"):
            return Action(kind="finish", outputs={"sub_account_created": True},
                          reasoning="Sub-account creation confirmed by success banner.")

        return Action(kind="stuck", reasoning=f"Unrecognized state at {url}; no rule matches goal {goal!r}.")


def make_llm_client() -> LLMClient:
    """Factory: real Claude if ANTHROPIC_API_KEY is set and the SDK is
    importable, otherwise the offline reactive mock brain."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicLLMClient()
        except RuntimeError:
            pass
    return ReactiveMockBrain()