"""
Artifact schema for recorded capabilities.

Design intent (see REPORT.md ## Artifact schema for the full rationale):

- An artifact is the *contract* an AI agent invokes in production. It is not
  a transcript of the discovery run -- the raw LLM reasoning is discarded;
  only the distilled, typed, replayable flow survives.
- Every step's target element is described by a `Locator` with a primary
  strategy plus an ordered list of fallbacks, because on a legacy surface any
  single selector strategy can break for reasons unrelated to a real UI
  change (a table row shifting, a label rewording).
- `input_params` / `output_params` are typed so a calling agent (or a human
  reviewer) can see the capability's contract without reading the steps.
- Every step also carries `expected_outcomes`, a small taxonomy of the
  runtime conditions the replay engine already knows how to recognize *for
  that step*, each tagged as business_outcome / recoverable / hard_failure.
  This is what lets replay distinguish "no such member" (business outcome)
  from "the page didn't load" (hard failure) without guessing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"


class OutcomeClass(str, Enum):
    """How the replay engine should treat a condition observed after a step."""
    BUSINESS_OUTCOME = "business_outcome"   # legitimate result the caller needs, e.g. "not found"
    RECOVERABLE = "recoverable"              # transient/known condition; engine handles and continues
    HARD_FAILURE = "hard_failure"            # stop, surface a clear debuggable error


class RiskLevel(str, Enum):
    SAFE = "safe"          # read-only / reversible
    RISKY = "risky"        # writes state, may be irreversible


@dataclass
class Locator:
    """An element/control target with a robustness fallback chain.

    `strategy` values: "role" (role+accessible name -- preferred), "text"
    (visible text match), "css", "xpath". `value` is a strategy-specific
    string; for "role" it's "ROLE::Accessible Name".
    """
    strategy: str
    value: str
    fallbacks: list["Locator"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "value": self.value,
            "fallbacks": [f.to_dict() for f in self.fallbacks],
        }

    @staticmethod
    def from_dict(d: dict) -> "Locator":
        return Locator(
            strategy=d["strategy"],
            value=d["value"],
            fallbacks=[Locator.from_dict(f) for f in d.get("fallbacks", [])],
        )


@dataclass
class ExpectedOutcome:
    """One recognized runtime condition a step (or the page state right after
    it) might produce, and how the replay engine should classify/handle it.
    """
    detect: Locator                 # element whose presence signals this condition
    outcome_class: OutcomeClass
    code: str                        # short machine-readable code, e.g. "member_not_found"
    message_template: str = ""       # human-readable description
    retry: bool = False               # if RECOVERABLE, whether the engine should retry the step
    max_retries: int = 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detect"] = self.detect.to_dict()
        d["outcome_class"] = self.outcome_class.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "ExpectedOutcome":
        return ExpectedOutcome(
            detect=Locator.from_dict(d["detect"]),
            outcome_class=OutcomeClass(d["outcome_class"]),
            code=d["code"],
            message_template=d.get("message_template", ""),
            retry=d.get("retry", False),
            max_retries=d.get("max_retries", 1),
        )


@dataclass
class Step:
    step_id: str
    action: ActionType
    locator: Optional[Locator] = None            # None for pure NAVIGATE/WAIT_FOR-by-time
    value: Optional[str] = None                  # literal or "{{param_name}}" template
    extract_as: Optional[str] = None              # output field name, for EXTRACT steps
    timeout_ms: int = 5000
    risk: RiskLevel = RiskLevel.SAFE
    requires_confirmation: bool = False
    expected_outcomes: list[ExpectedOutcome] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "action": self.action.value,
            "locator": self.locator.to_dict() if self.locator else None,
            "value": self.value,
            "extract_as": self.extract_as,
            "timeout_ms": self.timeout_ms,
            "risk": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
            "expected_outcomes": [o.to_dict() for o in self.expected_outcomes],
            "description": self.description,
        }

    @staticmethod
    def from_dict(d: dict) -> "Step":
        return Step(
            step_id=d["step_id"],
            action=ActionType(d["action"]),
            locator=Locator.from_dict(d["locator"]) if d.get("locator") else None,
            value=d.get("value"),
            extract_as=d.get("extract_as"),
            timeout_ms=d.get("timeout_ms", 5000),
            risk=RiskLevel(d.get("risk", "safe")),
            requires_confirmation=d.get("requires_confirmation", False),
            expected_outcomes=[ExpectedOutcome.from_dict(o) for o in d.get("expected_outcomes", [])],
            description=d.get("description", ""),
        )


@dataclass
class ParamSpec:
    name: str
    type: str            # "string" | "number" | "boolean"
    required: bool = True
    description: str = ""


@dataclass
class Checkpoint:
    """Final success condition for the whole capability."""
    detect: Locator
    description: str = ""

    def to_dict(self) -> dict:
        return {"detect": self.detect.to_dict(), "description": self.description}

    @staticmethod
    def from_dict(d: dict) -> "Checkpoint":
        return Checkpoint(detect=Locator.from_dict(d["detect"]), description=d.get("description", ""))


@dataclass
class Artifact:
    artifact_id: str
    name: str
    version: str
    target_app: str
    entry_url: str
    description: str
    goal_template: str
    input_params: list[ParamSpec]
    output_params: list[ParamSpec]
    steps: list[Step]
    checkpoint: Checkpoint
    created_at: str
    source_run_id: str
    allowlist_tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "version": self.version,
            "target_app": self.target_app,
            "entry_url": self.entry_url,
            "description": self.description,
            "goal_template": self.goal_template,
            "input_params": [asdict(p) for p in self.input_params],
            "output_params": [asdict(p) for p in self.output_params],
            "steps": [s.to_dict() for s in self.steps],
            "checkpoint": self.checkpoint.to_dict(),
            "created_at": self.created_at,
            "source_run_id": self.source_run_id,
            "allowlist_tags": self.allowlist_tags,
        }

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @staticmethod
    def from_dict(d: dict) -> "Artifact":
        return Artifact(
            artifact_id=d["artifact_id"],
            name=d["name"],
            version=d["version"],
            target_app=d["target_app"],
            entry_url=d["entry_url"],
            description=d["description"],
            goal_template=d["goal_template"],
            input_params=[ParamSpec(**p) for p in d["input_params"]],
            output_params=[ParamSpec(**p) for p in d["output_params"]],
            steps=[Step.from_dict(s) for s in d["steps"]],
            checkpoint=Checkpoint.from_dict(d["checkpoint"]),
            created_at=d["created_at"],
            source_run_id=d["source_run_id"],
            allowlist_tags=d.get("allowlist_tags", []),
        )

    @staticmethod
    def load(path: str) -> "Artifact":
        with open(path) as f:
            return Artifact.from_dict(json.load(f))


class ResultStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"
    ESCALATED = "escalated"


@dataclass
class ReplayResult:
    status: ResultStatus
    artifact_id: str
    run_id: str
    outputs: dict[str, Any] = field(default_factory=dict)
    business_outcome_code: Optional[str] = None
    failed_step_id: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    message: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d