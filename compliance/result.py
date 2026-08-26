"""The verdict type every compliance rule returns.

Deliberately tiny and deliberately strict. Two invariants are enforced in the
constructor rather than left to discipline:

* a blocking verdict must name a ``stop_reason`` -- an audit row that says only
  "blocked" is not an audit row;
* an approval must **not** carry one, so a stop reason in the trail always means
  something actually stopped.

When several rules speak at once the most restrictive answer wins, which is the one
piece of real logic here and the one place a subtle mistake would be invisible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    """The only four answers the guardrail can give."""

    APPROVE = "APPROVE"
    REDIRECT_TO_WINDOW = "REDIRECT_TO_WINDOW"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    DENY = "DENY"

    @staticmethod
    def severity(verdict: Verdict) -> int:
        """How restrictive a verdict is. Higher wins a disagreement."""
        return _SEVERITY[verdict]


_SEVERITY: dict[Verdict, int] = {
    Verdict.APPROVE: 0,
    Verdict.REDIRECT_TO_WINDOW: 1,
    Verdict.ESCALATE_HUMAN: 2,
    Verdict.DENY: 3,
}


@dataclass(frozen=True, slots=True)
class RuleResult:
    """One rule's answer about one proposed action.

    ``detail`` is rendered verbatim into ``decisions.authorizing_rule`` and onto the
    dashboard chip, so it is written for a human reading an audit trail rather than
    for a log parser: ``"npci_1_plus_3: attempt 2/4 permitted"``.
    """

    rule: str
    verdict: Verdict
    detail: str
    stop_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict is Verdict.APPROVE and self.stop_reason is not None:
            raise ValueError(
                f"{self.rule}: an APPROVE must not carry a stop_reason "
                f"(got {self.stop_reason!r}); a stop reason in the trail has to mean "
                "something actually stopped."
            )
        if self.verdict is not Verdict.APPROVE and not self.stop_reason:
            raise ValueError(
                f"{self.rule}: a {self.verdict} must name a stop_reason. A redirect and "
                "an escalation stop the action as proposed just as surely as a denial "
                "does, and an audit row that says only 'blocked' cannot be reviewed."
            )

    @property
    def allowed(self) -> bool:
        """Only an outright APPROVE permits execution now."""
        return self.verdict is Verdict.APPROVE

    def to_dict(self) -> dict[str, Any]:
        """The audit-row shape. A contract, not an implementation detail."""
        return {
            "rule": self.rule,
            "verdict": str(self.verdict),
            "detail": self.detail,
            "stop_reason": self.stop_reason,
            "metadata": dict(self.metadata),
        }


def most_restrictive(results: Sequence[RuleResult]) -> RuleResult:
    """The governing verdict when several rules have spoken.

    Ties keep the first rule that raised the objection, so the reason surfaced to a
    reviewer is the one that appeared earliest in a fixed evaluation order and does
    not drift as rules are added.
    """
    if not results:
        raise ValueError(
            "most_restrictive needs at least one result: no rules having run is not "
            "the same as every rule having passed, and must never approve."
        )
    return max(results, key=lambda r: _SEVERITY[r.verdict])
