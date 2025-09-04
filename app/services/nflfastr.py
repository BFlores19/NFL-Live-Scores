# services/nflfastr.py
"""
nflfastr / nflreadr weekly player-stats adapter (historical + current)

- Downloads season CSV once per process (with TTL) from:
  https://github.com/nflverse/nflverse-data/releases/download/player_stats/stats_player_week_{season}.csv
- Filters to the specific game using season, converted week (from your "overall_week"),
  and opponent matching.
- Maps nflverse columns to your canonical keys:
    passingYards, passingTouchdowns, interceptions,
    rushingYards, rushingTouchdowns,
    receivingYards, receivingTouchdowns, receptions,
    fumblesLost
- Yields (team_abbr, position, athlete_dict, stats_dict).
"""

from __future__ import annotations

import csv
import io
import logging
import time
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

CSV_BASE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/player_stats/"
    "stats_player_week_{season}.csv"
)

# Cache season CSVs in-memory for a while; nflverse updates nightly in-season
# (3–5am ET per docs), so a 20min TTL is a nice balance for dev.
CSV_TTL_SECONDS = 20 * 60

# Team alias map to reconcile ESPN <-> nflverse codes (upper-cased).
# - ESPN often uses WSH; nflverse standard is WAS.
# - Historical relocations and legacies covered.
TEAM_ALIAS_TO_NFLVERSE = {
    "WSH": "WAS",
    "JAC": "JAX",
    "SD": "LAC",
    "STL": "LAR",
    "OAK": "LV",
    # Passthrough for modern codes to be safe
    "LA": "LAR",
}
# And a reverse map for when we want to report back in ESPN-style if needed.
TEAM_ALIAS_TO_ESPN = {
    "WAS": "WSH",
    "JAX": "JAX",  # ESPN already uses JAX, keep stable
    "LAC": "LAC",
    "LAR": "LAR",
    "LV": "LV",
}

# Column names we expect in nflverse weekly "player stats" CSV (offense).
# Docs (nflreadr::load_player_stats) enumerate these; we handle fallbacks just in case.
# See: Data dictionary and function docs. (passing_yards, passing_tds, interceptions, etc.)
TEAM_COLS = ("team", "recent_team", "team_abbr")
OPP_COLS = ("opponent_team", "opponent", "opp")
POS_COLS = ("position",)
NAME_COLS = ("player_display_name", "player_name", "name")
ID_COLS = ("player_id", "gsis_id", "nflverse_id")

STAT_MAP = {
    "passingYards": ("passing_yards",),
    "passingTouchdowns": ("passing_tds",),
    "interceptions": ("interceptions",),  # thrown
    "rushingYards": ("rushing_yards",),
    "rushingTouchdowns": ("rushing_tds",),
    "receivingYards": ("receiving_yards",),
    "receivingTouchdowns": ("receiving_tds",),
    "receptions": ("receptions",),
    # We'll compute fumblesLost as a sum of available lost-fumble variants.
    # Dictionary lists rushing_fumbles_lost, receiving_fumbles_lost, sack_fumbles_lost.
    # Some builds may also include a generic fumbles_lost; include if present.
}

FUMBLE_LOST_CANDIDATES = (
    "rushing_fumbles_lost",
    "receiving_fumbles_lost",
    "sack_fumbles_lost",
    "fumbles_lost",
)

# Simple resilient HTTP session (timeouts + retry/backoff).
_session: Optional[requests.Session] = None
def _http() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _session = s
    return _session

# In-memory CSV cache: {season: {"fetched": ts, "rows": List[Dict[str, str]]}}
_csv_cache: Dict[int, Dict[str, object]] = {}

# ---------------------------------------------------------------------
# Public API (call this from fantasy.py)
# ---------------------------------------------------------------------

def iter_players_for_game(
    *,
    season: int,
    overall_week: int,
    home_abbr: str,
    away_abbr: str,
) -> Iterator[Tuple[str, str, Dict[str, str], Dict[str, int]]]:
    """
    Yield (team_abbr, position, athlete_dict, stats_dict) for a specific game,
    using nflverse/nflfastR weekly player stats.

    - season: official NFL season year (e.g., 2024)
    - overall_week: your app's "overall week"
        (1–3 = PRE weeks 1–3; 4.. = REG weeks starting at 1)
    - home_abbr / away_abbr: ESPN-style team abbreviations from your DB.
    """
    # Normalize team codes to nflverse standard so the CSV filter hits.
    home_nv = _to_nflverse_abbr(home_abbr)
    away_nv = _to_nflverse_abbr(away_abbr)

    season_type, nv_week = _convert_overall_to_nflverse(overall_week)
    rows = _load_player_stats_csv(season)

    # Figure out actual CSV header names for team/opponent/id/name/position once.
    team_key = _first_present_key(rows, TEAM_COLS)
    opp_key = _first_present_key(rows, OPP_COLS)
    pos_key = _first_present_key(rows, POS_COLS) or "position"
    name_key = _first_present_key(rows, NAME_COLS) or "player_display_name"
    id_key = _first_present_key(rows, ID_COLS) or "player_id"

    # These always exist per the docs
    # https://nflreadr.nflverse.com/reference/load_player_stats.html
    # (season, week, season_type, etc.)
    filtered = []
    for r in rows:
        try:
            if int(r.get("season", 0)) != season:
                continue
            # Match nflverse regular-week/post: if column exists, respect it; otherwise ignore.
            if "week" in r and str(r["week"]).isdigit() and int(r["week"]) != nv_week:
                continue
            if "season_type" in r and season_type and r["season_type"] != season_type:
                continue

            team = (r.get(team_key) or "").upper()
            opp = (r.get(opp_key) or "").upper() if opp_key else ""

            # Must be one of the two teams.
            if team not in (home_nv, away_nv):
                continue

            # If opponent column exists, make sure it matches the other team.
            if opp_key:
                expected_opp = away_nv if team == home_nv else home_nv
                if opp != expected_opp:
                    continue

            filtered.append(r)
        except Exception:  # be defensive against odd rows
            continue

    # If we somehow filtered to 0 rows (rare timing/version issues), try a looser filter:
    if not filtered:
        for r in rows:
            try:
                if int(r.get("season", 0)) != season:
                    continue
                if "week" in r and str(r["week"]).isdigit() and int(r["week"]) != nv_week:
                    continue
                team = (r.get(team_key) or "").upper()
                if team in (home_nv, away_nv):
                    filtered.append(r)
            except Exception:
                continue
        log.info(
            "nflfastr: fallback filter used (no opponent match) "
            f"season={season} week={nv_week} type={season_type} teams=[{home_nv},{away_nv}] rows={len(filtered)}"
        )

    # Yield rows mapped to your canonical stat keys
    for r in filtered:
        team = (r.get(team_key) or "").upper()
        pos = (r.get(pos_key) or "").upper() or "NA"
        name = (r.get(name_key) or "").strip()
        pid = (r.get(id_key) or "").strip()  # GSIS id when available

        athlete = {
            "ext_id": pid if pid else f"nflverse:{season}:{name}:{team}",
            "name": name,
        }

        stats = {
            "passingYards": _to_int(r.get("passing_yards")),
            "passingTouchdowns": _to_int(r.get("passing_tds")),
            "interceptions": _to_int(r.get("interceptions")),
            "rushingYards": _to_int(r.get("rushing_yards")),
            "rushingTouchdowns": _to_int(r.get("rushing_tds")),
            "receivingYards": _to_int(r.get("receiving_yards")),
            "receivingTouchdowns": _to_int(r.get("receiving_tds")),
            "receptions": _to_int(r.get("receptions")),
            "fumblesLost": _sum_ints(r, FUMBLE_LOST_CANDIDATES),
        }

        # Report team back in the same style your DB likely stores (ESPN-ish) to reduce remaps later.
        team_report = TEAM_ALIAS_TO_ESPN.get(team, team)
        yield (team_report, pos, athlete, stats)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _convert_overall_to_nflverse(overall_week: int) -> Tuple[str, int]:
    """
    Your app's "overall week" windows:
      - PRE: 1..3  (fixed Aug dates)
      - REG: starts at overall 4 => REG week 1, so REG week = overall_week - 3
    Return (season_type, nflverse_week)
    """
    if overall_week <= 0:
        return ("REG", 1)
    if overall_week <= 3:
        # nflverse docs primarily list REG/POST; many builds include PRE as well.
        # Keep PRE so we filter correctly if present; if PRE isn't present, our loose filter still works.
        return ("PRE", overall_week)
    # regular season
    return ("REG", overall_week - 3)


def _to_nflverse_abbr(abbr: str) -> str:
    if not abbr:
        return abbr
    a = abbr.upper()
    return TEAM_ALIAS_TO_NFLVERSE.get(a, a)


def _first_present_key(rows: List[Dict[str, str]], candidates: Tuple[str, ...]) -> Optional[str]:
    if not rows:
        return None
    row = rows[0]
    for c in candidates:
        if c in row:
            return c
    return None


def _to_int(v: Optional[str]) -> int:
    try:
        if v is None or v == "":
            return 0
        # some columns might be floats-as-strings
        return int(float(v))
    except Exception:
        return 0


def _sum_ints(row: Dict[str, str], keys: Tuple[str, ...]) -> int:
    total = 0
    for k in keys:
        if k in row:
            total += _to_int(row.get(k))
    return total


def _load_player_stats_csv(season: int) -> List[Dict[str, str]]:
    """
    Download (or return cached) weekly player stats CSV for a season.
    """
    now = time.time()
    cached = _csv_cache.get(season)
    if cached and (now - float(cached["fetched"])) < CSV_TTL_SECONDS:
        return cached["rows"]  # type: ignore[return-value]

    url = CSV_BASE_URL.format(season=season)
    log.info(f"nflfastr: downloading player stats CSV for season {season} from {url}")

    # 10s connect/read timeout; retries are configured on the session
    resp = _http().get(url, timeout=(8, 12))
    resp.raise_for_status()

    # Parse CSV into list-of-dicts
    text = resp.text
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    rows = [row for row in reader]

    _csv_cache[season] = {"fetched": now, "rows": rows}
    log.info(f"nflfastr: cached {len(rows)} rows for season {season}")
    return rows
