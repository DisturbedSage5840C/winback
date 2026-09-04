# Demo script — plain version

> Every number below is real, pulled from the actual running system — nothing is
> made up for the video. Keep the tone conversational, like explaining the project
> to a friend, not pitching a panel.

## The shape of the video

1. Say what the project is and why it exists (30–45 sec)
2. Show the GitHub repo, briefly (20–30 sec)
3. Show the website actually working (2–3 min)
4. Close (15 sec)

Total: roughly 4 minutes. No need to time it to the second — just keep it moving.

## 1. Intro — what is this and why

Say something like:

> "This is Winback. When a UPI Autopay payment fails, most businesses either give up
> or just retry it blindly and hope. The problem is you're not actually allowed to
> retry however you want — there's a hard rule from NPCI: one attempt plus three
> retries, and only during certain hours. Winback figures out, for every failed
> payment, whether a retry is even legal, and if it is, when the best time to try
> again is. It recovered about ₹3.57 lakh across 190 test invoices, without breaking
> a single rule."

Keep this short. No architecture diagram needed unless it helps you explain it —
if you want one, a single sentence over it is enough: "Compliance checks the rules
first, then the model picks the best legal option, and everything gets logged."

## 2. The GitHub repo

Open the repo in a browser. Just enough to show it's real, organized, and yours:

- Scroll the README for a few seconds
- Point out the folder structure briefly (backend, dashboard, tests) — no need to
  open individual files
- Mention there's a full test suite and it's all documented

Say something like:

> "Here's the code — it's all open on GitHub. Backend, the compliance rules, the
> model, and the dashboard you're about to see, all in one repo, with tests for
> the important parts."

Don't linger. This section exists so people believe the demo is real, not to
explain the code.

## 3. The website, working

Open the dashboard. Confirm the health badge is green before you start recording.

**Overview page**
Show the summary numbers — recovered amount, how many were deferred, how many
were blocked, how many failed. Say what they mean in plain words:

> "This is one full batch of 190 failed payments. About ₹3.57 lakh got recovered
> legally. Some were deferred because it wasn't the right time yet, some were
> blocked because retrying them would've broken the rules."

**Worklist → drill into one invoice**
Click into a single invoice. Show the options it considered and which one it
picked.

> "For every failed payment, the system looks at every legal way it could retry —
> which day, which time window — prices each option, and picks the best one."

**The compliance check**
Go to the compliance page, type in an invoice id that hits the retry limit
(`inv_3890_01`), and show the result.

> "This one already used all its legal retries. Everything else about it looks
> fine — the amount's fine, the timing's fine, notice went out — but the answer is
> still no, because it's already used its budget. That's not the AI being
> cautious, that's just the rule. A computer program decides this, not a
> guess — there's no room for the model to talk its way around a legal limit."

**Evaluation page**
Briefly show the comparison between "just retry blindly" and Winback's approach.

> "We compared this against just retrying everything with no rules. The blind
> approach actually broke the rules 66 times. Winback recovered basically the same
> amount of money — but with zero violations."

You can mention, briefly and honestly, that the results come from a simulated
environment built to mirror real payment behavior, not live production traffic —
one sentence, no need to dwell on it.

## 4. Close

> "That's Winback — a system that finds the legal way to retry a failed payment,
> and never the illegal one. Built in [X] days, code's on GitHub, and the
> dashboard you just saw is live."

Show the repo URL or the live site URL one more time on screen and end there.

## A few things to keep in mind while recording

- Bring the API up first and check the dashboard's health badge is green —
  recording with a red badge means a retake.
- Don't read numbers off a script word-for-word — say them the way you'd explain
  them to someone, it'll sound more natural.
- If something on screen looks broken or empty, pause and fix it before
  continuing rather than talking over it.
- Record a rough draft first as a safety net before doing the take you'll
  actually submit.
