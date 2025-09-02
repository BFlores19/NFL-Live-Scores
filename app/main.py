from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from datetime import datetime, timezone, timedelta
from typing import List
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db import get_db
from app.models import Game, Team, PlayerPerformance
from app.services.scores import (
    get_scores_cached,
    fetch_scores_fresh,
    _fixed_overall_week_range,
)
from app.services.fantasy import (
    fetch_summary,
    _iter_players,
    _get_full_ppr,
    _points,
)
from app.db.crud import (
    upsert_player,
    upsert_player_perf,
    upsert_team,
    get_or_create_season,
    upsert_game,
    upsert_game_team,
)

app = FastAPI(title="NFL Live Scores (ESPN)")
templates = Jinja2Templates(directory="app/templates")
CT = ZoneInfo("America/Chicago")

# ---------------------- Helpers ----------------------

def _extract_safe(d: dict, *path, default=None):
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int) and 0 <= p < len(cur):
            cur = cur[p]
        else:
            return default
    return cur if cur is not None else default

def _infer_overall_week_from_kickoff(kickoff_dt: datetime) -> int:
    k_date = kickoff_dt.astimezone(CT).date()
    year = k_date.year
    for w in range(1, 22):  # Pre 1-3, Reg 4-21
        start, end = _fixed_overall_week_range(year, w)
        if start <= k_date <= end:
            return w
    return 4  # default to Regular Wk 1

# ---------------------- Base / Health / Scores ----------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

@app.get("/api/scores")
async def scores(year: int | None = None, week: int | None = None, seasontype: int | None = None):
    # seasontype: 1 = Preseason, 2 = Regular, 3 = Postseason
    if seasontype and seasontype not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="seasontype must be 1, 2, or 3")
    data = await get_scores_cached(year=year, week=week, seasontype=seasontype)
    return JSONResponse(data)

# ---------------------- Week Meta (for frontend defaults) ----------------------

@app.get("/api/weekmeta")
def weekmeta():
    today_ct = datetime.now(CT).date()
    year = today_ct.year
    guess_week = 4
    for w in range(1, 22):
        start, end = _fixed_overall_week_range(year, w)
        if start <= today_ct <= end:
            guess_week = w
            break
    return {"year": year, "week": guess_week}

# ---------------------- Single Game Save / Score ----------------------

@app.post("/api/games/{event_id}/save")
async def save_game(event_id: str, db: Session = Depends(get_db)):
    """
    Fetch ESPN summary for a game and upsert Season, Game, Teams, and GameTeam rows.
    """
    summary = await fetch_summary(event_id)

    comp0 = _extract_safe(summary, "header", "competitions", 0, default={}) or {}
    competitors = comp0.get("competitors") or []
    if len(competitors) != 2:
        raise HTTPException(400, detail="Unexpected ESPN payload: missing competitors")

    kickoff_iso = comp0.get("date") or _extract_safe(summary, "header", "competitions", 0, "date")
    if not kickoff_iso:
        raise HTTPException(400, detail="Missing kickoff date in ESPN payload")
    try:
        kickoff_dt = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, detail="Invalid kickoff datetime format from ESPN")

    venue_name = (
        _extract_safe(comp0, "venue", "fullName")
        or _extract_safe(summary, "gameInfo", "venue", "fullName")
        or None
    )

    year = kickoff_dt.astimezone(CT).year
    overall_week = _infer_overall_week_from_kickoff(kickoff_dt)
    season = get_or_create_season(db, year=year)

    team_rows = {}
    for c in competitors:
        team_obj = c.get("team") or {}
        abbr = (team_obj.get("abbreviation") or team_obj.get("shortDisplayName") or "").upper()
        name = team_obj.get("displayName") or team_obj.get("name") or abbr or "Unknown"
        logo = team_obj.get("logo") or None
        if not logo:
            logos = team_obj.get("logos") or []
            if isinstance(logos, list) and logos and isinstance(logos[0], dict):
                logo = logos[0].get("href")
        if not abbr:
            raise HTTPException(400, detail="Missing team abbreviation in ESPN payload")

        team = upsert_team(db, abbr=abbr, name=name, logo_url=logo)

        score_val = c.get("score")
        try:
            score_int = int(score_val) if score_val is not None else None
        except Exception:
            score_int = None

        team_rows[c.get("homeAway")] = {"team": team, "score": score_int}

    if "home" not in team_rows or "away" not in team_rows:
        raise HTTPException(400, detail="Could not identify both home and away teams")

    status_state = _extract_safe(comp0, "status", "type", "state", default="pre")
    status_map = {"pre": "pre", "in": "in", "post": "post"}
    status = status_map.get(str(status_state).lower(), "pre")

    game = upsert_game(
        db=db,
        event_id=str(event_id),
        season=season,
        overall_week=overall_week,
        kickoff=kickoff_dt.astimezone(timezone.utc),
        status=status,
        venue=venue_name,
    )

    upsert_game_team(db, game=game, team=team_rows["home"]["team"], home_away="home", score=team_rows["home"]["score"])
    upsert_game_team(db, game=game, team=team_rows["away"]["team"], home_away="away", score=team_rows["away"]["score"])

    db.commit()

    return {
        "ok": True,
        "event_id": str(event_id),
        "year": year,
        "overall_week": overall_week,
        "venue": venue_name,
        "home": team_rows["home"]["team"].abbr,
        "away": team_rows["away"]["team"].abbr,
        "status": status,
        "kickoff": kickoff_dt.astimezone(timezone.utc).isoformat(),
    }

@app.post("/api/games/{event_id}/fantasy/fullppr")
async def compute_fantasy_fullppr(event_id: str, db: Session = Depends(get_db)):
    g = db.execute(select(Game).where(Game.event_id == event_id)).scalar_one_or_none()
    if not g:
        raise HTTPException(404, detail="Game not found in DB yet. (Save it first.)")

    summary = await fetch_summary(event_id)
    rule = _get_full_ppr(db)

    teams = {t.abbr: t for t in db.query(Team).all()}

    perfs: list[PlayerPerformance] = []
    async for abbr, pos, athlete, stats in _iter_players(summary):
        team = teams.get(abbr)
        if not team:
            continue
        ext_id = str(athlete.get("id") or athlete.get("uid") or "")
        name = athlete.get("displayName") or athlete.get("shortName") or "Unknown"
        player = upsert_player(db, ext_id=ext_id, name=name, position=pos, team=team)
        pp = upsert_player_perf(db, game=g, player=player, team=team, position=pos, stats=stats)
        pp.fantasy_points = round(_points(stats, rule), 2)
        perfs.append(pp)

    db.commit()

    res = {}
    for abbr, team in teams.items():
        team_perfs = [p for p in perfs if p.team_id == team.team_id]
        tops = sorted(team_perfs, key=lambda p: float(p.fantasy_points or 0), reverse=True)[:3]
        if tops:
            res[abbr] = [
                {"player": t.player.name, "pos": t.position, "points": float(t.fantasy_points or 0)}
                for t in tops
            ]

    return {
        "event_id": event_id,
        "parsed": len(perfs),
        "top_full_ppr": res
    }

@app.get("/api/games/{event_id}/fantasy/top")
def fantasy_top(event_id: str, top: int = 5, db: Session = Depends(get_db)):
    """
    Read-only: return top-N fantasy performers (already computed & stored).
    """
    g = db.execute(select(Game).where(Game.event_id == event_id)).scalar_one_or_none()
    if not g:
        raise HTTPException(404, detail="Game not found. Save it first.")
    perfs = (
        db.query(PlayerPerformance)
          .filter(PlayerPerformance.game_id == g.game_id)
          .order_by(PlayerPerformance.fantasy_points.desc())
          .limit(max(1, min(50, top)))
          .all()
    )
    return {
        "event_id": event_id,
        "top": [
            {"player": p.player.name, "team": p.team.abbr, "pos": p.position, "points": float(p.fantasy_points or 0)}
            for p in perfs
        ],
    }

# ---------------------- BULK Ingest / Backfill a Week ----------------------

async def _save_game_internal(event_id: str, db: Session):
    """Internal: same as /save, but returns the Game row for reuse."""
    summary = await fetch_summary(event_id)
    comp0 = _extract_safe(summary, "header", "competitions", 0, default={}) or {}
    competitors = comp0.get("competitors") or []
    if len(competitors) != 2:
        raise HTTPException(400, detail=f"Unexpected ESPN payload for {event_id}: missing competitors")

    kickoff_iso = comp0.get("date") or _extract_safe(summary, "header", "competitions", 0, "date")
    if not kickoff_iso:
        raise HTTPException(400, detail=f"Missing kickoff date for {event_id}")
    kickoff_dt = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))

    venue_name = (
        _extract_safe(comp0, "venue", "fullName")
        or _extract_safe(summary, "gameInfo", "venue", "fullName")
        or None
    )

    year = kickoff_dt.astimezone(CT).year
    overall_week = _infer_overall_week_from_kickoff(kickoff_dt)
    season = get_or_create_season(db, year=year)

    team_rows = {}
    for c in competitors:
        team_obj = c.get("team") or {}
        abbr = (team_obj.get("abbreviation") or team_obj.get("shortDisplayName") or "").upper()
        name = team_obj.get("displayName") or team_obj.get("name") or abbr or "Unknown"
        logo = team_obj.get("logo") or None
        if not logo:
            logos = team_obj.get("logos") or []
            if isinstance(logos, list) and logos and isinstance(logos[0], dict):
                logo = logos[0].get("href")
        if not abbr:
            raise HTTPException(400, detail=f"Missing team abbreviation for {event_id}")
        team = upsert_team(db, abbr=abbr, name=name, logo_url=logo)

        score_val = c.get("score")
        try:
            score_int = int(score_val) if score_val is not None else None
        except Exception:
            score_int = None

        team_rows[c.get("homeAway")] = {"team": team, "score": score_int}

    if "home" not in team_rows or "away" not in team_rows:
        raise HTTPException(400, detail=f"Home/Away not found for {event_id}")

    status_state = _extract_safe(comp0, "status", "type", "state", default="pre")
    status_map = {"pre": "pre", "in": "in", "post": "post"}
    status = status_map.get(str(status_state).lower(), "pre")

    game = upsert_game(
        db=db,
        event_id=str(event_id),
        season=season,
        overall_week=overall_week,
        kickoff=kickoff_dt.astimezone(timezone.utc),
        status=status,
        venue=venue_name,
    )
    upsert_game_team(db, game=game, team=team_rows["home"]["team"], home_away="home", score=team_rows["home"]["score"])
    upsert_game_team(db, game=game, team=team_rows["away"]["team"], home_away="away", score=team_rows["away"]["score"])
    return game

async def _score_game_internal(event_id: str, db: Session) -> int:
    """Internal: compute Full‑PPR for a saved game; returns number of rows written/updated."""
    g = db.execute(select(Game).where(Game.event_id == event_id)).scalar_one_or_none()
    if not g:
        raise HTTPException(404, detail=f"Game {event_id} not found in DB (save first).")

    summary = await fetch_summary(event_id)
    rule = _get_full_ppr(db)
    teams = {t.abbr: t for t in db.query(Team).all()}

    written = 0
    async for abbr, pos, athlete, stats in _iter_players(summary):
        team = teams.get(abbr)
        if not team:
            continue
        ext_id = str(athlete.get("id") or athlete.get("uid") or "")
        name = athlete.get("displayName") or athlete.get("shortName") or "Unknown"
        player = upsert_player(db, ext_id=ext_id, name=name, position=pos, team=team)
        pp = upsert_player_perf(db, game=g, player=player, team=team, position=pos, stats=stats)
        pp.fantasy_points = round(_points(stats, rule), 2)
        written += 1
    return written

@app.post("/api/weeks/{year}/{week}/ingest")
async def ingest_week(year: int, week: int, score: bool = True, db: Session = Depends(get_db)):
    """
    Bulk backfill: save all games for a given fixed window (year/week) and optionally compute Full‑PPR.
    Returns which event_ids were saved/scored and any errors, plus an allFinal heuristic for the window.
    """
    payload = await fetch_scores_fresh(year=year, week=week, seasontype=None)
    events: List[str] = [str(g["id"]) for g in payload.get("games", []) if g.get("id")]
    saved, scored, errors = [], [], []

    for eid in events:
        try:
            _ = await _save_game_internal(eid, db)
            db.commit()
            saved.append(eid)
            if score:
                wrote = await _score_game_internal(eid, db)
                db.commit()
                scored.append({"event_id": eid, "rows": wrote})
        except Exception as e:
            db.rollback()
            errors.append({"event_id": eid, "error": str(e)})

    # Heuristic: if the window ended at least one full day ago in CT, assume all games are final.
    start_d, end_d = _fixed_overall_week_range(year, week)
    all_final = datetime.now(CT).date() >= (end_d + timedelta(days=1))

    return {
        "year": year,
        "week": week,
        "events": events,
        "saved": saved,
        "scored": scored,
        "errors": errors,
        "allFinal": all_final,
    }
