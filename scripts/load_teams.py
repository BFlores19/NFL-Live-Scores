from app.db import SessionLocal
from app.models import Team

TEAMS = [
    ("ARI","Arizona Cardinals"), ("ATL","Atlanta Falcons"), ("BAL","Baltimore Ravens"),
    ("BUF","Buffalo Bills"), ("CAR","Carolina Panthers"), ("CHI","Chicago Bears"),
    ("CIN","Cincinnati Bengals"), ("CLE","Cleveland Browns"), ("DAL","Dallas Cowboys"),
    ("DEN","Denver Broncos"), ("DET","Detroit Lions"), ("GB","Green Bay Packers"),
    ("HOU","Houston Texans"), ("IND","Indianapolis Colts"), ("JAX","Jacksonville Jaguars"),
    ("KC","Kansas City Chiefs"), ("LV","Las Vegas Raiders"), ("LAC","Los Angeles Chargers"),
    ("LAR","Los Angeles Rams"), ("MIA","Miami Dolphins"), ("MIN","Minnesota Vikings"),
    ("NE","New England Patriots"), ("NO","New Orleans Saints"), ("NYG","New York Giants"),
    ("NYJ","New York Jets"), ("PHI","Philadelphia Eagles"), ("PIT","Pittsburgh Steelers"),
    ("SF","San Francisco 49ers"), ("SEA","Seattle Seahawks"), ("TB","Tampa Bay Buccaneers"),
    ("TEN","Tennessee Titans"), ("WSH","Washington Commanders"),
]

db = SessionLocal()
try:
    for abbr, name in TEAMS:
        row = db.query(Team).filter_by(abbr=abbr).one_or_none()
        if row:
            row.name = name
        else:
            db.add(Team(abbr=abbr, name=name))
    db.commit()
finally:
    db.close()
