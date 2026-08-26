"""The verdict type shared by every compliance rule.

Small, but it carries one piece of real logic: when several rules speak at once, the
**most restrictive** answer wins. Getting that backwards would let an APPROVE from the
retry cap override a DENY from the consent gate, which is the exact shape of a
compliance bug that never announces itself.
"""

from __future__ import annotations

import pytest

from compliance.result import RuleResult, Verdict, most_restrictive


def approve(rule: str = "r") -> RuleResult:
    return RuleResult(rule=rule, verdict=Verdict.APPROVE, detail="ok")


def deny(rule: str = "r", reason: str = "blocked") -> RuleResult:
    return RuleResult(rule=rule, verdict=Verdict.DENY, detail="no", stop_reason=reason)


def test_only_approve_counts_as_allowed() -> None:
    """A redirect and an escalation are not approvals. They are both perfectly good
    outcomes -- money may still be recovered later -- but neither permits execution
    *now*, and conflating the three is how an action slips through mistimed."""
    assert approve().allowed is True
    for verdict in (Verdict.REDIRECT_TO_WINDOW, Verdict.ESCALATE_HUMAN, Verdict.DENY):
        result = RuleResult(rule="r", verdict=verdict, detail="d", stop_reason="because")
        assert result.allowed is False


def test_severity_is_strictly_ordered() -> None:
    order = [
        Verdict.APPROVE,
        Verdict.REDIRECT_TO_WINDOW,
        Verdict.ESCALATE_HUMAN,
        Verdict.DENY,
    ]
    severities = [Verdict.severity(v) for v in order]
    assert severities == sorted(severities)
    assert len(set(severities)) == len(severities)


def test_most_restrictive_picks_the_worst_verdict() -> None:
    results = [
        approve("npci_retry_cap"),
        RuleResult(
            rule="non_peak_window",
            verdict=Verdict.REDIRECT_TO_WINDOW,
            detail="d",
            stop_reason="peak_window",
        ),
        deny("consent_gate", "consent_withdrawn"),
        RuleResult(
            rule="afa_threshold",
            verdict=Verdict.ESCALATE_HUMAN,
            detail="d",
            stop_reason="above_afa_ceiling",
        ),
    ]
    worst = most_restrictive(results)
    assert worst.verdict is Verdict.DENY
    assert worst.rule == "consent_gate"
    assert worst.stop_reason == "consent_withdrawn"


def test_most_restrictive_is_order_independent() -> None:
    """A rule evaluation order that changes the verdict is a bug waiting for a
    refactor to expose it."""
    a, b, c = approve("a"), deny("b"), approve("c")
    assert most_restrictive([a, b, c]).rule == "b"
    assert most_restrictive([c, b, a]).rule == "b"
    assert most_restrictive([b, a, c]).rule == "b"


def test_ties_keep_the_first_rule_that_raised_the_objection() -> None:
    first, second = deny("first", "reason_one"), deny("second", "reason_two")
    assert most_restrictive([first, second]).rule == "first"


def test_all_approving_yields_approve() -> None:
    assert most_restrictive([approve("a"), approve("b")]).verdict is Verdict.APPROVE


def test_an_empty_evaluation_is_refused() -> None:
    """No rules ran is not the same as every rule passed, and must never be treated
    as an approval."""
    with pytest.raises(ValueError, match="at least one"):
        most_restrictive([])


def test_a_blocking_verdict_requires_a_stop_reason() -> None:
    """An audit row saying only 'blocked' is not an audit row."""
    with pytest.raises(ValueError, match="stop_reason"):
        RuleResult(rule="r", verdict=Verdict.DENY, detail="no")


def test_approve_must_not_carry_a_stop_reason() -> None:
    with pytest.raises(ValueError, match="stop_reason"):
        RuleResult(rule="r", verdict=Verdict.APPROVE, detail="ok", stop_reason="why")


def test_to_dict_is_the_audit_row() -> None:
    """This dict is written verbatim into decisions/audit_log, so its shape is a
    contract, not an implementation detail."""
    row = deny("consent_gate", "dnd_cooloff").to_dict()
    assert row == {
        "rule": "consent_gate",
        "verdict": "DENY",
        "detail": "no",
        "stop_reason": "dnd_cooloff",
        "metadata": {},
    }
    assert isinstance(row["verdict"], str), "must serialise to JSON without a codec"


def test_results_are_immutable() -> None:
    """A verdict that can be edited after the fact is exactly what the append-only
    audit trail exists to prevent, one layer up."""
    result = approve()
    with pytest.raises((AttributeError, TypeError)):
        result.verdict = Verdict.DENY  # type: ignore[misc]
