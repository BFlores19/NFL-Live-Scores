import httpx
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# ✅ Correct ESPN endpoint (note the "site" segment)
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

CACHE_TTL_SECONDS = 10  # don't hammer ESPN

@dataclass
class Game:
    id: str
    away: str
    home: str
    awayScore: int
    homeScore: int
    status: str        # e.g., "Q3 08:12", "Final", "Pregame"
    startTimeUtc: str  # ISO8601

ScoresPayload = Dict[str, Any]  # {"asOfUtc": str, "source": str, "games": list[Game]}

_client: Optional[httpx.AsyncClient] = None
# cache is per-date so /api/scores?date=YYYYMMDD doesn't clash with "today"
_cache: Dict[str, Dict[str, Any]] = {}  # { key: {"data":..., "expires": float} }
_lock = asyncio.Lock()

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _now() -> float:
    return time.time()

async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    return _client

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
                return 0

        stype = (comp.get("status") or {}).get("type") or {}
        state = stype.get("state")   # "pre", "in", "post"
        detail = stype.get("shortDetail") or ""
        display_clock = (comp.get("status") or {}).get("displayClock") or ""
        period = (comp.get("status") or {}).get("period") or None

        if state == "pre":
            pretty = "Pregame"
        elif state == "in":
            q = f"Q{period}" if period else ""
            pretty = f"{q} {display_clock}".strip() if (q or display_clock) else (detail or "In Progress")
        elif state == "post":
            pretty = "Final"
        else:
            pretty = detail or "Status Unknown"

        start_iso = (comp.get("date") or ev.get("date") or _iso_now())

        games.append({
            "id": str(ev.get("id") or ""),
            "away": abbr(away),
            "home": abbr(home),
            "awayScore": score(away),
            "homeScore": score(home),
            "status": pretty,
            "startTimeUtc": start_iso
        })

    return {"asOfUtc": _iso_now(), "source": "ESPN public scoreboard", "games": games}

async def fetch_scores_fresh(date_yyyymmdd: Optional[str] = None) -> ScoresPayload:
    client = await _get_client()
    params = {"dates": date_yyyymmdd} if date_yyyymmdd else None
    r = await client.get(ESPN_SCOREBOARD, params=params)
    r.raise_for_status()
    return _normalize(r.json())

async def get_scores_cached(date_yyyymmdd: Optional[str] = None) -> ScoresPayload:
    key = date_yyyymmdd or "today"
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
                data = await fetch_scores_fresh(date_yyyymmdd)
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
