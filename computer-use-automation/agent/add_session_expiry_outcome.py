"""
One-off artifact curation: add the session_expired RECOVERABLE outcome to
the 'click Search' step, since discovery's own runs never naturally
experience a mid-run session timeout. In a real deployment this is exactly
the kind of known error branch a reviewer adds by hand (or via a targeted
probe) rather than waiting for discovery to stumble into it.
"""
from agent.schema import Artifact, ExpectedOutcome, OutcomeClass, Locator

artifact = Artifact.load("artifacts/balance_lookup.json")

for step in artifact.steps:
    if step.locator and step.locator.strategy == "role" and step.locator.value == "button::Search":
        existing = {eo.code for eo in step.expected_outcomes}
        if "session_expired" not in existing:
            step.expected_outcomes.append(ExpectedOutcome(
                detect=Locator(strategy="text", value="session has expired"),
                outcome_class=OutcomeClass.RECOVERABLE,
                code="session_expired",
                message_template="Your session has expired. Please sign in again.",
                retry=True,
            ))
            print(f"Added session_expired outcome to step {step.step_id}")
        else:
            print(f"session_expired already present on step {step.step_id}")

artifact.save("artifacts/balance_lookup.json")