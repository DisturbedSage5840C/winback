"""``python -m eval`` — run the whole evaluation and write it to the database.

Five runs, not one. The headline is one of them; the other four exist because a single
table of four arms invites exactly two objections, and both are answerable with a slice:

* *"Your model only wins where it had training data."* — the observed and censored runs
  are the same harness over the two regions the legacy policy's filter created.
* *"Your result depends on how well you assumed the nudge works."* — the sensitivity runs
  move the **world's** nudge effect while the policy keeps believing what it believed.
  The policy is not re-tuned between them; that is the point. What the table measures is
  how much the policy loses by being wrong, which is the honest version of a parameter
  nobody can measure without sending real messages.

Every run lands in ``eval_runs`` with its own parameters, so the report reads five rows
rather than trusting a comment about which numbers came from where.

Then ``python -m eval.report`` renders ``docs/EVALUATION.md`` from those tables.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from eval.bootstrap import DEFAULT_RESAMPLES, DEFAULT_SEED, bootstrap_run
from eval.counterfactual import build_cases, run_evaluation, split_by_region
from eval.persist import save
from ml.features import BankMethodRates
from ml.policy import DEFAULT_POLICY
from ml.scorer import load_scorer
from ml.train import ARTIFACTS
from sim.generate import build_dataset
from sim.world import DEFAULT_PARAMS

#: The headline run. Everything in ``docs/EVALUATION.md`` §04 comes from this id.
HEADLINE = "v1"

#: World nudge multipliers for the sensitivity table. Lower means the nudge works
#: harder. ``DEFAULT_PARAMS.nudge_balance_multiplier`` (0.62) is the assumed value and is
#: covered by the headline run itself rather than being re-run under another name.
SENSITIVITY = ((1.00, "v1_nudge_100"), (0.40, "v1_nudge_040"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval", description=__doc__)
    parser.add_argument("--cohort", default="test", choices=["train", "calibrate", "test"])
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--headline-only",
        action="store_true",
        help="skip the region and sensitivity runs (faster; leaves the report incomplete)",
    )
    args = parser.parse_args(argv)

    print("building the frozen dataset...", flush=True)
    dataset = build_dataset()
    scorer = load_scorer(ARTIFACTS)
    rates = BankMethodRates.fit(dataset, cohort="train")
    cases = build_cases(dataset, cohort=args.cohort)
    observed, censored = split_by_region(dataset, cases)

    print(
        f"dataset {dataset.dataset_version} ({dataset.fingerprint()}) · "
        f"{len(cases)} replay cases · {len(observed)} observed / {len(censored)} censored",
        flush=True,
    )

    plans: list[tuple[str, dict, str]] = [
        (
            HEADLINE,
            {"cases": cases},
            "Headline. All failed invoices in the cohort, default world and policy.",
        )
    ]
    if not args.headline_only:
        plans += [
            (
                "v1_observed",
                {"cases": observed},
                "Observed region: invoices the legacy policy did retry, so the model "
                "trained on labels from here.",
            ),
            (
                "v1_censored",
                {"cases": censored},
                "Censored region: under Rs 500 or netbanking, where the legacy policy "
                "never retried and the model has no training labels.",
            ),
        ]
        plans += [
            (
                run_id,
                {
                    "cases": cases,
                    "world": replace(DEFAULT_PARAMS, nudge_balance_multiplier=multiplier),
                },
                f"Nudge sensitivity: world multiplier {multiplier:.2f}, policy belief "
                f"held at {DEFAULT_POLICY.assumed_nudge_failure_multiplier:.2f}.",
            )
            for multiplier, run_id in SENSITIVITY
        ]

    for run_id, kwargs, notes in plans:
        print(f"\n[{run_id}] replaying four arms...", flush=True)
        run = run_evaluation(
            dataset,
            scorer=scorer,
            rates=rates,
            run_id=run_id,
            model_version="v1",
            cohort=args.cohort,
            **kwargs,
        )
        print(f"[{run_id}] bootstrapping {args.resamples:,} resamples...", flush=True)
        intervals = bootstrap_run(run, resamples=args.resamples, seed=args.seed)
        save(run, intervals, seed=args.seed, resamples=args.resamples, notes=notes)

        for arm in run.arms:
            ratio = arm.paise_per_legal_attempt
            print(
                f"    {arm.arm}  Rs {arm.compliant_recovered_paise / 100:>12,.0f} legal "
                f"· {arm.legal_attempts_consumed:>4} legal attempts "
                f"· {arm.compliance_violations:>4} violations "
                f"· {'—' if ratio is None else f'Rs {ratio / 100:,.0f}/attempt'}"
            )

    print("\nwritten to the database. Now: python -m eval.report", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
