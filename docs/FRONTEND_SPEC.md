# Frontend spec — Winback dashboard

> **This file is the frontend contract.** Everything below is exact: which pages exist,
> which endpoint each pixel reads from, which fields those endpoints actually return
> (checked against the live database as of this writing, not guessed), the design
> language, and the motion budget from `docs/IMPLEMENTATION_PLAN.md` §8. §09 at the
> bottom is a master prompt — paste it into a fresh Claude Code session pointed at this
> repo and it builds the whole thing.
>
> **No mocks anywhere.** Every number on every page comes from one of the ten endpoints
> in `api/main.py`, running against Postgres. If a page cannot get a real number for
> something, it says so or omits it — it never invents one.

---

## 00 — What this has to do, and why it matters more than it looks

This dashboard is not a demo prop. It is the panel's evidence that the four claims in
`docs/EVALUATION.md` are real: money was recovered, the recovery was compliant, the
compliance is provable per-invoice, and the system fails safely. A panelist who doesn't
believe the ₹ figure won't believe anything else. So the build order below is not
alphabetical — it is in the order a skeptical panelist would ask to see things, and nothing
ships until the thing before it is real.

**The one scene that has to be perfect:** a batch running live, decision rows streaming
in, and the 5th attempt on some invoice getting blocked with a red rule chip and its exact
`authorizing_rule` string on screen. That is twenty seconds of video that proves the whole
thesis without a word of narration. Build toward that scene first if time runs short.

---

## 01 — Stack

| Layer | Choice | Why |
|---|---|---|
| Framework | **Vite 8 + React 19 + TypeScript strict**, routed by `react-router-dom` (`HashRouter`) | see the note below — this spec originally called for Next.js 15 App Router and the delivered app is a Vite SPA |
| Styling | Tailwind CSS v4 (CSS-first config, no `tailwind.config.js`) | fastest path to the exact palette below with zero abstraction tax |
| Charts | Recharts | funnel bars, calibration reliability diagram, four-arm comparison — all standard chart shapes it does natively |
| Motion | Framer Motion (`motion` package) | the four animations in §06; respects `prefers-reduced-motion` everywhere |
| Data fetching | native `fetch` against the FastAPI base URL, `VITE_API_BASE` (defaults to `http://localhost:8000`) | no client library needed — ten endpoints, all `GET`, no auth |
| Live updates | polling `GET /runs/{id}/events?since=<cursor>` every 1.5s while a run page is open | see §07; no websocket exists on the backend and none should be built for this |
| Fonts | Satoshi (Fontshare) for display + UI, `Manrope, system-ui` fallback stack | matches the buildathon micro-site's own type, per the plan |
| State | local `useState` + a small `useAsync` hook; no Redux/Zustand — the app has no client state worth a store | keep it simple, nothing here needs global state |

**Stack note — why this is Vite and not Next.js.** The rows above were rewritten on Day 7
to describe what actually shipped. The dashboard was scaffolded from a Figma Make export,
which is a Vite + React SPA, and porting it to the App Router would have cost most of a
day to buy nothing this app uses: there is no SEO surface, no server-side secret, no
mutation, and no route that benefits from RSC — every page is an authenticated-by-nobody
read of ten `GET` endpoints on a FastAPI backend that must be running anyway. `HashRouter`
is deliberate for the same reason: the built `dist/` is a pile of static files that opens
correctly from `file://` or any static host with no rewrite rules to configure, which
matters more for a judge cloning the repo than clean URLs do. Everything else in this
spec — the palette, the type, the four animations, the motion budget, the endpoint
contracts, the page structure — was implemented as written and is unaffected.

Run the `dataviz` skill before writing the first chart and the `frontend-design` skill
before the first component, per the plan. Both are already-loaded skills in this
workspace — invoke them, don't reinvent their checklists.

---

## 02 — Design language (Razorpay's motion, not Razorpay's design)

Inspiration only — never replicate Razorpay's actual buildathon page. Re-composed
semantics below.

### Palette

```css
--ink:        #192839;   /* primary text on light */
--ink-deep:   #080d29;   /* headings, near-black */
--brand:      #305eff;   /* agent action — the "the system did this" color */
--brand-dim:  #2950da;
--brand-soft: #4d7fff;
--brand-tint: #75a3ff;
--brand-wash: #E9F0FF;   /* light fills, hover states */

--slate-900:  #40566d;
--slate-600:  #6c849d;
--slate-300:  #cbd5e2;
--slate-100:  #f8fafc;

--good:       #48d08c;   /* recovered */
--critical:   #F0263C;   /* blocked by guardrail — reserved, never reused */
--warn:       #f5a623;   /* escalated */
--neutral:    #6c849d;   /* written off */

--surface:    #ffffff;
--surface-dim:#f8fafc;
--surface-ink:#0e0b08;   /* dark-mode surface, matches the buildathon micro-site */
```

Run `node <dataviz-skill-path>/scripts/validate_palette.js` on the categorical set
(`good`, `critical`, `warn`, `neutral`, `brand`) before shipping any chart that uses them
together — this is a status palette (state), not a free categorical one, so it is
reserved: these four colors mean recovered / blocked / escalated / written-off and are
never reused for anything else, and they always ship with an icon + label, never color
alone. Validate both light and dark surfaces.

### Type

- Satoshi 700 for headlines and the ₹ headline number.
- Satoshi 500 for section labels (`01 —`, `02 —` numbered-section style, mirroring the
  buildathon micro-site's editorial rhythm).
- Satoshi 400 / `Manrope` fallback for body and table text.
- Numbers (₹ figures, counts, percentages) get tabular-nums always — a shifting-width
  count-up looks broken.

### Structural motif

Numbered, labelled sections — `01 — Overview`, `02 — Exception worklist`, etc. — carried
from the buildathon site's own editorial structure into every page header and into
`docs/ARCHITECTURE.md`'s own section numbering (already established there; match it).
Low chrome: no card-soup, no drop shadows beyond a 1px `slate-300` border and a single
soft elevation on the live-trace panel.

---

## 03 — Pages

Five pages. Nothing else. The plan's own scope line: *"Overview + worklist + drill-down
are mandatory; compliance panel is mandatory; the evaluation page may degrade to
embedded static PNGs from `ml/artifacts/`."*

### 1. `/` — Overview

**Reads:** `GET /runs` (list, newest first) → pick the latest by default, selector to
switch → `GET /runs/{id}/overview`.

**Layout, top to bottom:**

1. **Run selector** — a row of pills, one per `run_id` from `GET /runs`, each showing
   `arm`, `invoices`, and a small dot colored by `touched_live` (blue = live cohort
   touched, slate = simulated only). Clicking switches the whole page.
2. **Headline** — `recovered_paise` from the overview response, formatted `₹15,05,820`
   (Indian digit grouping — not `₹1,505,820`), with the count-up animation (§06.1). Below
   it in `slate-600`: `{recovered} invoices from a batch of {at_risk}, arm {arm}`.
3. **Funnel** — a horizontal stacked/staged bar chart of `at_risk → actions_taken →
   retry_attempted / nudges_sent → recovered → escalated → blocked` from the same
   overview payload, left-to-right stagger fill (§06.2). Each stage is a real count from
   the response — do not invent a stage the API doesn't return.
4. **Stop-reason breakdown** — a small horizontal bar list from `overview.stop_reasons`
   (`stop_reason`, `invoices`), sorted descending as the API already returns it. This is
   the panel's answer to "why didn't you just retry everything" — label it as such in a
   one-line caption.
5. **Compliance strip** — three stat tiles: `violations` (from the funnel view, should
   read 0 for a clean run — style it in `good` green when zero, `critical` red
   otherwise), `escalated`, `stopped`. Pull these from the same `/runs/{id}/overview` row
   — they are already on it, do not add a second query.
6. **Config footer** — small print reading execution mode and model version from
   `GET /config`: `simulated · model v1` or similar. This is where "is this real" gets
   answered without anyone having to ask.

### 2. `/worklist` — Exception worklist

**Reads:** `GET /worklist?limit=&offset=` for the live queue (default view) — **or**, when
a `run_id` is selected via the same run-selector component as the overview page,
`GET /runs/{id}/worklist?limit=&offset=&outcome=`.

This page has two modes and both are real, not a toggle over the same data:

- **Live queue** (`GET /worklist`): invoices still `at_risk`, nothing decided yet. This is
  the one that has to visibly shrink as a batch runs — poll it every 5s while any run is
  in `events`-streaming state (§07), or on manual refresh otherwise.
- **Run worklist** (`GET /runs/{id}/worklist`): what one batch decided about every invoice
  it touched, filterable by `outcome`.

**Table columns**, in order, sourced from `exception_worklist` fields both endpoints
return: `invoice_id`, `amount_paise` (right-aligned, ₹-formatted, the sort key — table is
pre-sorted by the API, don't re-sort client-side), `method`, `bank`, `mcc_category`,
`attempts_used / attempts_remaining` as `"2 / 4"` with a 4-segment mini-bar (filled
segments = used), `latest_root_cause` as a small chip (`TD` / `BD_transient` / `BD_hard`,
three distinct neutral colors — not the status palette, this is identity not state), and
for the run-worklist mode only: `action_taken`, `outcome` (colored with the status
palette), `compliance_violation` (a red dot if true, nothing if false — never a green
check for "no violation", silence is the correct default).

Row click → navigates to `/invoices/{invoice_id}` (optionally `?run_id=` when reached from
run-worklist mode, to scope the drill-down).

Pagination: `limit`/`offset` query params, 50 rows/page, simple prev/next — the API caps
at 500 per call, no need for anything fancier.

### 3. `/invoices/[id]` — Decision drill-down

**Reads:** `GET /invoices/{id}?run_id=` (optional) for facts/attempts/decisions/audit
trail, **and** `GET /invoices/{id}/compliance` for the live rule panel.

This is the single most important page for the panel's trust — it is where "show your
work" gets answered. Structure:

1. **Header** — invoice id, amount, method/bank/mcc, subscription status, customer_hash
   (never the raw customer id — the API already redacts it, don't re-derive one).
2. **Compliance panel** (see §04 below — it is its own spec section because it is reused
   as a drawer/embed elsewhere) — embedded here as the primary content, not a drawer,
   since this page *is* the drill-down.
3. **Attempt timeline** — every row from `attempts[]`, in order, each showing
   `attempt_number`, `attempted_at_ist`, `outcome`, `action`, `error_code`/`error_reason`
   when present, `root_cause_class`, and a small `observed` vs `counterfactual` tag
   (`observed: false` rows are oracle replay, not real history — label them distinctly,
   e.g. dimmed with an "counterfactual" chip, never presented as equal to real attempts).
4. **Decision cards** — one per row in `decisions[]`. Each card is the drill-down drawer
   content from §06.4: `candidate_set` rendered as a small table (action × slot →
   `calibrated_prob`, `expected_value_paise`, verdict — winner highlighted, losers shown
   with their refusal reason), `authorizing_rule` as the full string, `final_action`,
   `expected_value_paise`.
5. **Audit trail** — a flat chronological list from `audit_trail[]`: timestamp, action,
   outcome, `execution_mode`, `razorpay_entity_id` when present (real `plink_…`/`pay_…` —
   render as a monospace chip, this is the proof of a real Razorpay artifact), `stop_reason`
   when present.

404 handling: the API 404s on an unknown invoice — render a plain "no such invoice" state,
not a broken page.

### 4. `/compliance` — Compliance guardrail panel (standalone)

**Reads:** `GET /compliance/window` on load and every 30s (the countdown needs to tick
against something real, but re-fetching every 30s is enough — do not fake a client-side
ticking clock that could drift from the guardrail's own arithmetic; only
`seconds_to_transition` ticks locally between fetches, reset on each fetch), plus an
invoice search box that hits `GET /invoices/{id}/compliance` on submit.

This is the page-level version of the panel embedded in the drill-down (§04 shared
component). Layout:

1. **Live window strip** — big `is_non_peak` indicator (green "non-peak, presentments
   legal" / red "peak window, presentments blocked"), the two peak windows from
   `peak_windows_ist` rendered as a simple 24h timeline with the peak bands shaded red,
   `seconds_to_transition` as a countdown, `next_legal_slots_ist` as three chips.
2. **Invoice lookup** — search-by-`invoice_id`, then render the full shared compliance
   panel component (below) for that invoice.
3. **Static explainer strip** — the four other rules (NPCI cap, AFA ceiling, consent gate,
   pre-debit notice) each get a one-line static description of the rule itself (not
   invoice-specific data — this is the "what the rules are" reference, sourced from
   `docs/COMPLIANCE.md`, hand-written copy, clearly separated from the live per-invoice
   data above it).

### 5. `/evaluation` — Evaluation page

**Reads:** `GET /evaluation` (optionally `?run_id=`, defaults to latest).

Response shape: `{run, arms[], violations[], intervals[]}`.

1. **Headline** — the paired money-difference and the paired violations-difference,
   pulled from `intervals[]` where `comparison` names the D-vs-B pair (filter client-side
   on the `comparison`/`statistic` fields the row actually has — do not hardcode which row
   index that is, since `eval.report` decides ordering). Render as
   `"+₹28 (95% CI: −₹2,697 to ₹2,781) — reported as a tie"` and
   `"−66 violations (95% CI: −96 to −42)"`, with the CI as a small horizontal
   interval-bar mark (dataviz skill: this is exactly the diverging/interval mark case).
2. **Four-arm table** — one row per `arms[]` entry: `arm_label`, `recovered_paise`,
   `attempts_consumed`, `legal_attempts_consumed`, `paise_per_legal_attempt` (the
   thesis's headline metric — visually emphasized column), `nudges_sent`, `escalations`,
   `compliance_violations` (status-colored: 0 in green, >0 in red), `written_off`.
3. **Violations-by-arm chart** — bar chart from `violations[]` (`arm`, `stop_reason`,
   `violations`), grouped/stacked by `stop_reason` within each arm — this is the chart
   that shows arm B's violations are concentrated in a specific rule, not spread thin.
4. **Calibration section** — this is the one place the plan explicitly permits a static
   asset fallback: embed `ml/artifacts/` PNGs (`docs/assets/calibration.png`,
   `docs/assets/realism.png`, `docs/assets/four_arms.png`) directly as `<img>` if building
   live Recharts equivalents from `eval_intervals`/model artifacts is not worth the time
   left. State plainly in a caption that these are the committed report's own charts, not
   reconstructed — that is a feature, not an admission: it proves the dashboard and
   `docs/EVALUATION.md` show the identical evidence.
5. **Honesty note** — a static, hand-written callout naming the simulator-circularity
   limitation and the observed-vs-censored ECE finding (0.034 vs 0.442) verbatim from
   `docs/EVALUATION.md` §07/§08. This is not optional polish — the plan is explicit that
   naming your own limitation before a panelist does is worth more than hiding it.

---

## 04 — Shared component: the compliance panel

Used standalone on `/compliance` and embedded in `/invoices/[id]`. One React component,
one shape, fed by one `GET /invoices/{id}/compliance` response:

```
{
  invoice_id, evaluated_at, amount_paise,
  npci:          { attempts_used, attempts_remaining, cap, verdict, detail, stop_reason },
  afa:           { ceiling_paise, mcc_category, verdict, detail, stop_reason },
  consent:       { status, verdict, detail, stop_reason },
  pre_debit_notice: { notice_sent_at, charge_at, verdict, detail, stop_reason },
  window:        { now_utc, now_ist, is_non_peak, seconds_to_transition,
                   peak_windows_ist, next_legal_slots_ist },
  root_cause,
  retry: { verdict, authorizing_rule, stop_reason, results[], suggested_slots } | { verdict: null, detail },
  nudge: { verdict, authorizing_rule, stop_reason, results[], suggested_slots }
}
```

Render as: an attempts-used 4-segment bar (npci), a ₹ ceiling gauge (afa, amount vs
ceiling), a consent-status chip (consent), a notice-timing chip (pre_debit_notice), the
window strip (window, shared with `/compliance`'s own strip), and **two composed verdict
cards side by side** — "Retry" and "Nudge" — each showing its `verdict` as a large colored
chip (`APPROVE` brand-blue, `DENY`/blocked red, `ESCALATE_HUMAN` amber,
`REDIRECT_TO_WINDOW` amber), the full `authorizing_rule` string underneath in monospace,
and an expandable list of `results[]` (each a `{rule, verdict, detail}` — render every
rule's own verdict, not just the composed one, so a reviewer can see *which* rule decided
it). When `retry.verdict === null` (no root cause on record), render that card as a plain
disabled state with its `detail` string, never a fake verdict.

This component is the single most panel-facing piece of UI in the whole app — the plan's
own words: *"a compliance panel that recomputed the 1+3 cap in TypeScript would be a
second implementation of the law."* It renders exactly what the API returns and computes
nothing itself, not even the "is this good" framing — that's what the verdict colors are
for.

---

## 05 — Formatting rules (apply everywhere, no exceptions)

- **Rupees:** every `_paise` field ÷ 100, formatted with Indian digit grouping
  (`Intl.NumberFormat("en-IN", {style:"currency", currency:"INR", maximumFractionDigits:0})`).
  Never render a raw paise integer. Never do currency math in a template string — compute
  once, format once.
- **Timestamps:** the API returns both `_utc` and `_ist` variants on most rows — always
  display the `_ist` one to a human, keep `_utc` only for cursor/sort logic.
  `attempted_at_ist` etc. are already IST; don't re-convert.
- **Counts vs money:** the `_number()` coercion on the backend (see `api/main.py`)
  guarantees every integral value arrives as a JSON number, never a string — so
  `typeof x === "number"` holds for every `_paise` and count field. If a fetch ever
  returns a string for one of these, that's a backend regression, not something to
  work around client-side with `parseInt`.
- **Never invent a number the API didn't return.** No page computes a total, a
  percentage, or a rate that isn't already a field in the response — if a page wants
  `recovered / at_risk`, compute it inline from the two fields that are already there,
  don't add a fake data layer.
- **Empty states are real states.** A run with zero violations shows `0` in green, not a
  hidden row. A worklist with nothing outstanding shows "queue is empty" — that is a
  legitimate and good state, style it calmly, not as an error.

---

## 06 — Motion budget (exactly four animations — nothing else moves)

Framer Motion, `prefers-reduced-motion` respected by disabling all four (render final
states immediately) when the media query matches.

1. **₹ recovered count-up** — spring easing, ~700ms, animates from 0 to
   `overview.recovered_paise` once per page load / run switch (not on every re-render —
   guard with a ref on the run id). Tabular-nums, no layout shift as digits change.
2. **Funnel bars fill left-to-right with 60ms stagger** — each stage's bar width animates
   from 0 to its proportional width, stage N starting 60ms after stage N−1, on the overview
   page.
3. **Live agent trace** — while polling `GET /runs/{id}/events`, each newly-arrived row
   slides/fades in at the top of a trace list, then its `authorizing_rule` chip flashes:
   blue background pulse on `guardrail_verdict === "APPROVE"`, **red pulse** on
   `compliance_violation === true` or a `stop_reason` present. This is the twenty-second
   scene from §00 — build and test it against a real running batch, not synthetic data.
4. **Drill-down drawer** — on `/invoices/[id]`, the decision cards (§03.3) can be entered
   either as the full page or, when linked from the worklist as a quick-look, as a
   right-side drawer that slides in over the current page rather than navigating away.
   Slide from the right, ~300ms, standard ease-out.

Everything else — table sorts, page transitions, hover states — is instant or CSS
transition only. No parallax, no bounce, no scroll-jacking.

---

## 07 — Live trace: the polling contract

There is no websocket on this backend and none should be added — `GET /runs/{id}/events`
is a plain cursored `GET`. The frontend implements the loop:

```
let cursor: number | null = null
async function poll() {
  const res = await fetch(`${API}/runs/${runId}/events?since=${cursor ?? ""}&limit=50`)
  const { cursor: next, events } = await res.json()
  if (events.length) { appendToTrace(events); cursor = next }
  setTimeout(poll, 1500)
}
```

Stop polling when the page/tab is hidden (`document.visibilityState`) and resume on
visibility, to avoid hammering the API from a backgrounded tab. There is no "run finished"
signal from the API — treat a run as live as long as its page is open; a finished run
simply stops producing new events, which the trace UI should read as "caught up", not as
an error.

---

## 08 — What "no mocks" means in practice, checked

- Every fetch call targets `VITE_API_BASE` (defaulting to `http://localhost:8000`, so a
  fresh clone needs no `.env` at all) — no `mockData.ts`, no fixture JSON checked into
  `dashboard/`.
- Empty-database states (fresh clone, before any batch has run) render real empty states
  from real empty API responses (`GET /runs` → `[]`), never a hardcoded "example" row.
- `scripts/run_demo.sh` has to be able to seed the database, run a batch, and have
  this dashboard render that exact run with no code change — that is the actual definition
  of "no mocks" the plan's Day-7 gate is checking.

**Verified on Day 7, and it cost something.** The build originally shipped a 473-line
`src/data/demo.ts` fallback that `api.ts` silently switched to whenever a fetch threw —
which meant the app could render a complete, entirely fictional ₹ figure while the backend
was down, directly under a footer reading *"every number traces to an API response — no
mocks."* That file is deleted. `api.ts` now has no fallback branch at all: an unreachable
API produces a named error and an empty region, never a plausible number. Two smaller
consequences of the same rule: the header badge reports live `GET /health` (including the
role it connected as and the append-only trigger count) rather than whether a build-time
env var was set, and the compliance page's search placeholder shows a real invoice id
pulled off `GET /worklist` instead of an invented one.

---

## 09 — Master prompt

**Historical.** This is the prompt the dashboard was actually built from, kept verbatim as
a record. It says Next.js because that was the intent on Day 6; the scaffold turned out to
be a Vite SPA and §01's stack note explains why that was left alone. Read §01, not this
section, for what the code is.

Paste everything between the lines into a fresh Claude Code session with this repo as the
working directory.

---

I'm building the Winback dashboard — the Next.js frontend for a Razorpay AI Buildathon
submission (Track 03, AI Revenue Recovery). Read `docs/FRONTEND_SPEC.md` in full before
writing anything — it is the complete contract: every page, every endpoint, every field
name, the palette, the motion budget, and the formatting rules. Follow it exactly; it was
written against the live API responses, not guessed.

Also read `docs/ARCHITECTURE.md` §05 (the read API) and skim `api/main.py` directly so you
have the real response shapes in front of you, not just the spec's paraphrase of them.
The backend is already running (or can be started with
`uvicorn api.main:app --reload --port 8000` from the `winback/` directory) against a
seeded Postgres database — hit the real endpoints while building, don't stub them.

Build order, and don't skip ahead of a gate:

1. Scaffold: Next.js 15 App Router + TypeScript strict + Tailwind v4, `NEXT_PUBLIC_API_BASE`
   env var, the Satoshi font via Fontshare, the palette as CSS custom properties exactly as
   listed in §02. No page content yet — just confirm one real fetch to `GET /health`
   renders on screen.
2. Build the shared compliance-panel component (§04) first, standalone, fed by
   `GET /invoices/{id}/compliance` against a real invoice id you look up via
   `GET /worklist`. This is the highest-trust piece of UI in the app and every other page
   either embeds it or reuses its verdict-chip styling — get it right once.
3. `/` Overview page (§03.1), including the ₹ count-up (§06.1) and the funnel stagger
   (§06.2).
4. `/worklist` (§03.2), both modes (live queue and run worklist), with pagination.
5. `/invoices/[id]` (§03.3), embedding the compliance panel from step 2 plus the attempt
   timeline, decision cards, and audit trail.
6. `/compliance` (§03.4), reusing the panel component and adding the live window strip.
7. Live trace (§06.3, §07) — wire the polling loop into the worklist or a dedicated
   run-detail view, and get the red-flash-on-violation moment actually working against a
   real batch run (`python -m agent.orchestrator --run-id <new-id>` from `winback/`, run
   in the background, watch the trace update).
8. `/evaluation` (§03.5) — table + charts from `GET /evaluation`, falling back to the
   committed PNGs for calibration as the spec allows, plus the hand-written honesty
   callout.
9. Drill-down drawer variant (§06.4) on the worklist for quick-look without full
   navigation.
10. Pass over every page against §05 (formatting rules) and §08 (no-mocks checklist) —
    literally verify no file in the dashboard contains hardcoded example data.

Use the `dataviz` skill before writing the funnel chart, the calibration chart, or the
four-arm/violations charts — validate the status-palette colors with its script before
shipping any chart that uses them together. Use the `frontend-design` skill before writing
the first component. Both are available as skills in this session — invoke them, don't
approximate their checklists from memory.

Non-negotiables, repeated because they're easy to drift from mid-build: no mocked data
anywhere, ever — every number traces to a real endpoint response. Only four animations
exist in the whole app, exactly as specified in §06 — resist adding a fifth for polish.
The `good`/`critical`/`warn`/`neutral` status colors are reserved for
recovered/blocked/escalated/written-off and never reused elsewhere. Rupees are always
formatted with Indian digit grouping and never rendered as raw paise. Respect
`prefers-reduced-motion` on all four animations.

When each of the five pages is done and rendering real data end to end, tell me what's
left against §00's build-order priority and what, if anything, in `docs/FRONTEND_SPEC.md`
turned out to not match what the API actually returns — update the spec file itself if you
find a mismatch, don't just work around it silently in the component.

---
