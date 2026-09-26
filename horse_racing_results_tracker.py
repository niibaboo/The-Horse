#!/usr/bin/env python3
"""
Results Tracker -- UK/Ireland Horse Racing
--------------------------------------------------------------------------
Same pattern as the results trackers built for Match IQ, Strike Zone, Blitz
IQ, Blue Line, and Euro Ice: log every qualifying pick as "pending", check
pending picks once their race has actually run, mark hit/miss against the
REAL result, and rebuild a dashboard showing genuine track record -- not
anecdote, not screenshots.

NOTE ON FILENAME: this file is named horse_racing_results_tracker.py here
only because it sits alongside Match IQ's own results_tracker.py in this
shared output folder. The-Horse is its OWN standalone repo (not the shared
SportsIQ repo), so once uploaded there it should be renamed to plain
results_tracker.py -- horse_racing.py's `import results_tracker` line at
the bottom of its __main__ block expects that exact module name.

WHAT GETS LOGGED, one pick per category per race per day:
  - Market Favorite   -- the top de-vigged win-probability horse (win pick)
  - Recent Form       -- the horse ranked #1 by recency-weighted form (win pick)
  - Form/Market Gap   -- horses flagged as form-ahead-of-market (win pick)
  - Each-Way          -- the strongest PLACE-probability horse in the race
                         (place pick, graded against that race's own place
                         count -- top 2/3/4 depending on field size)
  - Projected Order    -- the #1 horse in the pure-market projected order
                         (win pick -- same horse as Market Favorite by
                         construction, logged separately so its category
                         label matches what's shown on the race card)
  - Form-Adjusted Order -- the #1 horse in the EXPERIMENTAL form-blended
                         order (win pick). THIS is the category that
                         actually answers the open question from this
                         project: does blending in recent form pick more
                         winners than the market-only projection, or fewer?
                         Compare its win-rate against Market Favorite's/
                         Projected Order's over time -- that comparison is
                         the entire point of tracking this category.

HOW RESULTS ARE FETCHED: horse-racing.p.rapidapi.com's per-horse "position"
field on GET /race/{id_race} is empty before the race runs and (assumed,
not yet confirmed against a real post-race response at the time this was
built) gets filled in with the finishing position once it has. A race is
treated as finished once AT LEAST ONE non-non-runner horse in it has a
non-empty position -- at that point every pending pick for that race gets
graded, even if a specific horse's own position field is still blank (that
horse is then scored a miss, not left pending forever). If this assumption
about the field turns out to be wrong on a real finished race, pending
entries will just accumulate and never verify -- watch the dashboard's
"still pending" count; if it only grows, this is the first thing to check
against a real completed race's raw API response.

Usage (called automatically from horse_racing.py's main block):
    import results_tracker
    results_tracker.run_results_tracker(predictions)

Output:
    docs/horse-racing/results/log.json
    docs/horse-racing/results/index.html
"""

import os
import json
from datetime import datetime, timedelta

import horse_racing  # reuses get_race_detail() -- same RAPIDAPI_KEY/pacing/session

LOG_PATH = "docs/horse-racing/results/log.json"
DASH_PATH = "docs/horse-racing/results/index.html"

# Only try to verify a race once its scheduled off-time is at least this
# far in the past -- guards against checking a race that's still running
# or has only just finished, where the API may not have posted results
# yet. Doesn't guarantee the race HAS finished (off-times can be delayed),
# just avoids wasting API calls checking races that obviously haven't.
VERIFY_DELAY_MINUTES = 45

# Caps how many DISTINCT races get a fresh API call in one run, so a big
# backlog of pending picks can't blow through the free-tier rate limit in
# a single execution. Each race needs only one call regardless of how many
# picks are pending for it (all its pending picks are graded together).
MAX_RACES_PER_RUN = 60


def _load_log(path=LOG_PATH):
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  [!] couldn't read existing log ({e}) -- starting a fresh one")
        return []


def _save_log(entries, path=LOG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(entries, f, indent=2, default=str)


def _make_id(category, id_race, id_horse):
    return f"{category}::{id_race}::{id_horse}"


def log_todays_signals(predictions, path=LOG_PATH):
    """Appends one pending entry per pick per category, for every race in
    today's (or whatever date was just built) predictions. Skips a pick
    that's already logged under the same category+race+horse, so re-running
    the same day's build (e.g. a manual re-trigger) never duplicates rows."""
    entries = _load_log(path)
    existing_ids = {e["id"] for e in entries}
    new_count = 0

    def _add(category, id_race, course, race_time, title, id_horse, horse_name, detail, extra=None):
        nonlocal new_count
        if id_horse is None:
            return  # can't grade a pick with no horse id to look up later
        eid = _make_id(category, id_race, id_horse)
        if eid in existing_ids:
            return
        entry = {
            "id": eid, "category": category, "id_race": id_race, "course": course,
            "race_time": race_time, "title": title, "id_horse": id_horse, "horse": horse_name,
            "detail": detail, "logged_at": datetime.now().isoformat(timespec="seconds"),
            "status": "pending", "actual_position": None,
        }
        if extra:
            entry.update(extra)
        entries.append(entry)
        existing_ids.add(eid)
        new_count += 1

    for p in predictions:
        id_race, course, race_time, title = p["id_race"], p["course"], p["date"], p["title"]

        with_prob = [r for r in p["runners"] if r.get("market_prob") is not None]
        if with_prob:
            fav = max(with_prob, key=lambda r: r["market_prob"])
            _add("Market Favorite", id_race, course, race_time, title,
                 fav.get("id_horse"), fav["name"], f"win prob {fav['market_prob']*100:.0f}%")

        for r in p["runners"]:
            if r.get("form_rank") == 1:
                _add("Recent Form", id_race, course, race_time, title,
                     r.get("id_horse"), r["name"], f"form {r.get('form_raw')}")

        for r in p["runners"]:
            fr, mr = r.get("form_rank"), r.get("market_rank")
            if fr is not None and mr is not None and fr <= 2 and mr - fr >= 2:
                _add("Form/Market Gap", id_race, course, race_time, title,
                     r.get("id_horse"), r["name"], f"form #{fr} vs market #{mr}")

        if p.get("place_count"):
            with_place = [r for r in p["runners"] if r.get("place_prob") is not None]
            if with_place:
                best_ew = max(with_place, key=lambda r: r["place_prob"])
                _add("Each-Way", id_race, course, race_time, title,
                     best_ew.get("id_horse"), best_ew["name"],
                     f"place prob {best_ew['place_prob']*100:.0f}% · top {p['place_count']}",
                     extra={"place_count": p["place_count"]})

        proj = p.get("projected_order") or []
        if proj:
            top = proj[0]
            _add("Projected Order", id_race, course, race_time, title,
                 top.get("id_horse"), top["name"], f"win prob {top['market_prob']*100:.0f}%")

        fa = p.get("form_adjusted_order") or []
        if fa:
            top = fa[0]
            mp = top.get("market_prob")
            _add("Form-Adjusted Order", id_race, course, race_time, title,
                 top.get("id_horse"), top["name"],
                 f"blended score {top.get('blended_score')}" +
                 (f" · win prob {mp*100:.0f}%" if mp is not None else ""))

    _save_log(entries, path)
    print(f"  Results tracker: logged {new_count} new pick(s), {len(entries)} total on file.")
    return entries


def _parse_position(pos_str):
    """'3' -> 3. Empty, non-numeric (DNF codes like PU/F/UR), or missing
    -> None. None here means "can't be graded as a specific finishing
    position" -- the caller decides whether that means "still pending" or
    "graded as a miss" depending on whether the REST of the race's horses
    show the race is actually finished."""
    if not pos_str:
        return None
    pos_str = str(pos_str).strip()
    if pos_str.isdigit():
        return int(pos_str)
    return None


def _race_is_gradeable(race_time_str):
    try:
        race_dt = datetime.strptime(race_time_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    return datetime.now() >= race_dt + timedelta(minutes=VERIFY_DELAY_MINUTES)


def verify_pending_results(path=LOG_PATH, max_races=MAX_RACES_PER_RUN):
    """Groups pending entries by race (one API call per race, however many
    picks are pending for it), fetches the real race detail, and grades
    every pending entry for that race once at least one horse in it shows
    a real finishing position (see module docstring on why that's the
    "race is finished" signal used here, and its one open assumption)."""
    entries = _load_log(path)
    pending = [e for e in entries if e["status"] == "pending"]
    if not pending:
        print("  Results tracker: no pending picks to verify.")
        return entries

    races_to_check = {}
    for e in pending:
        if _race_is_gradeable(e.get("race_time")):
            races_to_check.setdefault(e["id_race"], []).append(e)

    if not races_to_check:
        print(f"  Results tracker: {len(pending)} pending, none old enough to check yet.")
        return entries

    checked, graded, still_no_data = 0, 0, 0
    for id_race, race_entries in list(races_to_check.items())[:max_races]:
        detail = horse_racing.get_race_detail(id_race)
        checked += 1
        if not detail:
            continue
        horses = detail.get("horses") or []
        positions_by_horse = {h.get("id_horse"): (h.get("position") or "").strip() for h in horses}
        any_populated = any(v for v in positions_by_horse.values())
        if not any_populated:
            still_no_data += 1
            continue  # race probably hasn't actually finished yet -- try again next run

        for e in race_entries:
            pos_str = positions_by_horse.get(e["id_horse"], "")
            pos_int = _parse_position(pos_str)
            e["actual_position"] = pos_int if pos_int is not None else (pos_str or "DNF/unknown")
            if e["category"] == "Each-Way":
                hit = pos_int is not None and pos_int <= e.get("place_count", 0)
            else:
                hit = pos_int == 1
            e["status"] = "hit" if hit else "miss"
            e["verified_at"] = datetime.now().isoformat(timespec="seconds")
            graded += 1

    _save_log(entries, path)
    print(f"  Results tracker: checked {checked} race(s), graded {graded} pick(s), "
          f"{still_no_data} race(s) still show no result data.")
    return entries


CATEGORY_ORDER = ["Market Favorite", "Projected Order", "Form-Adjusted Order",
                   "Recent Form", "Form/Market Gap", "Each-Way"]

CATEGORY_NOTE = {
    "Market Favorite": "win pick · real de-vigged market data",
    "Projected Order": "win pick · same basis as Market Favorite",
    "Form-Adjusted Order": "win pick · EXPERIMENTAL blend, unvalidated weight",
    "Recent Form": "win pick · raw form screen, not a probability",
    "Form/Market Gap": "win pick · discrepancy screen, not validated",
    "Each-Way": "PLACE pick · graded against that race's own place count",
}


def build_results_dashboard(path=LOG_PATH, out_path=DASH_PATH):
    entries = _load_log(path)
    graded = [e for e in entries if e["status"] in ("hit", "miss")]
    pending = [e for e in entries if e["status"] == "pending"]

    by_cat = {}
    for e in graded:
        c = by_cat.setdefault(e["category"], {"hits": 0, "total": 0})
        c["total"] += 1
        if e["status"] == "hit":
            c["hits"] += 1

    overall_hits = sum(c["hits"] for c in by_cat.values())
    overall_total = sum(c["total"] for c in by_cat.values())
    overall_pct = round(overall_hits / overall_total * 100) if overall_total else None

    def _pct(c):
        return round(c["hits"] / c["total"] * 100) if c["total"] else None

    cat_rows = ""
    for cat in CATEGORY_ORDER:
        c = by_cat.get(cat)
        if not c or not c["total"]:
            continue
        pct = _pct(c)
        cat_rows += f"""<div style="display:flex;justify-content:space-between;padding:8px 0;border-top:1px solid #3a2a20">
          <div><b>{cat}</b><br><span style="color:#998;font-size:11px">{CATEGORY_NOTE.get(cat, "")}</span></div>
          <div style="text-align:right"><span style="color:{'#7dd3a8' if pct is not None and pct >= 50 else '#ff9a2e'};font-weight:bold;font-size:16px">{pct if pct is not None else '-'}%</span><br><span style="color:#998;font-size:11px">{c['hits']}/{c['total']}</span></div>
        </div>"""

    recent = sorted(graded, key=lambda e: e.get("verified_at", ""), reverse=True)[:20]
    recent_rows = ""
    for e in recent:
        color = "#7dd3a8" if e["status"] == "hit" else "#ff5a5a"
        recent_rows += f"""<div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-top:1px solid #3a2a20">
          <div><b>{e['horse']}</b><br><span style="color:#998">{e['course']} · {e['category']}</span></div>
          <div style="text-align:right"><span style="color:{color};font-weight:bold">{e['status'].upper()}</span><br><span style="color:#998">finished {e.get('actual_position', '?')}</span></div>
        </div>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Horse Racing — Results Tracker</title></head>
<body style="background:#0f0a08;color:#e8dcd0;font-family:Arial;padding:12px;max-width:600px;margin:auto">
<h2 style="text-align:center;color:#7dd3a8">📋 Results Tracker</h2>
<p style="text-align:center;color:#998;font-size:11px">Every pick logged before its race, graded against the real result · generated {datetime.now().strftime('%d %b %H:%M')}</p>

<div style="background:#14261a;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #2a4a34;text-align:center">
  <div style="font-size:12px;color:#998">Overall (all categories combined)</div>
  <div style="font-size:36px;font-weight:800;color:#7dd3a8">{overall_pct if overall_pct is not None else '-'}%</div>
  <div style="font-size:12px;color:#998">{overall_hits}/{overall_total} graded · {len(pending)} still pending</div>
</div>

<div style="background:#1a1310;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #3a2a20">
  <div style="font-size:14px;font-weight:bold;margin-bottom:6px">By Category</div>
  <div style="font-size:11px;color:#998;margin-bottom:6px">
    Market Favorite and Projected Order are win-graded from the same real market data — expect them to
    track closely. The one worth watching is Form-Adjusted Order vs. those two: if it starts beating them,
    that's the first real evidence the form blend helps; if it lags, the market-only order stays the better bet.
  </div>
  {cat_rows or '<p style="color:#998;text-align:center">No graded picks yet.</p>'}
</div>

<div style="background:#1a1310;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #3a2a20">
  <div style="font-size:14px;font-weight:bold;margin-bottom:6px">Recent Results</div>
  {recent_rows or '<p style="color:#998;text-align:center">Nothing graded yet.</p>'}
</div>

<div style="font-size:11px;color:#998;text-align:center;margin-top:20px;line-height:1.6">
  Every pick is logged BEFORE its race runs, so nothing here is cherry-picked after the fact. A pick stays
  "pending" until its race has actually run and the API has posted a finishing position for at least one
  horse in that race -- if a race never shows a result, its picks stay pending indefinitely rather than
  being scored on a guess.
</div>
</body></html>"""

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html)
    print(f"  Results tracker: dashboard written to {out_path} "
          f"({overall_hits}/{overall_total} graded, {len(pending)} pending).")


def run_results_tracker(predictions, log_path=LOG_PATH, dash_path=DASH_PATH):
    log_todays_signals(predictions, log_path)
    verify_pending_results(log_path)
    build_results_dashboard(log_path, dash_path)
