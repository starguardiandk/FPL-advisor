# FPL Advisor

Pulls your FPL squad (team ID 8007579), and emails you:
- Top 3 captain candidates for the upcoming gameweek
- Recommended starting XI vs bench, based on FPL's own `ep_next` projections
- Transfer suggestions for any injured/suspended/doubtful players in your squad

**What this does NOT do:** build a custom prediction model. It uses FPL's
own `ep_next` field (their expected-points-next-gameweek projection),
combined with straightforward rules (formation validity, injury flags,
budget constraints). See the chat where this was built for the reasoning.

## 1. Run it locally first (recommended before automating)

```bash
pip install -r requirements.txt
export FPL_TEAM_ID=8007579
python fpl_advisor.py
```

With no email credentials set, it prints the recommendation to your
console instead of emailing — good for a first test.

## 2. Set up email sending (Gmail)

1. Turn on 2-Step Verification on your Google account (required for app passwords).
2. Go to https://myaccount.google.com/apppasswords and create an app password
   for "Mail".
3. Set these env vars before running:
   ```bash
   export EMAIL_FROM="youraddress@gmail.com"
   export EMAIL_TO="youraddress@gmail.com"       # can be the same address
   export EMAIL_APP_PASSWORD="the 16-char app password"
   ```

## 3. Automate with GitHub Actions (free, no server needed)

1. Push this folder to a new GitHub repo.
2. In the repo: Settings -> Secrets and variables -> Actions -> New repository secret.
   Add each of: `FPL_TEAM_ID`, `EMAIL_FROM`, `EMAIL_TO`, `EMAIL_APP_PASSWORD`
   (and `FPL_FREE_TRANSFERS` if you want to set it — defaults to 1).
3. The workflow in `.github/workflows/fpl_advisor.yml` runs every Friday at
   18:00 UTC by default. Edit the cron line to change timing, or use the
   "Run workflow" button on the Actions tab to trigger it manually and test.

## How fixture difficulty is used

- **Captain & starting XI**: `ep_next` is multiplied by a factor derived from
  the player's *next* fixture's FDR (FPL's own 1-5 difficulty rating) — easy
  fixtures (FDR 1-2) boost the score, hard ones (FDR 4-5) reduce it. This is
  a simple linear heuristic (`(6 - FDR) / 3`), not a calibrated model —
  treat close calls as a nudge, not a verdict.
- **Transfers**: candidate replacements are ranked using their *average* FDR
  over the next `FPL_FIXTURE_HORIZON` gameweeks (default 3), since a
  transfer is a multi-week commitment, not a single-gameweek decision like
  captaincy.
- The email output shows each player's actual opponent, venue, and FDR so
  you can sanity-check the reasoning yourself rather than trusting the
  ranking blindly.

## Transfer logic

Two kinds of suggestions now, not just one:

1. **Injury/doubt replacements** — unchanged from before: anyone flagged
   injured, suspended, or under 75% chance of playing.
2. **Value upgrades** (new) — a fit player whose fixture-adjusted outlook
   over the next `FPL_FIXTURE_HORIZON` gameweeks is clearly worse than an
   affordable alternative in the same position. Gated by a fixed threshold
   (`MIN_VALUE_IMPROVEMENT` in the code) so it doesn't suggest marginal
   swaps — a transfer beyond your free ones costs -4 points, so "slightly
   better" isn't worth flagging.

Each suggestion is labeled `(free)` or `(costs -4 pts)` based on
`FPL_FREE_TRANSFERS`, and whether it came from the injury check or the
value check.

**Budget accuracy**: transfer budgets now use each player's real *sell*
price, not their current market price — FPL only refunds half your profit
when a player has risen in value (the "sell-on fee"), so using market price
overstated what you could actually afford. Sell price is reconstructed from
your transfer history when available (exact), falling back to a
season-start approximation (`now_cost - cost_change_start`) for players
you've held since the opening gameweek — exact if their price hasn't moved
since, otherwise a reasonable but not perfectly precise estimate, since the
public API has no way to see your exact original purchase price for
day-one squad members without you being authenticated.

## Chip suggestions (wildcard, free hit, bench boost, triple captain)

**Read this before trusting these** — chip timing is one of the most
strategic, season-long parts of FPL, and this script can only see what's
knowable right now: confirmed fixture counts for the upcoming gameweek(s)
within `FPL_FIXTURE_HORIZON`, your current squad's injury/value flags, and
which chips you've already used this half of the season (checked properly —
wildcard and free hit each have independent first-half/second-half windows,
tracked separately). It has NO knowledge of blank/double gameweeks that
haven't been announced yet, and won't tell you to save a chip for a better
week beyond your configured horizon. Treat every note as "here's the
evidence, you decide" — not a command.

What triggers each note:
- **Triple Captain**: your top fixture-adjusted captain pick has a double
  gameweek (2 fixtures) this week.
- **Bench Boost**: at least 6 of your 15 squad players have a double
  gameweek.
- **Free Hit**: at least 4 of your 15 squad players have a blank (no
  fixture) this gameweek.
- **Wildcard**: either 4+ squad players are currently flagged as
  injured/suspended/poor-value, or an upcoming gameweek within your horizon
  disrupts 5+ of your players' fixtures (a mix of doubles/blanks).

All four thresholds are named constants at the top of `recommend_chips()`
in the code — reasonable guesses, not calibrated against real outcomes.
Tune them if they feel too trigger-happy or too quiet.

## Free transfers

**Now auto-computed** from your real transfer history — you no longer need
to track this manually. The real rule: 1 free transfer earned per gameweek
deadline, banked up to a max of 5; Wildcard/Free Hit preserve your banked
count untouched (that gameweek's transfers don't draw from it, and you
still get the normal +1 the next week); paid transfers beyond your bank
don't create a negative count, they just cost points.

This is reconstructed by simulating that rule across your entire season's
`event_transfers` history (`/entry/{id}/history/`) — not read from a single
field, since FPL's API doesn't expose a "current free transfers" number
directly. `FPL_FREE_TRANSFERS` still exists as a manual override (useful if
you ever suspect the computed value is off, or want to test a hypothetical)
— set it and the auto-computation is skipped entirely for that run.

**Known limitation**: the simulation assumes a normal, uninterrupted season
history starting from gameweek 1 under the current entry ID. Mid-season
squad transfers between managers, or seasons where the ruleset itself
changed partway through, aren't accounted for — if your computed number
ever looks wrong, cross-check it against the official site and fall back
to the manual override.

## Bench player valuation (auto-substitution)

Bench players only score if FPL auto-subs them into your starting XI, which
only happens when a starter records 0 minutes. So a bench player's real
expected value is `ep_next × probability of actually being subbed in` — not
their raw `ep_next`. This is now modeled explicitly:

- For each bench player, the script finds the same-position starter with
  the highest blank risk (lowest `chance_of_playing_next_round`) and uses
  `(100 - that chance) / 100` as the auto-sub probability. A starter with
  no reported fitness doubt gets a small 2% baseline (real football has
  occasional last-minute non-injury absences).
- A bench-only transfer suggestion's raw score gain is multiplied by this
  probability before being compared against `MIN_VALUE_IMPROVEMENT`. In
  practice: a big upgrade sitting behind a fully fit starter will almost
  never clear the bar (correctly — it's very unlikely to ever matter), but
  the same upgrade behind a genuinely doubtful starter can.
- **Known simplification**: this only models same-position auto-subs (a
  bench DEF for a doubtful starting DEF, etc.), which is the common case
  and always formation-legal. It does NOT model the rarer cross-position
  case (e.g. a bench midfielder covering for a blanked defender when your
  defense is already at its 3-player minimum) — simulating exact formation
  eligibility for that edge case wasn't judged worth the added complexity.
  Treat the probability as a reasonable floor, not an exact figure.

## When the email actually sends

The script reads the real deadline for the upcoming gameweek straight from
FPL's own schedule (`bootstrap-static`'s `deadline_time`) — nothing is
hardcoded, so this stays correct even when a gameweek's deadline falls on
an unusual day (rearranged fixtures, midweek rounds, etc.).

Since deadlines aren't on a fixed weekly schedule, the GitHub Actions
workflow runs every 3 hours (`0 */3 * * *`), and the **script itself**
decides each time whether to actually send:

- `FPL_HOURS_BEFORE_DEADLINE` (default 24) — how long before the deadline
  you want the email.
- `FPL_SEND_WINDOW_TOLERANCE_HOURS` (default 3) — how much slack around
  that target to allow, since cron doesn't fire at the exact second and a
  3-hour run cadence means the "right moment" might be missed by up to
  ~1.5 hours either way without slack.
- A small state file (`state/last_notified.json`) records which gameweek
  was last emailed, so multiple runs landing inside the same window don't
  send duplicate emails. The GitHub Actions workflow commits this file back
  to the repo after each run so the state persists between runs (Actions
  runners don't keep local files otherwise).
- Testing locally without email credentials set never updates this state
  file — the run just prints to console — so you can test repeatedly
  without falsely blocking a real future send.
- To force an email immediately regardless of timing (for testing), either
  set `FPL_FORCE_SEND=1` locally, or use the "Run workflow" button on the
  Actions tab with the `force_send` input checked.

**Worth setting up `FPL_HOURS_BEFORE_DEADLINE` as a repo secret** even
though it has a default — that way changing your lead-time preference
later doesn't require editing code.

## Known limitations / things to improve

- **Free transfers**: the FPL API doesn't cleanly expose your current free
  transfer count, so this is a manual env var (`FPL_FREE_TRANSFERS`) you
  need to keep updated yourself, or extend the script to calculate it from
  `/entry/{id}/history/` transfer history.
- **Chips** (wildcard, bench boost, triple captain, free hit) aren't
  suggested at all yet — this needs its own logic since chip timing is
  strategic (e.g. saving triple captain for a double gameweek).
- Tested only with mock squad/fixture data in this environment (see
  `test_logic.py`) — the FPL-API-calling functions (`get_picks`,
  `get_entry_info`, `get_fixtures`) are untested against live data in this
  sandbox because it couldn't reach your specific endpoint. You already
  confirmed the core pull works against your real team; re-run after this
  update to confirm the fixture data and next-gameweek targeting also work
  end-to-end.
- **Gameweek targeting fix**: earlier versions of this script pulled picks
  for the *current* gameweek, which is already locked once its deadline has
  passed — meaning "recommendations" could describe a decision you could no
  longer act on. It now targets the *next* gameweek (the one with an open
  deadline) and falls back to the current one with a clear warning if the
  next gameweek's picks aren't fetchable yet.
