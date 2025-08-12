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
# cache key includes whether it's "current" or a specific (year, week) window
_cache: Dict[Tuple[Optional[int], Optional[int], Optional[int], str], Dict[str, Any]] = {}
_lock = asyncio.Lock()

def _next_weekday(d, target_weekday=2):
    """
    Return the next date on or after d that falls on target_weekday.
    Python weekday(): Mon=0 ... Sun=6, so Wednesday=2.
    """
    delta = (target_weekday - d.weekday()) % 7
    return d + timedelta(days=delta)

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

# -------------------- ESPN fetch primitives --------------------

async def _fetch_raw(params: Optional[Dict[str, Any]] = None) -> dict:
    client = await _get_client()
    r = await client.get(ESPN_SCOREBOARD, params=params or None)
    r.raise_for_status()
    return r.json()

# -------------------- Week boundary logic (custom) --------------------

def _parse_iso_date(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        # accept "YYYY-MM-DD" or ISO datetime
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None

async def _get_week1_start(year: int) -> date:
    """
    Find the earliest preseason game for the given season year.
    That becomes Week 1 start (HOF included), per your rule.
    Fallback: July 31 of that year.
    """
    # Ask for the season calendar snapshot
    cal = await _fetch_raw({"year": year, "seasontype": 1})
    leagues = cal.get("leagues") or []
    starts: List[date] = []

    if leagues:
        for block in (leagues[0].get("calendar") or []):
            # calendar blocks may have entries; collect their startDate fields
            entries = block.get("entries") or []
            for e in entries:
                d = _parse_iso_date(e.get("startDate") or "")
                if d:
                    starts.append(d)

    if starts:
        first = min(starts)
        return first  # HOF will be here if it exists

    # Fallback anchor if ESPN shape changes
    return date(year, 7, 31)

def _custom_week_range(week1_start: date, overall_week: int) -> Tuple[date, date]:
    """
    Week 1: 14 days starting at week1_start (through the following Tuesday).
    Weeks 2+: 7-day windows from Wednesday to Tuesday, starting at the
    first Wednesday on/after the day after Week 1 ends.
    """
    # Week 1: 14 days inclusive (Wed/Tue rule + early HOF)
    if overall_week <= 1:
        start = week1_start
        end = week1_start + timedelta(days=13)  # inclusive
        return (start, end)

    # Compute Week 2 anchor = first Wednesday on/after the day after Week 1 ends
    week1_end_plus_one = week1_start + timedelta(days=14)
    week2_start_anchor = _next_weekday(week1_end_plus_one, target_weekday=2)  # 2 = Wednesday

    # Week N (N>=2) starts at anchor + (N-2)*7, ends 6 days later (Tue)
    start = week2_start_anchor + timedelta(days=(overall_week - 2) * 7)
    end = start + timedelta(days=6)
    return (start, end)

# -------------------- Normalization --------------------

def _normalize(espn_json: dict) -> ScoresPayload:
    games = []
    for ev in espn_json.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        if len(comps) != 2:
            continue

        home = next((c for c in comps if c.get("homeAway") == "home"), comps[0])
        away = next((c for c in comps if c.get("homeAway") == "away"), comps[-1])

        def abbr(c):
            t = c.get("team") or {}
            return t.get("abbreviation") or t.get("shortDisplayName") or "UNK"

        def score(c):
            try:
                return int(c.get("score") or 0)
            except Exception:
                return None

        stype = (comp.get("status") or {}).get("type") or {}
        state = stype.get("state")   # "pre", "in", "post"
        display_clock = (comp.get("status") or {}).get("displayClock") or ""
        period = (comp.get("status") or {}).get("period") or None

        if state == "pre":
            try:
                kickoff_dt = datetime.fromisoformat((comp.get("date") or ev.get("date")).replace("Z", "+00:00"))
                kickoff_ct = kickoff_dt.astimezone(CT)
                pretty_status = kickoff_ct.strftime("%b %d, %I:%M %p CT")
            except Exception:
                pretty_status = "Pregame"
            home_score = away_score = None
        elif state == "in":
            q = f"Q{period}" if period else ""
            pretty_status = f"{q} {display_clock}".strip() or "In Progress"
            home_score = score(home)
            away_score = score(away)
        elif state == "post":
            pretty_status = "Final"
            home_score = score(home)
            away_score = score(away)
        else:
            pretty_status = "Status Unknown"
            home_score = away_score = None

        games.append({
            "id": str(ev.get("id") or ""),
            "away": abbr(away),
            "home": abbr(home),
            "awayScore": away_score,
            "homeScore": home_score,
            "status": pretty_status,
            "startTimeUtc": comp.get("date") or ev.get("date")
        })

    return {
        "asOfUtc": _iso_now(),
        "source": "ESPN public scoreboard",
        "games": games
    }

# -------------------- Public fetch API --------------------

async def fetch_scores_fresh(year: Optional[int] = None, week: Optional[int] = None, seasontype: Optional[int] = None) -> ScoresPayload:
    """
    If week is given, compute our custom Wed-based window and fetch with ?dates=YYYYMMDD-YYYYMMDD.
    IMPORTANT: pass ONLY 'dates' (no 'year') to avoid ESPN filtering out results.
    If week is omitted, fetch ESPN's current slate.
    """
    if week is not None and year is not None:
        wk1 = await _get_week1_start(year)
        start, end = _custom_week_range(wk1, week)
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
    Cache per (year, week) for custom windows, and a separate key for "current".
    We ignore seasontype here because your week number is "overall" already.
    """
    key = (year, week, None, "current" if week is None else "custom")
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
