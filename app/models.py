# app/models.py
from sqlalchemy import (
    Column, Integer, String, Text, ForeignKey, UniqueConstraint,
    CheckConstraint, Date, DateTime, BigInteger, Numeric
)
from sqlalchemy.orm import relationship, Mapped, mapped_column
from datetime import datetime
from app.db import Base

# TEAMS
class Team(Base):
    __tablename__ = "teams"
    team_id: Mapped[int] = mapped_column(primary_key=True)
    abbr: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

# PLAYERS
class Player(Base):
    __tablename__ = "players"
    player_id: Mapped[int] = mapped_column(primary_key=True)
    ext_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # ESPN/GSIS
    name: Mapped[str] = mapped_column(String(128), index=True)
    position: Mapped[str | None] = mapped_column(String(8))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"))

    team = relationship("Team")

# SEASONS
class Season(Base):
    __tablename__ = "seasons"
    season_id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(unique=True, index=True)
    pre_w1_start: Mapped[Date | None] = mapped_column(Date)   # e.g., 2025-08-07
    reg_w1_start: Mapped[Date | None] = mapped_column(Date)   # e.g., 2025-09-04

# GAMES
class Game(Base):
    __tablename__ = "games"
    game_id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # ESPN event id
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"))
    overall_week: Mapped[int] = mapped_column()
    kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(16))  # pre/in/post/final
    venue: Mapped[str | None] = mapped_column(String(128))

    season = relationship("Season")
    teams = relationship("GameTeam", back_populates="game", cascade="all, delete-orphan")
    performances = relationship("PlayerPerformance", back_populates="game", cascade="all, delete-orphan")

# GAME_TEAMS
class GameTeam(Base):
    __tablename__ = "game_teams"
    game_id: Mapped[int] = mapped_column(ForeignKey("games.game_id"), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), primary_key=True)
    home_away: Mapped[str] = mapped_column(String(4))  # 'home' or 'away'
    score: Mapped[int | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("home_away IN ('home','away')"),
        UniqueConstraint("game_id", "home_away", name="uq_game_homeaway"),
    )

    game = relationship("Game", back_populates="teams")
    team = relationship("Team")

# PLAYER_STATS
class PlayerPerformance(Base):
    __tablename__ = "player_stats"
    game_id: Mapped[int] = mapped_column(ForeignKey("games.game_id"), primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), index=True)

    position: Mapped[str | None] = mapped_column(String(8))

    pass_yd: Mapped[int] = mapped_column(default=0)
    pass_td: Mapped[int] = mapped_column(default=0)
    pass_int: Mapped[int] = mapped_column(default=0)
    rush_yd: Mapped[int] = mapped_column(default=0)
    rush_td: Mapped[int] = mapped_column(default=0)
    rec_yd: Mapped[int] = mapped_column(default=0)
    rec_td: Mapped[int] = mapped_column(default=0)
    receptions: Mapped[int] = mapped_column(default=0)
    fumbles_lost: Mapped[int] = mapped_column(default=0)

    fantasy_points: Mapped[float] = mapped_column(Numeric(6,2), default=0)  # optional cache
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    game = relationship("Game", back_populates="performances")
    player = relationship("Player")
    team = relationship("Team")

# SCORING_RULES
class ScoringRule(Base):
    __tablename__ = "scoring_rules"
    scoring_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)

    pass_yd: Mapped[float] = mapped_column(Numeric(6,3), default=0.04)
    pass_td: Mapped[float] = mapped_column(Numeric(6,3), default=4.0)
    pass_int: Mapped[float] = mapped_column(Numeric(6,3), default=-2.0)
    rush_yd: Mapped[float] = mapped_column(Numeric(6,3), default=0.1)
    rush_td: Mapped[float] = mapped_column(Numeric(6,3), default=6.0)
    rec_yd: Mapped[float] = mapped_column(Numeric(6,3), default=0.1)
    rec_td: Mapped[float] = mapped_column(Numeric(6,3), default=6.0)
    reception: Mapped[float] = mapped_column(Numeric(6,3), default=0.5)
    fumble_lost: Mapped[float] = mapped_column(Numeric(6,3), default=-2.0)
