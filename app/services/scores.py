import httpx
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List
from zoneinfo import ZoneInfo  # Python 3.9+

CT = ZoneInfo("America/Chicago")
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
CACHE_TTL_SECONDS = 10  # refresh interval

@dataclass
class Game:
    id: str
    away: str
    home: str
    awayScore: Optional[int]
    homeScore: Optional[int]
    status: str
    startTimeUtc: str

ScoresPayload = Dict[str, Any]

_client: Optional[httpx.AsyncClient] = None
_cache: Dict[Tuple[Optional[int], Optional[int], str], Dict[str, Any]] = {}
_lock = asyncio.Lock()

def _iso_now() -> str:
    # Central Time "as of"
    return datetime.now(CT).isoformat()

def _now() -> float:
    return time.time()

async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=12)
    return _client

# -------------------- ESPN fetch primitive --------------------

async def _fetch_raw(params: Optional[Dict[str, Any]] = None) -> dict:
    client = await _get_client()
    r = await client.get(ESPN_SCOREBOARD, params=params or None)
    r.raise_for_status()
    return r.json()

# -------------------- Fixed overall-week windows --------------------
# Rule (your latest spec):
#   Wk 1:  Aug 7  → Aug 12   (inclusive)
#   Wk 2+: start Aug 13, then Wed→Tue 7-day windows forever (repeat through season)

def _fixed_overall_week_range(year: int, overall_week: int) -> Tuple[date, date]:
    # Preseason
    if overall_week == 1:
        return (date(year, 8, 7), date(year, 8, 12))
    if overall_week == 2:
        return (date(year, 8, 13), date(year, 8, 19))
    if overall_week == 3:
        return (date(year, 8, 20), date(year, 8, 26))

    # Gap week (Aug 27–Sep 3) is ignored — won't match any week
    if overall_week == 4:
        # Regular Season Week 1
        start = date(year, 9, 4)
        end = start + timedelta(days=6)
        return (start, end)

    # Regular season Week N after week 4
    start = date(year, 9, 4) + timedelta(days=(overall_week - 4) * 7)
    end = start + timedelta(days=6)
    return (start, end)


# -------------------- Normalization --------------------

def _normalize(espn_json: dict) -> ScoresPayload:
    def team_abbr(c):
        t = c.get("team") or {}
        return t.get("abbreviation") or t.get("shortDisplayName") or "UNK"

    def team_logo(c):
        t = c.get("team") or {}
        if t.get("logo"):
            return t["logo"]
        logos = t.get("logos") or []
        if logos and isinstance(logos, list) and logos[0].get("href"):
            return logos[0]["href"]
        return ""

    def as_int_or_none(val):
        try:
            return int(val)
        except Exception:
            return None

    games = []
    for ev in espn_json.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        if len(comps) != 2:
            continue

        home = next((c for c in comps if c.get("homeAway") == "home"), comps[0])
        away = next((c for c in comps if c.get("homeAway") == "away"), comps[-1])

        status_obj = (comp.get("status") or {})
        stype = status_obj.get("type") or {}
        state = stype.get("state")  # "pre", "in", "post"
        display_clock = status_obj.get("displayClock") or ""
        period = status_obj.get("period") or None

        if state == "pre":
            try:
                kickoff_dt = datetime.fromisoformat((comp.get("date") or ev.get("date")).replace("Z", "+00:00"))
                kickoff_dt = kickoff_dt.astimezone(CT)
                pretty_status = kickoff_dt.strftime("%b %d, %I:%M %p CT")
            except Exception:
                pretty_status = "Pregame"
            home_score = away_score = None
        elif state == "in":
            q = f"Q{period}" if period else ""
            pretty_status = f"{q} {display_clock}".strip() or "In Progress"
            home_score = as_int_or_none(home.get("score"))
            away_score = as_int_or_none(away.get("score"))
        elif state == "post":
            pretty_status = "Final"
            home_score = as_int_or_none(home.get("score"))
            away_score = as_int_or_none(away.get("score"))
        else:
            pretty_status = "Status Unknown"
            home_score = away_score = None

        games.append({
            "id": str(ev.get("id") or ""),
            "away": team_abbr(away),
            "home": team_abbr(home),
            "awayScore": away_score,
            "homeScore": home_score,
            "status": pretty_status,
            "startTimeUtc": comp.get("date") or ev.get("date"),
            "awayLogo": team_logo(away),
            "homeLogo": team_logo(home),
        })

    return {
        "asOfUtc": _iso_now(),  # in CT
        "source": "ESPN public scoreboard",
        "games": games,
    }

# -------------------- Public fetch API --------------------

async def fetch_scores_fresh(year: Optional[int] = None, week: Optional[int] = None, seasontype: Optional[int] = None) -> ScoresPayload:
    """
    For a specific overall week: compute the fixed window by rule and fetch with ?dates=YYYYMMDD-YYYYMMDD.
    IMPORTANT: pass ONLY 'dates' (no 'year') to avoid ESPN filtering inconsistencies.
    If no week is given: return ESPN's current slate.
    """
    if week is not None and year is not None:
        start, end = _fixed_overall_week_range(year, week)

        # Hard ignore anything before Aug 7 implicitly via the window.
        # (HoF on Jul 31 won't be in any window.)
        dates_range = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
        raw = await _fetch_raw({"dates": dates_range})
        print(f"[scores] week={week} year={year} window={dates_range} events={len(raw.get('events', []))}")
        return _normalize(raw)

    # No specific week: ESPN's current slate
    raw = await _fetch_raw()
    try:
        cw = raw.get("week", {}).get("number")
        cst = raw.get("season", {}).get("type")
        cy = raw.get("season", {}).get("year")
        print(f"[scores] current slate per ESPN -> week={cw}, seasontype={cst}, year={cy}")
    except Exception:
        pass
    return _normalize(raw)

async def get_scores_cached(year: Optional[int] = None, week: Optional[int] = None, seasontype: Optional[int] = None) -> ScoresPayload:
    """
    Cache per (year, week) for fixed windows, and a separate key for "current".
    'seasontype' is unused here (your overall week controls the window).
    """
    key = (year, week, "current" if week is None else "fixed")
    entry = _cache.get(key)
    if entry and entry["expires"] > _now():
        return entry["data"]

    async with _lock:
        entry = _cache.get(key)
        if entry and entry["expires"] > _now():
            return entry["data"]

        delay = 0.5
        last_err = None
        for _ in range(4):
            try:
                data = await fetch_scores_fresh(year=year, week=week, seasontype=seasontype)
                _cache[key] = {"data": data, "expires": _now() + CACHE_TTL_SECONDS}
                return data
            except Exception as e:
                last_err = e
                await asyncio.sleep(delay)
                delay *= 2

        if key in _cache:
            stale = dict(_cache[key]["data"])
            stale["source"] += " (stale)"
            return stale

        raise last_err or RuntimeError("Failed to fetch ESPN scoreboard")

