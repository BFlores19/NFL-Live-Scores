# app/db/crud.py
from typing import Optional, Iterable, Dict, Any, List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models import Team, Player, Season, Game, GameTeam, PlayerPerformance, ScoringRule

# ----- Teams -----
def upsert_team(db: Session, abbr: str, name: str, logo_url: Optional[str] = None) -> Team:
    team = db.execute(select(Team).where(Team.abbr == abbr)).scalar_one_or_none()
    if team:
        team.name = name
        team.logo_url = logo_url
        return team
    team = Team(abbr=abbr, name=name, logo_url=logo_url)
    db.add(team)
    return team

# ----- Players -----
def upsert_player(db: Session, ext_id: str, name: str, position: Optional[str], team: Optional[Team]) -> Player:
    p = db.execute(select(Player).where(Player.ext_id == ext_id)).scalar_one_or_none()
    if p:
        p.name = name
        p.position = position
        p.team = team
        return p
    p = Player(ext_id=ext_id, name=name, position=position, team=team)
    db.add(p)
    return p

# ----- Seasons -----
def get_or_create_season(db: Session, year: int, pre_w1_start=None, reg_w1_start=None) -> Season:
    s = db.execute(select(Season).where(Season.year == year)).scalar_one_or_none()
    if s:
        return s
    s = Season(year=year, pre_w1_start=pre_w1_start, reg_w1_start=reg_w1_start)
    db.add(s)
    return s

# ----- Games -----
def upsert_game(
    db: Session,
    event_id: str,
    season: Season,
    overall_week: int,
    kickoff,                       # datetime | None
    status: Optional[str],
    venue: Optional[str] = None,
) -> Game:
    g = db.execute(select(Game).where(Game.event_id == event_id)).scalar_one_or_none()
    if g:
        g.season = season
        g.overall_week = overall_week
        g.kickoff = kickoff
        g.status = status
        g.venue = venue
        return g
    g = Game(
        event_id=event_id,
        season=season,
        overall_week=overall_week,
        kickoff=kickoff,
        status=status,
        venue=venue,
    )
    db.add(g)
    return g

def upsert_game_team(db: Session, game: Game, team: Team, home_away: str, score: Optional[int]) -> GameTeam:
    gt = db.execute(
        select(GameTeam).where(GameTeam.game_id == game.game_id, GameTeam.team_id == team.team_id)
    ).scalar_one_or_none()
    if gt:
        gt.home_away = home_away
        gt.score = score
        return gt
    gt = GameTeam(game=game, team=team, home_away=home_away, score=score)
    db.add(gt)
    return gt

# ----- Player performances (raw box stats) -----
def upsert_player_perf(
    db: Session,
    game: Game,
    player: Player,
    team: Team,
    position: Optional[str],
    stats: Dict[str, Any],
) -> PlayerPerformance:
    pp = db.execute(
        select(PlayerPerformance).where(
            PlayerPerformance.game_id == game.game_id,
            PlayerPerformance.player_id == player.player_id,
        )
    ).scalar_one_or_none()

    # extract with defaults
    def _stat(k): 
        try: return int(stats.get(k, 0))
        except Exception: 
            try: return float(stats.get(k, 0))
            except Exception: return 0

    fields = dict(
        position=position,
        team=team,
        pass_yd=_stat("passingYards"),
        pass_td=_stat("passingTouchdowns"),
        pass_int=_stat("interceptions"),
        rush_yd=_stat("rushingYards"),
        rush_td=_stat("rushingTouchdowns"),
        rec_yd=_stat("receivingYards"),
        rec_td=_stat("receivingTouchdowns"),
        receptions=_stat("receptions"),
        fumbles_lost=_stat("fumblesLost"),
    )

    if pp:
        for k, v in fields.items():
            setattr(pp, k, v if k != "team" else team)
        return pp

    pp = PlayerPerformance(game=game, player=player, team=team, **fields)
    db.add(pp)
    return pp
