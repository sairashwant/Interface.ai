"""
Safety & policy guardrails.

Two independent concerns, kept separate on purpose:

1. Allowlist enforcement (`PolicyError` on violation) -- checked before every
   action, both during discovery and replay. This is the hard boundary: the
   agent literally cannot navigate outside allowed domains/routes or perform
   a disallowed action type, regardless of what the LLM decided.

2. Redaction -- applied to everything written to logs or artifacts, so that
   even if a screen momentarily displays something sensitive, it never
   persists in a form a log reader could recover.

Risk classification (safe vs risky) is attached to individual Steps in the
artifact schema, not enforced here directly -- see discover.py, where
POST-ing forms / any state-changing action is tagged `risk=RISKY` and
`requires_confirmation=True` by default. The replay engine (replay.py) is
what actually gates on a risky step if you choose to require confirmation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


class PolicyError(Exception):
    """Raised when an action would violate the configured allowlist."""


@dataclass
class Allowlist:
    allowed_domains: list[str]
    allowed_route_prefixes: list[str] = field(default_factory=lambda: ["/"])
    allowed_action_types: list[str] = field(
        default_factory=lambda: ["navigate", "click", "type", "select", "wait_for", "extract"]
    )

    def check_navigate(self, url: str) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not any(host == d or host.endswith("." + d) for d in self.allowed_domains):
            raise PolicyError(f"Navigation to disallowed domain: {host!r} (url={url})")
        if self.allowed_route_prefixes and not any(
            parsed.path.startswith(p) for p in self.allowed_route_prefixes
        ):
            raise PolicyError(f"Navigation to disallowed route: {parsed.path!r}")

    def check_action_type(self, action_type: str) -> None:
        if action_type not in self.allowed_action_types:
            raise PolicyError(f"Action type not permitted by policy: {action_type!r}")


# --- Redaction -------------------------------------------------------------

_SENSITIVE_FIELD_NAMES = {
    "password", "pwd", "secret", "token", "api_key", "apikey", "ssn",
    "social_security", "credit_card", "card_number", "cvv", "pin",
}

_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),          # SSN-shaped
    re.compile(r"\b\d{13,19}\b"),                    # card-number-shaped
]


def redact_value(field_name: str, value: str) -> str:
    if field_name and field_name.lower() in _SENSITIVE_FIELD_NAMES:
        return "[REDACTED]"
    if isinstance(value, str):
        for pat in _PATTERNS:
            if pat.search(value):
                return "[REDACTED]"
    return value


def redact_dict(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, str):
            out[k] = redact_value(k, v)
        else:
            out[k] = v
    return out


# Default policy for this project's target app.
DEFAULT_ALLOWLIST = Allowlist(
    allowed_domains=["127.0.0.1", "localhost"],
    allowed_route_prefixes=["/login", "/members", "/logout"],
    allowed_action_types=["navigate", "click", "type", "select", "wait_for", "extract"],
)