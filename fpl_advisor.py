"""
FPL Advisor — pulls your FPL squad and recommends transfers, captain, and
starting XI changes to maximize points in the NEXT actionable gameweek
(the one whose deadline hasn't passed yet), factoring in fixture difficulty.

Run: python fpl_advisor.py
Config: set env vars (see README.md) or edit config.py directly.
"""

import os
import json
from collections import defaultdict
from datetime import datetime, timezone
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

BASE = "https://fantasy.premierleague.com/api"

# ---- Config (env vars override these defaults) ----
TEAM_ID = int(os.environ.get("FPL_TEAM_ID", "8007579"))
FREE_TRANSFERS_OVERRIDE = os.environ.get("FPL_FREE_TRANSFERS")  # optional manual override; unset -> auto-computed each run from your real transfer history
FIXTURE_HORIZON = int(os.environ.get("FPL_FIXTURE_HORIZON", "3"))  # gameweeks ahead to average FDR over for transfer targets
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
HOURS_BEFORE_DEADLINE = float(os.environ.get("FPL_HOURS_BEFORE_DEADLINE", "24"))
SEND_WINDOW_TOLERANCE_HOURS = float(os.environ.get("FPL_SEND_WINDOW_TOLERANCE_HOURS", "3"))
FORCE_SEND = os.environ.get("FPL_FORCE_SEND", "").lower() in ("1", "true", "yes")
STATE_PATH = os.environ.get("FPL_STATE_PATH", "state/last_notified.json")

POSITION_NAMES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def fetch_json(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def get_bootstrap():
    return fetch_json(f"{BASE}/bootstrap-static/")


def get_fixtures():
    # Deliberately NOT using ?future=1 — that excludes fixtures that have
    # already kicked off, which breaks fixture lookups whenever picks fall
    # back to the current (in-progress) gameweek. The full list is ~380
    # fixtures for a season, small enough to fetch and filter locally.
    return fetch_json(f"{BASE}/fixtures/")


def get_current_and_next_event(bootstrap):
    """Returns (current_event_id, next_event_id). next is the one whose
    deadline hasn't passed — the one recommendations should target."""
    current, nxt = None, None
    for e in bootstrap["events"]:
        if e["is_current"]:
            current = e["id"]
        if e["is_next"]:
            nxt = e["id"]
    return current, nxt


def get_deadline(bootstrap, event_id):
    """ISO8601 UTC deadline string (e.g. '2026-09-12T12:30:00Z') for the
    given gameweek, straight from FPL's own schedule — nothing hardcoded."""
    for e in bootstrap["events"]:
        if e["id"] == event_id:
            return e["deadline_time"]
    return None


def hours_until(deadline_iso):
    deadline_dt = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (deadline_dt - now).total_seconds() / 3600.0


def should_send_now(hours_remaining, target_hours_before, tolerance_hours):
    """True if 'hours_remaining until deadline' falls inside the window
    around your target lead time. The tolerance exists because a cron job
    fires at approximate times, not the exact second — without slack, a
    5-minute scheduling delay could make the run miss the window entirely
    and skip sending that week."""
    return (target_hours_before - tolerance_hours) <= hours_remaining <= (target_hours_before + tolerance_hours)


def read_last_notified_event(path):
    try:
        with open(path) as f:
            return json.load(f).get("event")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_last_notified_event(path, event_id):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"event": event_id, "sent_at": datetime.now(timezone.utc).isoformat()}, f)


def get_picks_safe(team_id, event_id):
    try:
        return fetch_json(f"{BASE}/entry/{team_id}/event/{event_id}/picks/")
    except requests.HTTPError:
        return None


def get_entry_info(team_id):
    return fetch_json(f"{BASE}/entry/{team_id}/")


def get_entry_history(team_id):
    """Includes 'chips': [{'name': 'wildcard', 'event': N, ...}, ...] and
    'current': [{'event': N, 'event_transfers': N, ...}, ...] — everything
    needed to reconstruct both chip usage and free-transfer banking."""
    try:
        return fetch_json(f"{BASE}/entry/{team_id}/history/")
    except requests.HTTPError:
        return {"chips": [], "current": []}


def compute_free_transfers(entry_history, decision_event):
    """
    Simulates FPL's free-transfer banking rule from the entry's own history,
    so you don't have to track it manually:
      - 1 free transfer awarded per gameweek deadline, banked up to a max of 5.
      - Wildcard/Free Hit do NOT consume the bank — that gameweek's transfers
        are free regardless of count, and the bank is untouched — but you
        still receive the normal +1 the following gameweek.
      - Paid transfers beyond your banked amount don't create a negative
        bank; they just cost points (-4 each) and leave the bank at 0 for
        that step before the next +1 is added.

    The free-transfer system has no meaning for gameweek 1 (that's initial
    squad selection, not a transfer), so simulation starts at gameweek 2
    with a banked count of 1 — this naturally falls out of only iterating
    gameweeks in [2, decision_event).
    """
    chips_used_by_event = {c["event"]: c["name"] for c in entry_history.get("chips", [])}
    relevant = sorted(
        (e for e in entry_history.get("current", []) if 2 <= e["event"] < decision_event),
        key=lambda e: e["event"]
    )

    banked = 1  # entering gameweek 2, standard starting point
    for entry in relevant:
        chip = chips_used_by_event.get(entry["event"])
        transfers_used = 0 if chip in ("wildcard", "freehit") else entry.get("event_transfers", 0)
        consumed = min(transfers_used, banked)
        banked = min(5, banked - consumed + 1)

    return banked


def available_chips(bootstrap, entry_history, decision_event):
    """
    Which chips can still be played THIS gameweek. Handles the fact that
    wildcard and free hit each have two separate windows (first half /
    second half of season) that share the same chip 'name' in the API —
    bootstrap['chips'] defines each window's start/stop event and its own
    'number' (1 or 2); a chip used in window 1 doesn't block window 2.
    """
    used_events_by_name = {}
    for c in entry_history.get("chips", []):
        used_events_by_name.setdefault(c["name"], []).append(c["event"])

    available = set()
    for chip_def in bootstrap["chips"]:
        if not (chip_def["start_event"] <= decision_event <= chip_def["stop_event"]):
            continue
        used_in_this_window = any(
            chip_def["start_event"] <= ev <= chip_def["stop_event"]
            for ev in used_events_by_name.get(chip_def["name"], [])
        )
        if not used_in_this_window:
            available.add(chip_def["name"])
    return available


def get_fixture_counts_per_team(fixtures, event):
    """team_id -> number of fixtures in this gameweek. 0 = blank, 2 = double."""
    counts = defaultdict(int)
    for f in fixtures:
        if f.get("event") != event:
            continue
        counts[f["team_h"]] += 1
        counts[f["team_a"]] += 1
    return counts


def recommend_chips(squad, bench, captain_options, next_fixture_info, fixture_counts,
                     transfer_suggestions, available_chip_names, fixtures, decision_event, fdr_horizon):
    """
    Heuristic chip nudges based only on what's currently knowable: confirmed
    fixture counts for the upcoming gameweek(s), your current squad health,
    and which chips you haven't already used. This is NOT season-long chip
    planning — it can't know about a blank/double gameweek that hasn't been
    announced yet, and won't tell you to "save" a chip for a better future
    week beyond what FIXTURE_HORIZON already reveals.
    """
    # Thresholds below are reasonable-guess heuristics, not calibrated —
    # treat every note as a prompt to look closer, not a verdict.
    DOUBLE_GW_SQUAD_THRESHOLD = 6   # of 15 squad players with 2 fixtures -> strong bench boost signal
    BLANK_GW_SQUAD_THRESHOLD = 4    # of 15 squad players with 0 fixtures -> strong free hit signal
    SQUAD_ISSUE_THRESHOLD = 4       # injury-flagged + value-upgrade-flagged players -> consider wildcard
    UPCOMING_DISRUPTION_THRESHOLD = 5

    notes = []

    # --- Triple Captain ---
    if "3xc" not in available_chip_names:
        notes.append("TRIPLE CAPTAIN: already used this half of the season (or not yet available).")
    elif captain_options:
        top = captain_options[0]
        n_fix = fixture_counts.get(top["team"], 1)
        if n_fix >= 2:
            notes.append(f"TRIPLE CAPTAIN: worth considering — {top['web_name']} has a double gameweek ({n_fix} fixtures).")
        else:
            notes.append(f"TRIPLE CAPTAIN: not flagged this week — {top['web_name']} has a single fixture, no double gameweek detected.")

    # --- Bench Boost ---
    if "bboost" not in available_chip_names:
        notes.append("BENCH BOOST: already used this half of the season (or not yet available).")
    else:
        doubles_count = sum(1 for p in squad if fixture_counts.get(p["team"], 1) >= 2)
        if doubles_count >= DOUBLE_GW_SQUAD_THRESHOLD:
            notes.append(f"BENCH BOOST: worth considering — {doubles_count} of your 15 players have a double gameweek this week.")
        else:
            notes.append(f"BENCH BOOST: not flagged this week — only {doubles_count} of 15 players have a double gameweek.")

    # --- Free Hit ---
    if "freehit" not in available_chip_names:
        notes.append("FREE HIT: already used this half of the season (or not yet available).")
    else:
        blanks_count = sum(1 for p in squad if fixture_counts.get(p["team"], 1) == 0)
        if blanks_count >= BLANK_GW_SQUAD_THRESHOLD:
            notes.append(f"FREE HIT: worth considering — {blanks_count} of your 15 players have no fixture this gameweek (blank).")
        else:
            notes.append(f"FREE HIT: not flagged this week — only {blanks_count} of 15 players have a blank.")

    # --- Wildcard ---
    if "wildcard" not in available_chip_names:
        notes.append("WILDCARD: already used this half of the season (or not yet available).")
    else:
        squad_issues = len({s["out"]["id"] for s in transfer_suggestions})
        upcoming_disruption = 0
        for offset in range(fdr_horizon):
            fc = get_fixture_counts_per_team(fixtures, decision_event + offset)
            disrupted = sum(1 for p in squad if fc.get(p["team"], 1) != 1)
            upcoming_disruption = max(upcoming_disruption, disrupted)
        if squad_issues >= SQUAD_ISSUE_THRESHOLD:
            notes.append(f"WILDCARD: worth considering — {squad_issues} squad players currently flagged as injured/suspended/poor value.")
        elif upcoming_disruption >= UPCOMING_DISRUPTION_THRESHOLD:
            notes.append(f"WILDCARD: worth considering — an upcoming gameweek within your {fdr_horizon}-GW horizon disrupts {upcoming_disruption} of your players' fixtures.")
        else:
            notes.append("WILDCARD: not flagged — squad health and near-term fixtures look normal.")

    return notes


def get_transfer_history(team_id):
    """List of all transfers this entry has ever made. Used to reconstruct
    real purchase prices — the public API has no direct 'my current squad
    with purchase prices' endpoint without authenticating as the user."""
    try:
        return fetch_json(f"{BASE}/entry/{team_id}/transfers/")
    except requests.HTTPError:
        return []


def build_purchase_prices(squad, transfer_history):
    """
    player_id -> purchase price (same 0.1m-unit scale as now_cost).

    Approach: if the player was ever transferred IN, use the most recent
    such transfer's recorded cost (exact). Otherwise, assume they've been
    held since the season opener and approximate their purchase price as
    now_cost - cost_change_start (the player's total price movement since
    the season began). This approximation is exact for anyone who has never
    moved in price, and only approximate — not wrong in direction, just
    potentially off by a few tenths — for early-squad players whose price
    has changed since day one.
    """
    prices = {}
    # Most recent transfer-in per player (transfers list order isn't
    # guaranteed, so take the max by event/time rather than assuming order).
    latest_buy = {}
    for t in transfer_history:
        pid = t.get("element_in")
        if pid is None:
            continue
        event = t.get("event", 0)
        if pid not in latest_buy or event >= latest_buy[pid][0]:
            latest_buy[pid] = (event, t.get("element_in_cost"))

    for p in squad:
        if p["id"] in latest_buy and latest_buy[p["id"]][1] is not None:
            prices[p["id"]] = latest_buy[p["id"]][1]
        else:
            prices[p["id"]] = p["now_cost"] - p.get("cost_change_start", 0)
    return prices


def compute_sell_price(now_cost, purchase_price):
    """FPL's sell-on-fee rule: if the player has risen in price since you
    bought them, you only recoup half the profit (rounded down); if they've
    fallen, you sell at the current (lower) price with no extra penalty."""
    profit = now_cost - purchase_price
    if profit > 0:
        return purchase_price + profit // 2
    return now_cost


def build_player_index(bootstrap):
    """id -> player dict, with team short name attached."""
    teams_by_id = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    index = {}
    for el in bootstrap["elements"]:
        el = dict(el)
        el["team_short"] = teams_by_id.get(el["team"], "?")
        el["pos"] = POSITION_NAMES.get(el["element_type"], "?")
        index[el["id"]] = el
    return index


def build_next_fixture_info(fixtures, target_event, teams_by_id):
    """team_id -> {'opponent': str, 'venue': 'H'/'A', 'fdr': int} for target_event."""
    info = {}
    for f in fixtures:
        if f.get("event") != target_event:
            continue
        h, a = f["team_h"], f["team_a"]
        info[h] = {"opponent": teams_by_id.get(a, "?"), "venue": "H", "fdr": f["team_h_difficulty"]}
        info[a] = {"opponent": teams_by_id.get(h, "?"), "venue": "A", "fdr": f["team_a_difficulty"]}
    return info


def build_avg_fixture_difficulty(fixtures, start_event, horizon):
    """team_id -> average FDR over [start_event, start_event+horizon)."""
    sums = defaultdict(list)
    for f in fixtures:
        ev = f.get("event")
        if ev is None or ev < start_event or ev >= start_event + horizon:
            continue
        sums[f["team_h"]].append(f["team_h_difficulty"])
        sums[f["team_a"]].append(f["team_a_difficulty"])
    return {team: sum(vals) / len(vals) for team, vals in sums.items()}


def fixture_multiplier(fdr):
    """FDR runs 1 (easiest) to 5 (hardest). Converts to a score multiplier:
    FDR 1 -> 1.67x, FDR 3 -> 1.0x (neutral), FDR 5 -> 0.33x.
    This is a simple linear heuristic, not a calibrated model — treat the
    resulting ranking as a nudge, not gospel, especially for close calls."""
    if fdr is None:
        return 1.0
    return (6 - fdr) / 3.0


def weighted_score(player, next_fixture_info):
    """ep_next adjusted for the difficulty of the player's next fixture."""
    ep = float(player["ep_next"] or 0)
    fdr = next_fixture_info.get(player["team"], {}).get("fdr")
    return ep * fixture_multiplier(fdr)


def squad_from_picks(picks, player_index):
    squad = []
    for p in picks["picks"]:
        pl = player_index[p["element"]]
        squad.append({
            **pl,
            "is_captain": p["is_captain"],
            "is_vice_captain": p["is_vice_captain"],
            "multiplier": p["multiplier"],  # 0 if benched (as last set)
        })
    return squad


def recommend_captain(squad, next_fixture_info):
    """Highest fixture-weighted ep_next among likely starters (multiplier > 0
    as last set — this is just used to identify your XI, not to lock it)."""
    starters = [p for p in squad if p["multiplier"] > 0]
    ranked = sorted(starters, key=lambda p: weighted_score(p, next_fixture_info), reverse=True)
    return ranked[:3]


def recommend_best_xi(squad, next_fixture_info):
    """
    Pick the best valid starting XI from the 15 squad players by
    fixture-weighted ep_next, respecting FPL formation rules
    (1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD, 11 total).
    """
    by_pos = {"GKP": [], "DEF": [], "MID": [], "FWD": []}
    for p in squad:
        by_pos[p["pos"]].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: weighted_score(p, next_fixture_info), reverse=True)

    xi = [by_pos["GKP"][0]]
    remaining_slots = 10
    mins = {"DEF": 3, "MID": 2, "FWD": 1}
    maxs = {"DEF": 5, "MID": 5, "FWD": 3}

    for pos in ["DEF", "MID", "FWD"]:
        xi.extend(by_pos[pos][:mins[pos]])
        remaining_slots -= mins[pos]

    pool = []
    for pos in ["DEF", "MID", "FWD"]:
        pool.extend(by_pos[pos][mins[pos]:maxs[pos]])
    pool.sort(key=lambda p: weighted_score(p, next_fixture_info), reverse=True)
    xi.extend(pool[:remaining_slots])

    bench = [p for p in squad if p not in xi]
    return xi, bench


def flag_injury_risks(squad):
    risks = []
    for p in squad:
        if p["status"] in ("i", "d", "s", "u") or (p["chance_of_playing_next_round"] is not None and p["chance_of_playing_next_round"] < 75):
            risks.append(p)
    return risks


def estimate_autosub_probability(bench_player, squad):
    """
    Rough probability this bench player actually gets auto-subbed into the
    starting XI this gameweek, i.e. actually plays for you.

    FPL's real rule: a bench player comes on only if a starter records 0
    minutes AND the swap keeps a valid formation. This function only models
    the SAME-POSITION case (bench DEF subs in for a doubtful starting DEF,
    etc.) — that's the common case and is always formation-legal, so it's
    a reasonable floor. It does NOT model the rarer cross-position case
    (e.g. a bench midfielder covering for a blanked defender when your
    defense is already above its minimum) — that would require simulating
    the specific formation, which is more complexity than the accuracy
    gain justifies here. Treat this as a floor estimate, not exact.

    Approximation: uses the single most-at-risk starter in the same
    position (lowest chance_of_playing_next_round) as representative of
    "will a slot open up" — a starter with no reported doubt (chance is
    None, meaning FPL hasn't flagged them) gets a small baseline risk
    rather than treating them as a guaranteed 0% chance of blanking.
    """
    BASELINE_RISK = 0.02  # small chance even an apparently-fit player blanks unexpectedly

    same_pos_starters = [p for p in squad if p["pos"] == bench_player["pos"] and p["multiplier"] > 0]
    if not same_pos_starters:
        return 0.0

    riskiest = min(
        same_pos_starters,
        key=lambda p: p["chance_of_playing_next_round"] if p["chance_of_playing_next_round"] is not None else 100
    )
    chance = riskiest["chance_of_playing_next_round"]
    if chance is None:
        return BASELINE_RISK
    return max(BASELINE_RISK, (100 - chance) / 100.0)


def suggest_transfers(squad, player_index, bank, sell_prices, free_transfers, avg_fdr, fdr_horizon, xi_ids):
    """
    Two kinds of suggestions, combined into one ranked list:

    1. INJURY/DOUBT replacements — for anyone flagged by flag_injury_risks.
       Always included regardless of free_transfers count (you may need to
       take the hit).
    2. VALUE UPGRADES — a fit player whose fixture-adjusted outlook over the
       next `fdr_horizon` gameweeks is clearly worse than an affordable
       alternative in the same position. "Clearly worse" uses a fixed
       threshold (MIN_VALUE_IMPROVEMENT) so trivial, noisy swaps aren't
       suggested — a transfer beyond your free ones costs -4 points, so the
       bar for "worth it" should be more than marginal.

    Two things this function deliberately guards against:
    - Suggesting the SAME incoming player as the replacement for two
      different outgoing players (you can only buy them once) — handled via
      a shared `already_suggested_in` exclusion set across both passes.
    - Treating a bench-only upgrade the same as a starting-XI upgrade — a
      bench swap doesn't change your score unless that player is likely to
      start, so starter swaps are ranked first via `xi_ids` (your current
      recommended starting XI), and every suggestion is tagged so you can
      see which kind it is.

    Budget for each swap = bank + this player's REAL sell price (accounting
    for the 50% sell-on fee on any profit), not their current market price.
    """
    MIN_VALUE_IMPROVEMENT = 1.5  # min gain in horizon-avg fixture-weighted score to bother suggesting
    MAX_VALUE_SUGGESTIONS = 3

    squad_ids = {p["id"] for p in squad}
    team_counts = {}
    for p in squad:
        team_counts[p["team"]] = team_counts.get(p["team"], 0) + 1

    already_suggested_in = set()

    def find_best_replacement(out_player, budget):
        candidates = [
            pl for pl in player_index.values()
            if pl["pos"] == out_player["pos"]
            and pl["id"] not in squad_ids
            and pl["id"] not in already_suggested_in
            and pl["now_cost"] <= budget
            and pl["status"] == "a"
            and team_counts.get(pl["team"], 0) < 3
        ]
        for c in candidates:
            c["_fdr_score"] = float(c["ep_next"] or 0) * fixture_multiplier(avg_fdr.get(c["team"]))
        # Tie-break deterministically on player id when scores are exactly
        # equal, so identical input always gives identical output — makes
        # any future "the suggestion changed" report actually mean the
        # underlying live data changed, not the code.
        candidates.sort(key=lambda p: (p["_fdr_score"], -p["id"]), reverse=True)
        return candidates[0] if candidates else None

    def make_suggestion(out_player, best, reason, autosub_prob=1.0):
        sell = sell_prices.get(out_player["id"], out_player["now_cost"])
        out_score = float(out_player["ep_next"] or 0) * fixture_multiplier(avg_fdr.get(out_player["team"]))
        weekly_gain = (best["_fdr_score"] - out_score) * autosub_prob
        return {
            "out": out_player,
            "in": best,
            "cost_delta": (best["now_cost"] - sell) / 10.0,
            "in_avg_fdr": avg_fdr.get(best["team"]),
            "fdr_horizon": fdr_horizon,
            "reason": reason,
            "affects_starting_xi": out_player["id"] in xi_ids,
            "autosub_probability": autosub_prob,
            "weekly_gain": weekly_gain,  # expected extra points per gameweek this swap is worth, going forward
        }

    suggestions = []
    injury_flagged_ids = set()

    # Injury pass: process starters first, since an injured/doubtful starter
    # is more urgent than an injured/doubtful bench player — and tag bench
    # cases with their real autosub probability too, since fixing an
    # injured bench player is close to meaningless on its own (they weren't
    # going to play regardless) unless a same-position starter is also
    # genuinely at risk.
    injury_risks = sorted(flag_injury_risks(squad), key=lambda p: p["id"] not in xi_ids)
    for out_player in injury_risks:
        injury_flagged_ids.add(out_player["id"])
        is_starter = out_player["id"] in xi_ids
        autosub_prob = 1.0 if is_starter else estimate_autosub_probability(out_player, squad)
        sell = sell_prices.get(out_player["id"], out_player["now_cost"])
        best = find_best_replacement(out_player, bank + sell)
        if best:
            already_suggested_in.add(best["id"])
            suggestions.append(make_suggestion(out_player, best, "injury/doubt", autosub_prob))

    # Value pass: greedy round-by-round assignment. Each round, recompute
    # every remaining candidate's best-available replacement FRESH (so it
    # respects exclusions from prior rounds), then award the single best
    # by EXPECTED-VALUE gain to lock in.
    #
    # Bench players' raw gain is discounted by estimate_autosub_probability
    # — a big ep_next improvement to a bench player is only worth anything
    # in proportion to how likely they are to actually play (via auto-sub).
    # Starters use the full raw gain (probability effectively 1, since
    # they're either playing or already caught by the injury pass above).
    # This replaces a cruder "starters always outrank bench" rule with an
    # apples-to-apples expected-points comparison — a bench upgrade behind
    # a genuinely doubtful starter can legitimately outrank a marginal
    # starter swap, and a bench upgrade behind a fully fit starter should
    # almost never clear the threshold, which is the correct behavior.
    remaining = [p for p in squad if p["id"] not in injury_flagged_ids]
    while remaining and sum(1 for s in suggestions if s["reason"] == "value upgrade") < MAX_VALUE_SUGGESTIONS:
        round_best = None  # (effective_gain, out_player, candidate, autosub_prob)
        for out_player in remaining:
            is_starter = out_player["id"] in xi_ids
            autosub_prob = 1.0 if is_starter else estimate_autosub_probability(out_player, squad)
            out_score = float(out_player["ep_next"] or 0) * fixture_multiplier(avg_fdr.get(out_player["team"]))
            sell = sell_prices.get(out_player["id"], out_player["now_cost"])
            candidate = find_best_replacement(out_player, bank + sell)
            if candidate is None:
                continue
            raw_gain = candidate["_fdr_score"] - out_score
            effective_gain = raw_gain * autosub_prob
            if effective_gain < MIN_VALUE_IMPROVEMENT:
                continue
            if round_best is None or effective_gain > round_best[0]:
                round_best = (effective_gain, out_player, candidate, autosub_prob)

        if round_best is None:
            break  # no remaining player has a qualifying upgrade available
        _, chosen_out, chosen_in, chosen_prob = round_best
        already_suggested_in.add(chosen_in["id"])
        suggestions.append(make_suggestion(chosen_out, chosen_in, "value upgrade", chosen_prob))
        remaining.remove(chosen_out)

    for idx, s in enumerate(suggestions):
        s["costs_points"] = idx >= free_transfers  # beyond your free transfers, each extra costs -4 pts

    return suggestions


def fmt_fixture(player, next_fixture_info):
    fi = next_fixture_info.get(player["team"])
    if not fi:
        return "no fixture"
    return f"vs {fi['opponent']} ({fi['venue']}), FDR {fi['fdr']}"


def format_email(gw, squad, captain_options, xi, bench, transfer_suggestions, entry_info, next_fixture_info, picks_stale, chip_notes):
    lines = []
    lines.append(f"FPL Gameweek {gw} Recommendations")
    lines.append(f"Team: {entry_info.get('name', '')} | Overall rank: {entry_info.get('summary_overall_rank', 'N/A')}")
    if picks_stale:
        lines.append("NOTE: the public FPL API can't show your saved squad for a future "
                      "deadline without you being logged in, so this uses your squad as of "
                      "your last-played gameweek. This is your real 15 players unless you've "
                      "made transfers since — if you have, this won't reflect them yet.")
    lines.append("")

    lines.append("CAPTAIN PICKS (fixture-adjusted, in order — check injury news before deadline):")
    for i, p in enumerate(captain_options, 1):
        news = f" — {p['news']}" if p["news"] else ""
        lines.append(
            f"  {i}. {p['web_name']} ({p['team_short']}, {p['pos']}) — "
            f"ep_next {p['ep_next']}, {fmt_fixture(p, next_fixture_info)}{news}"
        )
    lines.append("")

    # Cross-reference so a suggested OUT player is flagged right where you'd
    # see them in your lineup, not just in the separate transfers section —
    # and made explicit whether it's a "do this now" or "your call" case
    # based on the breakeven math already computed above.
    transfer_out_map = {s["out"]["id"]: s for s in transfer_suggestions}

    def transfer_annotation(player):
        s = transfer_out_map.get(player["id"])
        if not s:
            return ""
        if not s["costs_points"]:
            urgency = "do this now — free transfer, no downside to waiting"
        elif s["weekly_gain"] > 0 and (4.0 / s["weekly_gain"]) <= 2.0:
            urgency = f"do this now — breaks even in {4.0 / s['weekly_gain']:.1f} GWs, waiting only loses this week's gap for nothing"
        else:
            urgency = "your call — smaller/slower payoff, reasonable to bank the transfer instead"
        return f"  >>> SUGGESTED TRANSFER OUT: → {s['in']['web_name']} ({s['in']['team_short']}), ep_next {s['in']['ep_next']} [{urgency}]"

    lines.append("RECOMMENDED STARTING XI (fixture-adjusted ranking):")
    for p in xi:
        lines.append(f"  {p['web_name']} ({p['team_short']}, {p['pos']}) — ep_next {p['ep_next']}, {fmt_fixture(p, next_fixture_info)}")
        note = transfer_annotation(p)
        if note:
            lines.append(note)
    lines.append("")
    lines.append("BENCH (in order):")
    for p in bench:
        lines.append(f"  {p['web_name']} ({p['team_short']}, {p['pos']}) — ep_next {p['ep_next']}, {fmt_fixture(p, next_fixture_info)}")
        note = transfer_annotation(p)
        if note:
            lines.append(note)
    lines.append("")

    if transfer_suggestions:
        lines.append(f"SUGGESTED TRANSFERS (candidate fixture run averaged over next {FIXTURE_HORIZON} GWs):")
        for s in transfer_suggestions:
            o, i = s["out"], s["in"]
            avg_fdr_str = f"{s['in_avg_fdr']:.1f}" if s["in_avg_fdr"] is not None else "N/A"
            xi_tag = "STARTING XI" if s["affects_starting_xi"] else f"bench, ~{s['autosub_probability']*100:.0f}% chance of playing (auto-sub)"
            if s["costs_points"]:
                cost_tag = (
                    f" (costs -4 pts, breaks even in {4.0 / s['weekly_gain']:.1f} GWs if you keep this player that long)"
                    if s["weekly_gain"] > 0 else " (costs -4 pts — WARNING: no positive weekly gain computed, likely not worth the hit)"
                )
            else:
                cost_tag = " (free)"
            lines.append(
                f"  [{s['reason']}, {xi_tag}]{cost_tag} "
                f"OUT: {o['web_name']} ({o['team_short']}) [{o['news'] or o['status']}]  "
                f"->  IN: {i['web_name']} ({i['team_short']}) ep_next {i['ep_next']}, "
                f"avg FDR next {s['fdr_horizon']}GW: {avg_fdr_str}, "
                f"cost change {'+' if s['cost_delta']>=0 else ''}{s['cost_delta']:.1f}m "
                f"(using real sell price, not market price)"
            )
    else:
        lines.append("SUGGESTED TRANSFERS: none needed — no flagged injuries/suspensions this week.")
    lines.append("")
    lines.append("CHIP CONSIDERATIONS (based only on currently known fixtures — not season-long planning):")
    for note in chip_notes:
        lines.append(f"  {note}")
    lines.append("")
    lines.append(
        "Note: fixture difficulty uses a simple linear multiplier on FPL's own FDR scale "
        "(1=easiest, 5=hardest) applied to their ep_next projection — not a calibrated model. "
        "Treat close rankings as a nudge, not a verdict."
    )

    return "\n".join(lines)


def send_email(subject, body):
    """Returns True if an actual email was sent, False if it only printed
    to console (no credentials configured) — the caller uses this to decide
    whether to record the gameweek as 'notified', so local testing runs
    without credentials never falsely block a real future send."""
    if not (EMAIL_FROM and EMAIL_TO and EMAIL_APP_PASSWORD):
        print("Email credentials not set — printing to console instead:\n")
        print(subject)
        print(body)
        return False

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        server.send_message(msg)
    print(f"Email sent to {EMAIL_TO}")
    return True


def main():
    bootstrap = get_bootstrap()
    fixtures = get_fixtures()
    teams_by_id = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    player_index = build_player_index(bootstrap)

    current_event, next_event = get_current_and_next_event(bootstrap)
    # Two DIFFERENT things, easy to conflate (this was a real bug):
    #   - which gameweek's picks we can read your squad from (API-limited
    #     to gameweeks that have started — see get_picks_safe fallback below)
    #   - which gameweek's fixtures actually matter for the decision (always
    #     the next one with an open deadline, independent of picks source)
    decision_event = next_event or current_event
    if decision_event is None:
        raise RuntimeError("Could not determine current or next gameweek from bootstrap-static.")

    # Timing gate: only proceed if we're within the configured window before
    # THIS gameweek's real deadline (from FPL's own schedule), and we
    # haven't already sent for this gameweek. This lets the workflow run on
    # a frequent cron (e.g. every few hours) without spamming duplicate
    # emails or firing at the wrong time for gameweeks with unusual
    # deadlines (midweek fixtures, rearranged rounds, etc.).
    deadline_iso = get_deadline(bootstrap, decision_event)
    if deadline_iso is None:
        print(f"WARNING: no deadline found for GW{decision_event} in bootstrap-static; proceeding anyway.")
        remaining = None
    else:
        remaining = hours_until(deadline_iso)
        print(f"GW{decision_event} deadline: {deadline_iso} ({remaining:.1f}h from now)")

    last_notified = read_last_notified_event(STATE_PATH)
    already_sent = (last_notified == decision_event)

    in_window = FORCE_SEND or remaining is None or should_send_now(remaining, HOURS_BEFORE_DEADLINE, SEND_WINDOW_TOLERANCE_HOURS)

    if already_sent and not FORCE_SEND:
        print(f"Already sent a notification for GW{decision_event} — skipping to avoid duplicate. "
              f"(Set FPL_FORCE_SEND=1 to override.)")
        return
    if not in_window:
        print(f"Not within the send window yet (target {HOURS_BEFORE_DEADLINE}h before deadline, "
              f"±{SEND_WINDOW_TOLERANCE_HOURS}h tolerance) — skipping this run.")
        return

    entry_info = get_entry_info(TEAM_ID)
    entry_history = get_entry_history(TEAM_ID)  # used for both free-transfer count and chip availability below

    if FREE_TRANSFERS_OVERRIDE is not None and FREE_TRANSFERS_OVERRIDE != "":
        free_transfers = max(0, min(5, int(FREE_TRANSFERS_OVERRIDE)))
        print(f"Free transfers: {free_transfers} (manual override via FPL_FREE_TRANSFERS)")
    else:
        free_transfers = compute_free_transfers(entry_history, decision_event)
        print(f"Free transfers: {free_transfers} (auto-computed from your transfer history)")

    picks_source_event = decision_event
    picks_data = get_picks_safe(TEAM_ID, picks_source_event)
    picks_stale = False
    if picks_data is None and current_event is not None and current_event != picks_source_event:
        # Expected, not an error: the public API only exposes picks for a
        # gameweek once it has started, so there's no way to see your saved
        # team for a future deadline without authenticating as you. This
        # still reflects your real 15 players — it just can't show any
        # transfers you've made since the last gameweek locked in.
        picks_data = get_picks_safe(TEAM_ID, current_event)
        picks_stale = True
        picks_source_event = current_event
    if picks_data is None:
        raise RuntimeError(f"Could not fetch picks for team {TEAM_ID} for gameweek {picks_source_event}.")

    squad = squad_from_picks(picks_data, player_index)
    bank = picks_data["entry_history"]["bank"]

    transfer_history = get_transfer_history(TEAM_ID)
    purchase_prices = build_purchase_prices(squad, transfer_history)
    sell_prices = {pid: compute_sell_price(player_index[pid]["now_cost"], purchase_prices[pid]) for pid in purchase_prices}

    # Fixture/captaincy/XI analysis ALWAYS targets decision_event (the next
    # actionable gameweek) — never the picks_source_event fallback, since
    # that gameweek's fixtures are already locked and irrelevant to a
    # decision you can still act on.
    next_fixture_info = build_next_fixture_info(fixtures, decision_event, teams_by_id)
    avg_fdr = build_avg_fixture_difficulty(fixtures, decision_event, FIXTURE_HORIZON)

    captain_options = recommend_captain(squad, next_fixture_info)
    xi, bench = recommend_best_xi(squad, next_fixture_info)
    xi_ids = {p["id"] for p in xi}
    transfer_suggestions = suggest_transfers(squad, player_index, bank, sell_prices, free_transfers, avg_fdr, FIXTURE_HORIZON, xi_ids)

    available = available_chips(bootstrap, entry_history, decision_event)
    fixture_counts = get_fixture_counts_per_team(fixtures, decision_event)
    chip_notes = recommend_chips(squad, bench, captain_options, next_fixture_info, fixture_counts,
                                  transfer_suggestions, available, fixtures, decision_event, FIXTURE_HORIZON)

    body = format_email(decision_event, squad, captain_options, xi, bench, transfer_suggestions, entry_info, next_fixture_info, picks_stale, chip_notes)
    sent = send_email(f"FPL GW{decision_event} Recommendations", body)
    if sent:
        write_last_notified_event(STATE_PATH, decision_event)


if __name__ == "__main__":
    main()
