# Evaluation

> **Status: not yet generated.** This file is written by `python -m eval.report`
> directly from `eval_runs` / `eval_arm_results` on Day 5 — never hand-typed. Numbers
> that a human can retype are numbers a human can round. The design below is fixed now
> so that the results, whatever they are, cannot be chosen after seeing them.

## 01 — The metric

**Rupees recovered per legal attempt consumed**, reported beside a
compliance-violations count. Not rupees recovered.

A policy that recovers more money by taking a fifth attempt has not beaten anything —
it has broken NPCI OC-215-A, and no merchant can ship it. Making legality part of the
denominator rather than a footnote is what makes the comparison honest, and it is why
the naive baseline loses on grounds that were fixed before the experiment ran.

## 02 — The four arms

| Arm | Policy | Role |
|---|---|---|
| A | Never retry, always escalate | Over-conservative floor |
| B | Retry everything to the cap, any hour | Naive baseline — **and illegal** |
| C | Legacy policy (fixed T+1/2/3 at 09:00, amount- and method-filtered) | What the merchant does today |
| D | **Winback** — calibrated model + cost policy + guardrail | The submission |

All four are scored on the **same** held-out invoices with the **same** oracle seeds,
so a difference between arms is a difference in policy and not in luck. Confidence
intervals come from a paired bootstrap over subscriptions.

## 03 — What gets reported, per arm

Rupees recovered · attempts consumed · **legal** attempts consumed ·
**rupees per legal attempt** · nudges sent · escalations · **compliance violations** ·
invoices written off · paired bootstrap CI.

Plus, for the model itself: ECE (10-bin) · Brier · PR-AUC · minority-class
precision/recall · reliability diagram · the confusion matrix **priced in rupees**
(a false positive costs one burned legal attempt plus messaging; a false negative
costs the invoice times margin). Never plain accuracy — on an 8–15% failure base rate
accuracy is a number that rewards predicting nothing.

## 04 — Calibration on the censored slice

Reported separately for the slice the legacy policy observed and the slice it never
retried (low-value, netbanking). The gap between them is the honest measure of how far
the model can be trusted outside its training distribution, and it is reported whether
or not it flatters the result.

## 05 — The limitation, stated first

Arm D beats arms B and C **inside a world I wrote**. Three things were done to make
that less circular than it sounds — the simulator uses a deliberately different
functional form from the model, the training data is censored by a biased legacy
policy so the model must generalise past what it observed, and calibration is measured
on both slices — but a simulator is a model, not the world. Naming this before a
panelist does is not modesty; it is the only reading of the evidence that survives
contact with someone who has run a real dunning system.
