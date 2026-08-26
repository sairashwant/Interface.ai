"""
Small artifact-curation utility, not part of the core loop.

Discovery only records ExpectedOutcomes it actually observes in a given run.
When a later discovery run (e.g. probing a known error path on purpose)
observes a *new* outcome on a step whose locator already exists in a
previously-saved artifact, this merges that outcome in -- so an artifact's
error-taxonomy coverage can grow across runs / reviewer edits, without
re-recording the whole capability from scratch.

Usage:
  python -m agent.merge_outcomes --into artifacts/balance_lookup.json \
      --from artifacts/balance_lookup_notfound_probe.json
"""
import argparse

from .schema import Artifact


def merge(into: Artifact, source: Artifact) -> int:
    merged = 0
    for src_step in source.steps:
        if not src_step.expected_outcomes:
            continue
        for dst_step in into.steps:
            if dst_step.locator and src_step.locator and \
               dst_step.locator.strategy == src_step.locator.strategy and \
               dst_step.locator.value == src_step.locator.value:
                existing_codes = {eo.code for eo in dst_step.expected_outcomes}
                for eo in src_step.expected_outcomes:
                    if eo.code not in existing_codes:
                        dst_step.expected_outcomes.append(eo)
                        merged += 1
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--into", required=True)
    ap.add_argument("--from", dest="source", required=True)
    args = ap.parse_args()

    into = Artifact.load(args.into)
    source = Artifact.load(args.source)
    n = merge(into, source)
    into.save(args.into)
    print(f"Merged {n} new expected outcome(s) into {args.into}")


if __name__ == "__main__":
    main()