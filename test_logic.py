"""Sanity tests for the fixture-adjusted logic using mock data, since the
FPL API isn't reachable from this sandbox's network."""

from fpl_advisor import (
    recommend_best_xi, recommend_captain, flag_injury_risks, suggest_transfers,
    build_next_fixture_info, build_avg_fixture_difficulty, fixture_multiplier,
    weighted_score, build_purchase_prices, compute_sell_price,
    available_chips, get_fixture_counts_per_team, recommend_chips,
    estimate_autosub_probability, format_email,
    hours_until, should_send_now, read_last_notified_event, write_last_notified_event,
    compute_free_transfers, env_or_default,
)
from datetime import datetime, timezone, timedelta
import os

def mk(id, pos, ep, status="a", chance=None, team=1, cost=50, mult=1):
    return {
        "id": id, "pos": pos, "ep_next": ep, "status": status,
        "chance_of_playing_next_round": chance, "team": team,
        "now_cost": cost, "web_name": f"P{id}", "team_short": "TST",
        "news": "" if status == "a" else "injury", "multiplier": mult,
        "is_captain": False, "is_vice_captain": False,
    }

# ---- fixture_multiplier ----
assert fixture_multiplier(1) > fixture_multiplier(5), "easy fixture should score higher than hard"
assert fixture_multiplier(3) == 1.0, "FDR 3 should be neutral (1.0x)"
assert fixture_multiplier(None) == 1.0, "missing FDR should default to neutral"
print("fixture_multiplier: OK")

# ---- build_next_fixture_info ----
fixtures = [
    {"event": 4, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4},
    {"event": 4, "team_h": 3, "team_a": 4, "team_h_difficulty": 5, "team_a_difficulty": 1},
    {"event": 5, "team_h": 1, "team_a": 4, "team_h_difficulty": 3, "team_a_difficulty": 3},
]
teams_by_id = {1: "AAA", 2: "BBB", 3: "CCC", 4: "DDD"}
nfi = build_next_fixture_info(fixtures, 4, teams_by_id)
assert nfi[1] == {"opponent": "BBB", "venue": "H", "fdr": 2}
assert nfi[2] == {"opponent": "AAA", "venue": "A", "fdr": 4}
assert 5 not in nfi.get(5, {})  # team 5 has no event-4 fixture in this mock
print("build_next_fixture_info: OK")

# ---- build_avg_fixture_difficulty ----
avg = build_avg_fixture_difficulty(fixtures, 4, horizon=2)  # events 4,5
assert avg[1] == (2 + 3) / 2, f"expected team1 avg 2.5, got {avg[1]}"
assert avg[4] == (1 + 3) / 2, f"expected team4 avg 2.0, got {avg[4]}"
print("build_avg_fixture_difficulty: OK")

# ---- weighted_score favors easier fixture over raw ep_next ----
p_hard = mk(1, "MID", "6.0", team=2)   # faces team1 at FDR 4 (from nfi)
p_easy = mk(2, "MID", "5.5", team=1)   # faces team2 at FDR 2
assert weighted_score(p_easy, nfi) > weighted_score(p_hard, nfi), \
    "lower ep_next but much easier fixture should outrank higher ep_next with hard fixture"
print("weighted_score fixture adjustment: OK")

# ---- best XI / captain still respect formation + fixture weighting ----
squad = (
    [mk(1, "GKP", "4.0", team=1), mk(2, "GKP", "1.0", team=3)]
    + [mk(i, "DEF", str(6 - i*0.3), team=1) for i in range(3, 8)]
    + [mk(i, "MID", str(7 - i*0.2), team=2) for i in range(8, 13)]
    + [mk(i, "FWD", str(5 - i*0.1), status="i" if i == 13 else "a", team=4) for i in range(13, 16)]
)
xi, bench = recommend_best_xi(squad, nfi)
assert len(xi) == 11 and len(bench) == 4
gkp = sum(1 for p in xi if p["pos"] == "GKP")
defn = sum(1 for p in xi if p["pos"] == "DEF")
mid = sum(1 for p in xi if p["pos"] == "MID")
fwd = sum(1 for p in xi if p["pos"] == "FWD")
assert gkp == 1 and 3 <= defn <= 5 and 2 <= mid <= 5 and 1 <= fwd <= 3
print(f"recommend_best_xi formation: GKP {gkp} DEF {defn} MID {mid} FWD {fwd} — OK")

captains = recommend_captain(squad, nfi)
assert len(captains) <= 3
print(f"recommend_captain top pick: P{captains[0]['id']} — OK")

risks = flag_injury_risks(squad)
assert any(p["id"] == 13 for p in risks)
print("flag_injury_risks: OK")

player_index = {p["id"]: p for p in squad}
player_index[99] = mk(99, "FWD", "6.0", team=1, cost=50)   # easy fixture (team1, avg fdr from avg dict)
player_index[98] = mk(98, "FWD", "7.0", team=3, cost=50)   # no fixture data -> neutral multiplier
avg_fdr_map = build_avg_fixture_difficulty(fixtures, 4, horizon=2)
sell_prices_mock = {p["id"]: p["now_cost"] for p in squad}  # no price movement in this mock
xi_ids_mock = {p["id"] for p in squad if p["multiplier"] > 0}
transfers = suggest_transfers(squad, player_index, bank=5, sell_prices=sell_prices_mock, free_transfers=1, avg_fdr=avg_fdr_map, fdr_horizon=2, xi_ids=xi_ids_mock)
injury_suggestions = [t for t in transfers if t["reason"] == "injury/doubt"]
assert len(injury_suggestions) == 1
assert injury_suggestions[0]["in"]["id"] == 98
print(f"suggest_transfers (injury path): OUT P{injury_suggestions[0]['out']['id']} -> IN P{injury_suggestions[0]['in']['id']} (avg FDR {injury_suggestions[0]['in_avg_fdr']}) — OK")

# ---- sell price: profit is halved, loss is not penalized ----
assert compute_sell_price(now_cost=70, purchase_price=50) == 60, "profit of 20 should yield sell price of purchase+10"
assert compute_sell_price(now_cost=45, purchase_price=50) == 45, "a price drop should sell at current price, no extra penalty"
assert compute_sell_price(now_cost=50, purchase_price=50) == 50, "no price change should sell at cost"
print("compute_sell_price (profit halved, loss not penalized): OK")

# ---- build_purchase_prices: transfer history overrides season-start approximation ----
squad_for_prices = [mk(201, "MID", "5.0", cost=60), mk(202, "MID", "5.0", cost=55)]
squad_for_prices[0]["cost_change_start"] = 10  # id 201: no transfer record -> approx purchase = 60-10=50
squad_for_prices[1]["cost_change_start"] = 5   # id 202: HAS a transfer record below, which should win instead
transfer_history_mock = [
    {"element_in": 202, "element_in_cost": 52, "event": 3},
    {"element_in": 202, "element_in_cost": 48, "event": 1},  # older buy of the same player (re-bought) — most recent (event 3) should win
]
prices = build_purchase_prices(squad_for_prices, transfer_history_mock)
assert prices[201] == 50, f"expected season-start approx 50 for untransferred player, got {prices[201]}"
assert prices[202] == 52, f"expected most recent transfer-in cost 52, got {prices[202]}"
print("build_purchase_prices (transfer history overrides approximation, most recent wins): OK")

# ---- value upgrade: a FIT player (no injury flag) still gets suggested when
# a clearly better fixture-adjusted alternative exists — this is the new
# capability that used to be entirely missing ----
fit_but_mediocre = mk(301, "MID", "3.0", status="a", team=3, cost=50)  # fit, low ep_next, team3 -> neutral-ish fixture
squad_with_value_gap = squad_for_prices + [fit_but_mediocre]
player_index_v2 = {p["id"]: p for p in squad_with_value_gap}
player_index_v2[401] = mk(401, "MID", "9.0", team=1, cost=50)  # much better, team1 -> easy fixture per nfi/avg_fdr_map
sell_prices_v2 = {p["id"]: p["now_cost"] for p in squad_with_value_gap}
xi_ids_v2 = {p["id"] for p in squad_with_value_gap if p["multiplier"] > 0}
transfers_v2 = suggest_transfers(squad_with_value_gap, player_index_v2, bank=5, sell_prices=sell_prices_v2, free_transfers=1, avg_fdr=avg_fdr_map, fdr_horizon=2, xi_ids=xi_ids_v2)
value_suggestions = [t for t in transfers_v2 if t["reason"] == "value upgrade"]
assert len(value_suggestions) >= 1, "expected a value-upgrade suggestion for a fit-but-mediocre player with a much better alternative available"
assert value_suggestions[0]["out"]["id"] == 301
print(f"value-upgrade suggestion (no injury needed): OUT P{value_suggestions[0]['out']['id']} -> IN P{value_suggestions[0]['in']['id']} — OK")

# ---- regression test: the EXACT bug reported — two different OUT players
# both have the same best-fit replacement; must not suggest buying the same
# player twice, and the second suggestion should fall through to the
# next-best available candidate instead ----
out_a = mk(501, "DEF", "3.0", team=5, cost=45)  # weak, team5 -> should trigger value upgrade
out_b = mk(502, "DEF", "2.5", team=5, cost=45)  # also weak, same team, same position
squad_dup_test = [out_a, out_b]
shared_best = mk(601, "DEF", "8.0", team=1, cost=45)   # the single best replacement both would want
second_best = mk(602, "DEF", "7.5", team=1, cost=45)   # next-best, should be used for whichever OUT loses the tie
player_index_dup = {p["id"]: p for p in squad_dup_test}
player_index_dup[601] = shared_best
player_index_dup[602] = second_best
sell_prices_dup = {p["id"]: p["now_cost"] for p in squad_dup_test}
xi_ids_dup = {501, 502}  # both starters, so this also exercises the starter-priority path
avg_fdr_dup = {1: 1.0, 5: 5.0}  # team1 easy, team5 hard -> guarantees both outs see team1 replacements as better
transfers_dup = suggest_transfers(squad_dup_test, player_index_dup, bank=5, sell_prices=sell_prices_dup, free_transfers=2, avg_fdr=avg_fdr_dup, fdr_horizon=1, xi_ids=xi_ids_dup)
in_ids = [t["in"]["id"] for t in transfers_dup]
assert len(in_ids) == len(set(in_ids)), f"duplicate incoming player suggested across different OUT slots: {in_ids}"
assert set(in_ids) == {601, 602}, f"expected both distinct replacements to be used (601 and 602), got {in_ids}"
print(f"suggest_transfers never recommends the same incoming player twice: OK (used {in_ids})")

# ---- estimate_autosub_probability: fit starter -> low baseline risk;
# doubtful starter -> proportionally higher probability ----
fit_starter = mk(901, "FWD", "5.0", team=1, chance=None, mult=1)     # chance unknown -> assumed fit
doubtful_starter = mk(902, "FWD", "5.0", team=1, chance=20, mult=1)  # 20% chance of playing = very doubtful
bench_fwd = mk(903, "FWD", "3.0", team=1, mult=0)

prob_vs_fit = estimate_autosub_probability(bench_fwd, [fit_starter, bench_fwd])
prob_vs_doubtful = estimate_autosub_probability(bench_fwd, [doubtful_starter, bench_fwd])
assert prob_vs_fit == 0.02, f"expected baseline 2% risk against a fit starter, got {prob_vs_fit}"
assert abs(prob_vs_doubtful - 0.80) < 1e-9, f"expected 80% (100-20)/100 against a doubtful starter, got {prob_vs_doubtful}"
print(f"estimate_autosub_probability (fit={prob_vs_fit}, doubtful={prob_vs_doubtful}): OK")

# ---- regression test: a bench upgrade behind a FULLY FIT starter must be
# SUPPRESSED entirely (not just ranked lower) — this is the actual fix for
# the reported concern that bench transfers don't affect score unless
# there's real auto-sub risk ----
bench_weak = mk(701, "FWD", "2.0", team=5, cost=45, mult=0, chance=None)
starter_fit = mk(702, "FWD", "2.5", team=5, cost=45, mult=1, chance=None)  # fit -> bench has ~2% autosub chance
squad_fit_starter = [bench_weak, starter_fit]
much_better_fwd = mk(801, "FWD", "8.0", team=1, cost=45)  # huge raw upgrade, but bench's real EV gain is tiny
player_index_fit = {p["id"]: p for p in squad_fit_starter}
player_index_fit[801] = much_better_fwd
sell_prices_fit = {p["id"]: p["now_cost"] for p in squad_fit_starter}
xi_ids_fit = {702}
transfers_fit = suggest_transfers(squad_fit_starter, player_index_fit, bank=5, sell_prices=sell_prices_fit, free_transfers=2, avg_fdr=avg_fdr_dup, fdr_horizon=1, xi_ids=xi_ids_fit)
bench_suggestions_fit = [t for t in transfers_fit if t["out"]["id"] == 701]
assert len(bench_suggestions_fit) == 0, (
    f"a huge ep_next upgrade for a bench player behind a FIT starter should be suppressed "
    f"(negligible real-world EV), but got: {bench_suggestions_fit}"
)
print("bench upgrade behind a fit starter is correctly suppressed (not just deprioritized): OK")

# ---- same scenario, but the starter is now genuinely doubtful — the SAME
# bench upgrade should now surface, discounted by the real autosub odds ----
bench_weak2 = mk(701, "FWD", "2.0", team=5, cost=45, mult=0, chance=None)
starter_doubtful = mk(702, "FWD", "2.5", team=5, cost=45, mult=1, chance=20)  # doubtful -> 80% autosub odds
squad_doubtful_starter = [bench_weak2, starter_doubtful]
player_index_doubt = {p["id"]: p for p in squad_doubtful_starter}
player_index_doubt[801] = mk(801, "FWD", "8.0", team=1, cost=45)
player_index_doubt[802] = mk(802, "FWD", "7.5", team=1, cost=45)  # so starter_doubtful's own upgrade has a distinct candidate
sell_prices_doubt = {p["id"]: p["now_cost"] for p in squad_doubtful_starter}
xi_ids_doubt = {702}
transfers_doubt = suggest_transfers(squad_doubtful_starter, player_index_doubt, bank=5, sell_prices=sell_prices_doubt, free_transfers=2, avg_fdr=avg_fdr_dup, fdr_horizon=1, xi_ids=xi_ids_doubt)
bench_suggestions_doubt = [t for t in transfers_doubt if t["out"]["id"] == 701]
assert len(bench_suggestions_doubt) == 1, f"expected the bench upgrade to surface once the starter is doubtful, got: {bench_suggestions_doubt}"
assert abs(bench_suggestions_doubt[0]["autosub_probability"] - 0.80) < 1e-9
print(f"bench upgrade correctly surfaces once starter is doubtful, tagged with real odds ({bench_suggestions_doubt[0]['autosub_probability']*100:.0f}%): OK")

# ---- regression test: fixture lookup must work for an in-progress/current
# gameweek (event 3), not just future ones — this was the actual bug ----
fixtures_with_started = [
    {"event": 3, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4},
    {"event": 4, "team_h": 1, "team_a": 3, "team_h_difficulty": 3, "team_a_difficulty": 3},
]
nfi_current_gw = build_next_fixture_info(fixtures_with_started, 3, teams_by_id)
assert 1 in nfi_current_gw and nfi_current_gw[1]["fdr"] == 2, \
    "fixture lookup must still find fixtures for the current/in-progress gameweek, not just future ones"
print("fixture lookup works for current (already-started) gameweek: OK")

# ---- regression test: fixture analysis must use the DECISION gameweek
# (next actionable one), not whichever gameweek picks happened to be
# sourced from — this was the actual second bug (picks fell back to GW3,
# and fixture analysis wrongly followed it instead of staying on GW4) ----
fixtures_multi_gw = [
    {"event": 3, "team_h": 1, "team_a": 2, "team_h_difficulty": 5, "team_a_difficulty": 1},  # GW3: team1 has a HARD fixture
    {"event": 4, "team_h": 1, "team_a": 3, "team_h_difficulty": 1, "team_a_difficulty": 5},  # GW4: team1 has an EASY fixture
]
decision_event = 4  # the actionable gameweek, even if picks were sourced from GW3
nfi_decision = build_next_fixture_info(fixtures_multi_gw, decision_event, teams_by_id)
assert nfi_decision[1]["fdr"] == 1, (
    "fixture info must reflect the DECISION gameweek (GW4, easy fixture) "
    f"even when squad data was sourced from an earlier gameweek — got FDR {nfi_decision[1]['fdr']}"
)
print("fixture analysis correctly follows decision gameweek, not picks-source gameweek: OK")

# ---- available_chips: wildcard1 vs wildcard2 windows are independent ----
mock_bootstrap_chips = {
    "chips": [
        {"id": 1, "name": "wildcard", "number": 1, "start_event": 2, "stop_event": 19},
        {"id": 2, "name": "wildcard", "number": 2, "start_event": 20, "stop_event": 38},
        {"id": 4, "name": "bboost", "number": 1, "start_event": 1, "stop_event": 19},
        {"id": 5, "name": "3xc", "number": 1, "start_event": 1, "stop_event": 19},
    ]
}
entry_history_used_wc1 = {"chips": [{"name": "wildcard", "event": 8}]}  # used wildcard in GW8 (window 1)
avail_gw10 = available_chips(mock_bootstrap_chips, entry_history_used_wc1, 10)
avail_gw25 = available_chips(mock_bootstrap_chips, entry_history_used_wc1, 25)
assert "wildcard" not in avail_gw10, "wildcard used in window 1 should be unavailable within window 1"
assert "wildcard" in avail_gw25, "wildcard usage in window 1 should NOT block window 2 (separate chip)"
print("available_chips (wildcard1/wildcard2 windows independent): OK")

# ---- get_fixture_counts_per_team: detects doubles and blanks ----
fixtures_dgw = [
    {"event": 6, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 3},
    {"event": 6, "team_h": 1, "team_a": 3, "team_h_difficulty": 3, "team_a_difficulty": 2},  # team1 plays twice = DGW
    # team4 has no fixture in event 6 at all = blank
]
counts = get_fixture_counts_per_team(fixtures_dgw, 6)
assert counts[1] == 2, f"expected team1 to have 2 fixtures (double), got {counts.get(1)}"
assert counts.get(4, 0) == 0, "team4 should have 0 fixtures (blank) in event 6"
print("get_fixture_counts_per_team (detects doubles and blanks): OK")

# ---- recommend_chips: triple captain flagged when top captain has a double ----
squad_chip_test = [mk(1, "GKP", "4.0", team=1)] + [mk(i, "MID", "5.0", team=1) for i in range(2, 16)]
captain_opts = [squad_chip_test[1]]  # top pick, team=1, which has a double in fixtures_dgw event 6
chip_notes = recommend_chips(
    squad_chip_test, [], captain_opts, {}, counts, [],
    available_chip_names={"3xc", "bboost", "freehit", "wildcard"},
    fixtures=fixtures_dgw, decision_event=6, fdr_horizon=1,
)
assert any("TRIPLE CAPTAIN" in n and "double gameweek" in n for n in chip_notes), \
    f"expected a triple captain double-gameweek note, got: {chip_notes}"
print("recommend_chips (triple captain flags a double gameweek): OK")

# ---- recommend_chips: a chip already used this window is reported as such, not evaluated ----
chip_notes_no_wc = recommend_chips(
    squad_chip_test, [], captain_opts, {}, counts, [],
    available_chip_names={"3xc", "bboost", "freehit"},  # wildcard deliberately excluded (already used)
    fixtures=fixtures_dgw, decision_event=6, fdr_horizon=1,
)
assert any("WILDCARD" in n and "already used" in n for n in chip_notes_no_wc)
print("recommend_chips (already-used chip correctly reported, not evaluated): OK")

# ---- weekly_gain / breakeven: a costs-points suggestion's weekly_gain
# should let you compute a sane "weeks to break even on the -4 hit" figure ----
out_low = mk(1001, "MID", "2.0", team=5, cost=50)   # weak, hard fixture (team5, avg_fdr_dup=5.0)
in_high = mk(1002, "MID", "8.0", team=1, cost=50)   # strong, easy fixture (team1, avg_fdr_dup=1.0)
squad_breakeven = [out_low]
player_index_be = {out_low["id"]: out_low, in_high["id"]: in_high}
sell_prices_be = {out_low["id"]: out_low["now_cost"]}
xi_ids_be = {out_low["id"]}
transfers_be = suggest_transfers(squad_breakeven, player_index_be, bank=5, sell_prices=sell_prices_be, free_transfers=0, avg_fdr=avg_fdr_dup, fdr_horizon=1, xi_ids=xi_ids_be)
assert len(transfers_be) == 1
weekly_gain = transfers_be[0]["weekly_gain"]
assert weekly_gain > 0, f"expected a positive weekly gain for a clear upgrade, got {weekly_gain}"
breakeven_weeks = 4.0 / weekly_gain
assert breakeven_weeks > 0
print(f"weekly_gain/breakeven computable: gain={weekly_gain:.2f} pts/GW, breaks even in {breakeven_weeks:.1f} GWs — OK")

# ---- format_email: suggested transfers must be cross-referenced directly
# on the XI/bench listing, with urgency wording that matches the breakeven
# math (free or fast-breakeven -> "do this now"; slow -> "your call") ----
def mk_email_player(id, name, pos, ep, team_short="TST"):
    return {"id": id, "web_name": name, "pos": pos, "ep_next": ep, "team_short": team_short,
            "news": "", "status": "a", "team": 1}

email_xi = [mk_email_player(1, "Truffert", "DEF", "1.7"), mk_email_player(2, "Haaland", "FWD", "8.0")]
email_bench = [mk_email_player(3, "James", "DEF", "0.3")]
email_captains = [email_xi[1]]
email_entry = {"name": "Test FC"}

s_free = {
    "out": email_xi[0], "in": mk_email_player(99, "Ajayi", "DEF", "8.3", "HUL"),
    "costs_points": False, "weekly_gain": 5.0, "cost_delta": -1.4,
    "in_avg_fdr": 3.3, "fdr_horizon": 3, "reason": "value upgrade", "affects_starting_xi": True,
}
body = format_email(4, email_xi + email_bench, email_captains, email_xi, email_bench, [s_free], email_entry, {}, False, [])
assert ">>> SUGGESTED TRANSFER OUT: → Ajayi" in body and "do this now — free transfer" in body
print("format_email: free-transfer XI annotation shows 'do this now': OK")

s_fast = dict(s_free, costs_points=True, weekly_gain=5.0)
body2 = format_email(4, email_xi + email_bench, email_captains, email_xi, email_bench, [s_fast], email_entry, {}, False, [])
assert "breaks even in 0.8 GWs" in body2 and "do this now" in body2
print("format_email: fast-breakeven costs-points annotation shows 'do this now': OK")

s_slow = dict(s_free, costs_points=True, weekly_gain=0.5)
body3 = format_email(4, email_xi + email_bench, email_captains, email_xi, email_bench, [s_slow], email_entry, {}, False, [])
assert "your call" in body3
print("format_email: slow-breakeven costs-points annotation shows 'your call': OK")

body4 = format_email(4, email_xi + email_bench, email_captains, email_xi, email_bench, [], email_entry, {}, False, [])
assert ">>> SUGGESTED TRANSFER" not in body4
print("format_email: no annotation when there are no suggestions: OK")

# ---- hours_until: exact deadline math ----
future_iso = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
h = hours_until(future_iso)
assert 23.9 < h < 24.1, f"expected ~24h, got {h}"
print(f"hours_until: OK ({h:.2f}h)")

# ---- should_send_now: within/outside tolerance window ----
assert should_send_now(24.0, target_hours_before=24, tolerance_hours=3) is True
assert should_send_now(21.5, target_hours_before=24, tolerance_hours=3) is True   # edge of window
assert should_send_now(26.5, target_hours_before=24, tolerance_hours=3) is True   # other edge
assert should_send_now(15.0, target_hours_before=24, tolerance_hours=3) is False  # too early
assert should_send_now(2.0, target_hours_before=24, tolerance_hours=3) is False   # too late (deadline almost here)
print("should_send_now window logic: OK")

# ---- state file: read/write round-trip, and missing-file default ----
test_state_path = "/tmp/test_fpl_state.json"
if os.path.exists(test_state_path):
    os.remove(test_state_path)
assert read_last_notified_event(test_state_path) is None, "missing state file should return None, not error"
write_last_notified_event(test_state_path, 4)
assert read_last_notified_event(test_state_path) == 4
write_last_notified_event(test_state_path, 5)
assert read_last_notified_event(test_state_path) == 5, "writing again should overwrite, not append"
os.remove(test_state_path)
print("state file read/write round-trip: OK")

# ---- compute_free_transfers: exact FPL banking rules simulated from history ----
h1 = {"current": [{"event": 1, "event_transfers": 0}], "chips": []}
assert compute_free_transfers(h1, 2) == 1
print("compute_free_transfers (baseline, GW2=1): OK")

h2 = {"current": [{"event": 1, "event_transfers": 0}, {"event": 2, "event_transfers": 0}], "chips": []}
assert compute_free_transfers(h2, 3) == 2
print("compute_free_transfers (unused transfer banks to 2): OK")

h3 = {"current": [
    {"event": 1, "event_transfers": 0}, {"event": 2, "event_transfers": 0}, {"event": 3, "event_transfers": 1},
], "chips": []}
assert compute_free_transfers(h3, 4) == 2
print("compute_free_transfers (use exactly available -> steady state): OK")

h4 = {"current": [
    {"event": 1, "event_transfers": 0}, {"event": 2, "event_transfers": 0},
    {"event": 3, "event_transfers": 1}, {"event": 4, "event_transfers": 3},
], "chips": []}
assert compute_free_transfers(h4, 5) == 1
print("compute_free_transfers (paid hit doesn't go negative): OK")

h5 = {"current": [{"event": i, "event_transfers": 0} for i in range(1, 6)] + [{"event": 6, "event_transfers": 11}],
      "chips": [{"name": "wildcard", "event": 6}]}
assert compute_free_transfers(h5, 6) == 5
assert compute_free_transfers(h5, 7) == 5
print("compute_free_transfers (wildcard preserves bank, unaffected by transfer count): OK")

h6 = {"current": [{"event": i, "event_transfers": 0} for i in range(1, 8)], "chips": []}
assert compute_free_transfers(h6, 8) == 5
print("compute_free_transfers (caps at 5, never exceeds): OK")

# ---- env_or_default: the exact bug reported — a GitHub Actions secret
# that's unset comes through as an empty string, not a missing key, which
# broke os.environ.get(key, default)'s normal fallback ----
import os as _os
assert env_or_default("FPL_TEST_UNSET_VAR_XYZ", "fallback") == "fallback", "truly missing key should use default"
_os.environ["FPL_TEST_EMPTY_VAR_XYZ"] = ""
assert env_or_default("FPL_TEST_EMPTY_VAR_XYZ", "fallback") == "fallback", "empty-string env var must ALSO fall back to default (this was the bug)"
_os.environ["FPL_TEST_REAL_VAR_XYZ"] = "actual_value"
assert env_or_default("FPL_TEST_REAL_VAR_XYZ", "fallback") == "actual_value", "a real value must still override the default"
del _os.environ["FPL_TEST_EMPTY_VAR_XYZ"]
del _os.environ["FPL_TEST_REAL_VAR_XYZ"]
print("env_or_default (empty-string secrets fall back correctly, real values still override): OK")

print("\nALL TESTS PASSED")
