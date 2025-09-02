# app/services/fantasy.py
from __future__ import annotations

import re
import json
import httpx
from typing import Any, Dict, Iterable, Tuple, AsyncGenerator

from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models import ScoringRule

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
BOX_HTML_URL = "https://www.espn.com/nfl/boxscore/_/gameId/{event_id}"
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"


# ----------------------------- Public fetch -----------------------------

async def fetch_summary(event_id: str, session: Optional[aiohttp.ClientSession] = None) -> dict:
    """Fetch the ESPN summary/boxscore for a given event."""
    url = f"https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"
    async with (session or aiohttp.ClientSession()) as sess:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ESPN summary fetch failed with status {resp.status}")
            return await resp.json()



# ----------------------------- HTML boxscore scraper -----------------------------

async def _fetch_boxscore_fitt(event_id: str) -> dict | None:
    """
    Fetch the public ESPN boxscore HTML and extract the embedded JSON assigned to window.__espnfitt__.
    Returns that dict (root boot JSON) or None if not found/parsable.
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(timeout=12, headers=headers) as client:
        r = await client.get(BOX_HTML_URL.format(event_id=event_id))
        r.raise_for_status()
        html = r.text

    # window.__espnfitt__ = {...};
    m = re.search(r"window(?:\[['\"]__espnfitt__['\"]|.__espnfitt__)\s*=\s*(\{.*?\})\s*;\s*</script>", html, re.DOTALL)
    if not m:
        return None
    raw = m.group(1)
    try:
        return json.loads(raw)
    except Exception:
        # tolerate minor garbage
        try:
            return json.loads(raw.encode("utf-8", "ignore").decode("utf-8", "ignore"))
        except Exception:
            return None


def _fitt_gamepackage_json(root: dict | None) -> dict | None:
    """
    Find gamepackageJSON inside the fitt boot object; ESPN nests this a few ways.
    """
    if not isinstance(root, dict):
        return None

    def _get(d, *path):
        cur = d
        for p in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        return cur

    gpj = (
        _get(root, "page", "content", "gamepackage", "gamepackageJSON")
        or _get(root, "page", "content", "gamepackageJSON")
        or _get(root, "content", "gamepackage", "gamepackageJSON")
        or _get(root, "content", "gamepackageJSON")
    )
    return gpj if isinstance(gpj, dict) else None


# ----------------------------- Scoring -----------------------------

def _get_full_ppr(db: Session) -> ScoringRule:
    rule = db.execute(select(ScoringRule).where(ScoringRule.name == "Full PPR")).scalar_one_or_none()
    if not rule:
        raise RuntimeError("Full PPR rule missing—seed it first.")
    return rule

def _points(stats: dict, R: ScoringRule) -> float:
    gi = lambda k: float(stats.get(k, 0) or 0)
    return (
        float(R.pass_yd)     * gi("passingYards") +
        float(R.pass_td)     * gi("passingTouchdowns") +
        float(R.pass_int)    * gi("interceptions") +
        float(R.rush_yd)     * gi("rushingYards") +
        float(R.rush_td)     * gi("rushingTouchdowns") +
        float(R.rec_yd)      * gi("receivingYards") +
        float(R.rec_td)      * gi("receivingTouchdowns") +
        float(R.reception)   * gi("receptions") +
        float(R.fumble_lost) * gi("fumblesLost")
    )


# ----------------------------- Player extraction (Summary JSON) -----------------------------

def _summary_players_iter(summary_json: dict) -> Iterable[Tuple[str, str, Dict[str, Any], Dict[str, float]]]:
    """
    Yields (team_abbr, position, athlete_dict, stats_dict) from the SUMMARY JSON.
    Supports BOTH shapes ESPN uses:
      A) boxscore.teams[].players[].athletes[]
      B) boxscore.teams[].statistics[].athletes[]
    """

    def coerce_num(v):
        try:
            return float(v)
        except Exception:
            try:
                return float(str(v).replace(",", ""))
            except Exception:
                return 0.0

    ALIASES = {
        "passingYds": "passingYards",
        "passingTDs": "passingTouchdowns",
        "ints": "interceptions",
        "rushingYds": "rushingYards",
        "rushingTDs": "rushingTouchdowns",
        "receivingYds": "receivingYards",
        "receivingTDs": "receivingTouchdowns",
        "rec": "receptions",
        "fumbles": "fumblesLost",  # prefer fumblesLost if present
    }

    box = summary_json.get("boxscore") or {}
    for t in (box.get("teams") or []):
        team_abbr = ((t.get("team") or {}).get("abbreviation") or "").upper().strip()
        if not team_abbr:
            continue

        groups = []
        if isinstance(t.get("players"), list):
            groups.extend(t.get("players"))
        if isinstance(t.get("statistics"), list):
            groups.extend(t.get("statistics"))

        for grp in groups:
            pos = ""
            pos_obj = grp.get("position")
            if isinstance(pos_obj, dict):
                pos = (pos_obj.get("abbreviation") or pos_obj.get("displayName") or "").upper()

            for row in (grp.get("athletes") or []):
                athlete = row.get("athlete") or {}
                if not athlete:
                    continue

                stats: dict = {}

                totals = row.get("totals")
                if isinstance(totals, dict):
                    for k, v in totals.items():
                        stats[k] = coerce_num(v)

                raw_stats = row.get("stats")
                if isinstance(raw_stats, dict):
                    for k, v in raw_stats.items():
                        stats[k] = coerce_num(v)
                elif isinstance(raw_stats, list):
                    for sc in raw_stats:
                        if not isinstance(sc, dict):
                            continue
                        if "name" in sc:
                            stats[sc["name"]] = coerce_num(sc.get("value"))
                        if "abbreviation" in sc:
                            ab = sc["abbreviation"]
                            val = coerce_num(sc.get("value"))
                            if ab == "INT":
                                stats.setdefault("interceptions", val)
                            elif ab == "REC":
                                stats.setdefault("receptions", val)

                out = {}
                has_true_fumbles_lost = "fumblesLost" in stats
                for k, v in stats.items():
                    canon = ALIASES.get(k, k)
                    if canon == "fumblesLost" and has_true_fumbles_lost and k != "fumblesLost":
                        continue
                    out[canon] = coerce_num(v)

                a_pos = (athlete.get("position") or {}).get("abbreviation") or ""
                use_pos = (a_pos or pos or "").upper()

                yield team_abbr, use_pos, athlete, out


# ----------------------------- Player extraction (Core graph) -----------------------------

def _core_fetch_json(client: httpx.Client, url: str) -> dict:
    r = client.get(url)
    r.raise_for_status()
    return r.json()

def _core_competitor_items(client: httpx.Client, comp: dict) -> list[dict]:
    """
    Return a list of competitor refs/objects from a competition object,
    handling shapes:
      - {"competitors":{"items":[...]}}
      - {"competitors":{"$ref":".../competitors"}}
      - {"competitors":[ ... ]}
    """
    comps = comp.get("competitors")
    if isinstance(comps, dict):
        if "items" in comps and isinstance(comps["items"], list):
            return comps["items"]
        if "$ref" in comps and isinstance(comps["$ref"], str):
            try:
                linked = _core_fetch_json(client, comps["$ref"])
                return linked.get("items", []) if isinstance(linked, dict) else []
            except Exception:
                return []
        return []
    if isinstance(comps, list):
        return comps
    return []

def _core_resolve_team_info(client: httpx.Client, team_field: dict | None) -> dict:
    """
    competitor['team'] might be a dict with '$ref' or an inline team object.
    Return a dict that has at least 'abbreviation' if possible.
    """
    if not isinstance(team_field, dict):
        return {}
    if "abbreviation" in team_field:
        return team_field
    ref = team_field.get("$ref")
    if isinstance(ref, str) and ref:
        try:
            return _core_fetch_json(client, ref)
        except Exception:
            return {}
    return {}

def _core_players_iter(event_id: str) -> Iterable[Tuple[str, str, Dict[str, Any], Dict[str, float]]]:
    """
    Fallback: walk ESPN Core graph to fetch per‑athlete stats.
    Handles list/dict variations in competitors and team refs.
    """
    with httpx.Client(timeout=12) as client:
        comp = _core_fetch_json(client, f"{CORE_BASE}/events/{event_id}/competitions/{event_id}")

        teams = _core_competitor_items(client, comp)
        for team_ref in teams:
            # team_ref may be {"$ref": "..."} or an inline object
            if isinstance(team_ref, dict) and "$ref" in team_ref:
                team_obj = _core_fetch_json(client, team_ref["$ref"])
            else:
                team_obj = team_ref if isinstance(team_ref, dict) else {}

            team_info = _core_resolve_team_info(client, team_obj.get("team"))
            team_abbr = (team_info.get("abbreviation") or "").upper().strip()
            comp_team_id = str(team_obj.get("id") or "").strip()  # competitor id within this competition

            if not team_abbr or not comp_team_id:
                continue

            # competition-scoped roster for this competitor id
            ros_url = f"{CORE_BASE}/events/{event_id}/competitions/{event_id}/competitors/{comp_team_id}/roster"
            try:
                ros = _core_fetch_json(client, ros_url)
            except Exception:
                continue

            for item in (ros.get("items") or []):
                try:
                    ath = _core_fetch_json(client, item["$ref"]) if isinstance(item, dict) and "$ref" in item else item
                except Exception:
                    continue

                athlete = {
                    "id": ath.get("id"),
                    "displayName": ath.get("displayName") or ath.get("shortName"),
                    "position": ath.get("position") or {},
                }
                pos = (ath.get("position") or {}).get("abbreviation") or ""

                # per‑athlete per‑game statistics bucket "0"
                try:
                    stats0 = _core_fetch_json(
                        client,
                        f"{CORE_BASE}/events/{event_id}/competitions/{event_id}/competitors/{comp_team_id}/roster/{ath['id']}/statistics/0"
                    )
                except Exception:
                    continue

                stats = {}
                for cat in (stats0.get("categories") or []):
                    for metric in (cat.get("stats") or []):
                        k = metric.get("name")
                        v = metric.get("value")
                        if k is not None and v is not None:
                            stats[k] = v

                yield team_abbr, (pos or "").upper(), athlete, stats


# ----------------------------- Utilities -----------------------------

def _extract_event_id_from_summary(summary_json: dict) -> str | None:
    try:
        h = summary_json.get("header") or {}
        if "id" in h:
            return str(h["id"])
        comps = (h.get("competitions") or [])
        if comps and isinstance(comps, list):
            cid = comps[0].get("id")
            if cid:
                return str(cid)
    except Exception:
        pass
    return None


# ----------------------------- Public iterator (async) -----------------------------

async def _iter_players(summary_json: dict) -> AsyncGenerator[Tuple[str, str, Dict[str, Any], Dict[str, float]], None]:
    """
    Yield players in this order:
      1) SUMMARY JSON (fast path)
      2) Embedded JSON from boxscore HTML (reliable for live games)
      3) CORE graph (historical)
    """
    yielded_any = False

    # 1) summary shapes
    for tup in _summary_players_iter(summary_json):
        yielded_any = True
        yield tup
    if yielded_any:
        return

    # 2) HTML boxscore JSON (requires event id)
    event_id = _extract_event_id_from_summary(summary_json)
    if event_id:
        fitt = await _fetch_boxscore_fitt(event_id)
        gpj = _fitt_gamepackage_json(fitt) if fitt else None
        if isinstance(gpj, dict):
            for tup in _summary_players_iter(gpj):
                yielded_any = True
                yield tup
            if yielded_any:
                return

    # 3) CORE fallback (requires event id)
    if event_id:
        for tup in _core_players_iter(event_id):
            yield tup
