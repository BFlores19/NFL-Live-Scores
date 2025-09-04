# scripts/seed_basic.py (snippet)
from datetime import date
from app.db import SessionLocal
from app.models import Season, ScoringRule

db = SessionLocal()
try:
    if not db.query(Season).filter_by(year=2025).one_or_none():
        db.add(Season(year=2025, pre_w1_start=date(2025, 8, 7), reg_w1_start=date(2025, 9, 4)))

    if not db.query(ScoringRule).filter_by(name="Full PPR").one_or_none():
        db.add(
            ScoringRule(
                name="Full PPR",
                pass_yd=0.04,     # 1 per 25 pass yds
                pass_td=4.0,
                pass_int=-2.0,
                rush_yd=0.1,      # 1 per 10 rush yds
                rush_td=6.0,
                rec_yd=0.1,       # 1 per 10 rec yds
                rec_td=6.0,
                reception=1.0,    # FULL PPR
                fumble_lost=-2.0,
            )
        )
    db.commit()
finally:
    db.close()
