"""Assert the numbers in the docs against the numbers in the live tree.

`python -m eval.report --check` already does this for the evaluation figures —
compute the number, compare it to what's committed, fail loudly on drift. This is
the same mechanism applied to the four counts that kept going stale in prose because
nothing machine-checked them: test count, WHAT_BROKE entry count, API endpoint
count, and the simulated batch's subscription count (see docs/WHAT_BROKE.md,
"pyproject.toml's testpaths omitted api" — that entry exists because none of this
was checked).

Each doc claim is read out of the file with a regex, not hardcoded here, so this
script cannot itself drift from the prose it's checking. Run directly:

    python -m scripts.check_docs

Exits 1 and prints every mismatch (not just the first) if anything disagrees.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Claim:
    doc: str
    pattern: str
    label: str


def _actual_test_count() -> int:
    """Sum pytest's own per-file collection counts — the only total it prints."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    counts = re.findall(r"^\S.*\.py: (\d+)$", result.stdout, flags=re.MULTILINE)
    if not counts:
        raise RuntimeError("could not parse any per-file counts from pytest --collect-only -q")
    return sum(int(n) for n in counts)


def _actual_what_broke_entries() -> int:
    text = (ROOT / "docs" / "WHAT_BROKE.md").read_text()
    headers = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    return sum(1 for h in headers if h != "Open")


def _actual_endpoint_count() -> int:
    text = (ROOT / "api" / "main.py").read_text()
    return len(re.findall(r'^@app\.get\(', text, flags=re.MULTILINE))


def _actual_subscription_count() -> int:
    text = (ROOT / "sim" / "generate.py").read_text()
    match = re.search(r"^N_SUBSCRIPTIONS = ([\d_]+)$", text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("sim/generate.py: N_SUBSCRIPTIONS not found")
    return int(match.group(1).replace("_", ""))


#: (doc path, regex with one capturing group for the claimed number, human label)
CLAIMS: list[Claim] = [
    Claim("README.md", r"\*\*(\d[\d,]*) tests passing", "test count"),
    Claim("README.md", r"has \*\*(\d+) entries\*\*", "WHAT_BROKE entry count"),
    Claim(
        "docs/ARCHITECTURE.md",
        r"FastAPI over `winback_reader`\. (\d+) endpoints",
        "endpoint count (ARCHITECTURE.md)",
    ),
    Claim(
        "docs/FRONTEND_SPEC.md",
        r"(\d+) `?GET`? endpoints",
        "endpoint count (FRONTEND_SPEC.md)",
    ),
    Claim(
        "docs/ARCHITECTURE.md",
        r"Full batch, ([\d,]+) subscriptions",
        "subscription count (ARCHITECTURE.md)",
    ),
]

ACTUALS = {
    "test count": _actual_test_count,
    "WHAT_BROKE entry count": _actual_what_broke_entries,
    "endpoint count (ARCHITECTURE.md)": _actual_endpoint_count,
    "endpoint count (FRONTEND_SPEC.md)": _actual_endpoint_count,
    "subscription count (ARCHITECTURE.md)": _actual_subscription_count,
}


def main() -> int:
    mismatches: list[str] = []
    missing: list[str] = []
    # Compute each actual value once even though several claims share one.
    cache: dict[str, int] = {}

    for claim in CLAIMS:
        path = ROOT / claim.doc
        text = path.read_text()
        match = re.search(claim.pattern, text)
        if match is None:
            missing.append(f"{claim.doc}: pattern for {claim.label!r} did not match anything")
            continue
        claimed = int(match.group(1).replace(",", ""))

        if claim.label not in cache:
            cache[claim.label] = ACTUALS[claim.label]()
        actual = cache[claim.label]

        if claimed != actual:
            mismatches.append(
                f"{claim.doc}: claims {claim.label} = {claimed}, tree says {actual}"
            )

    if missing:
        print("check_docs: could not find the claim to check (doc drifted more than expected):")
        for line in missing:
            print(f"  - {line}")
    if mismatches:
        print("check_docs: doc/tree mismatches:")
        for line in mismatches:
            print(f"  - {line}")

    if missing or mismatches:
        return 1

    print(f"check_docs: all {len(CLAIMS)} claims match the live tree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
