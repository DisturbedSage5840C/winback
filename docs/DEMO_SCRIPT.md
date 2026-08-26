# Demo script — 5:00

> **Status: outline.** Shot list and timings firm up on Day 8 against the real
> dashboard; recorded Day 9. Written now so the build has a target to point at.

| Time | Beat | On screen |
|---|---|---|
| 0:00–0:30 | **The problem, quantified.** "UPI Autopay fails on 8–15% of debits versus 2–3% for card mandates. Since August 2025 NPCI caps you at one attempt plus three retries, non-peak only. You cannot brute-force this back." | The failure-rate and retry-cap numbers, nothing else |
| 0:30–1:00 | **The loop in one sentence.** Compliance generates the legal option set, the model ranks it, the gate executes it, the ledger records it. | Architecture diagram — 10 seconds, no more |
| 1:00–3:00 | **Live batch run.** Ingest → deterministic TD/BD classification → calibrated probability → candidate set scored → guardrail verdict → execution → funnel updates with measured ₹ recovered. Real `plink_…` on the live cohort. | Agent trace streaming, rule chips flashing, funnel filling |
| 3:00–3:45 | **The handled failure, on purpose.** The fifth attempt refused by the cap — red chip, audit row on screen, stop reason named. Then the Day-8 drill: kill the local MCP server mid-run and let it degrade to remote plus simulated without losing the batch. | The red chip and the audit row, held long enough to read |
| 3:45–4:30 | **Rigour.** Reliability diagram, ECE, the rupee-priced confusion matrix, the four-arm table with ₹/legal-attempt and violations-by-arm, and the observed-vs-censored calibration gap. Name the simulator limitation here, unprompted. | Evaluation page |
| 4:30–5:00 | **Close.** This is Razorpay's own listed direction — "mandate retry sequencer" — built independently on the public MCP server and the Claude Agent SDK, in ten days. | Repo, one-command demo |

## Non-negotiables

- The red block chip on screen long enough to read. It is the compliance proof, and it
  is the best twenty seconds in the video.
- Say out loud: *"using an LLM to decide a legal retry cap would be a bug."*
- State the simulator limitation before anyone asks.
- No mocked data anywhere on screen.

## Practicalities

`ffmpeg` is not installed on this machine. Record with QuickTime / macOS screen
capture, or install ffmpeg on Day 8 if programmatic editing turns out to be needed.
Record the rough cut on Day 8 as insurance, not Day 9.
