"""
Utility script to backfill the database with games and fantasy scores
for past NFL seasons.

This script iterates through the years 2020–2024 and calls the
`/api/weeks/{year}/{week}/ingest?score=true` endpoint on your running
FastAPI server.  The ingest endpoint saves all games for the given
week into the database and (when `score=true`) computes Full‑PPR
fantasy points for every player.  Running this script once will
populate your database with historical data so that the web
application can serve top‑five fantasy performers without re‑scoring
older games on every page load.

Usage:

    # Activate your virtualenv and start the FastAPI server in one
    # shell (e.g. `uvicorn app.main:app --reload`)
    # Then, in another shell, run this script:
    python ingest_past_seasons.py

You can adjust the `BASE_URL`, `START_YEAR`, `END_YEAR`, and
`MAX_WEEK` constants below to suit your environment or desired
historical range.
"""

import asyncio
import httpx

# Base URL for your running FastAPI server.  Change the port or host as
# necessary if your server is bound elsewhere.
BASE_URL = "http://localhost:8000"

# First and last seasons (inclusive) to ingest.
START_YEAR = 2020
END_YEAR = 2024

# Maximum overall week number to ingest.  The NFL Live Scores project
# treats overall week numbers 1–3 as preseason and 4–21 as regular
# season.  Setting this to 21 ensures you cover all preseason and
# regular season weeks.
MAX_WEEK = 21

# Mapping of NFL seasons to the start date of the preseason.  These
# dates correspond to the annual Hall of Fame Game, which kicks off
# each preseason.  You can update or extend this mapping as new
# seasons occur.
#
# Evidence for these dates:
# - 2021: An NFL.com article announcing the 2021 preseason lists the
#   Hall of Fame Game on Aug. 5【599590515221947†L12-L16】.
# - 2022: A 2022 Hall of Fame Game preview notes that the Raiders
#   versus Jaguars game takes place on Thursday, Aug. 4 at 8 p.m. ET【398150336081662†L26-L29】.
# - 2023: A CBSSports.com preview of the Browns vs. Jets Hall of Fame
#   Game states that it is scheduled for Aug. 3【336738695351510†L770-L778】.
# - 2024: The Pro Football Hall of Fame announces that the Bears and
#   Texans will play the 2024 Hall of Fame Game on Aug. 1【203968517851244†L85-L87】.
SEASON_START_DATES: dict[int, str] = {
    2021: "2021-08-05",
    2022: "2022-08-04",
    2023: "2023-08-03",
    2024: "2024-08-01",
    # 2020 preseason was cancelled due to the COVID‑19 pandemic; the
    # regular season began in September.  We still include a nominal
    # date for completeness.
    2020: "2020-08-06",
}

# Mapping of NFL seasons to the overall week number that corresponds
# to the Hall of Fame Game in the current backend implementation.
# Historically, preseason weeks occupy overall weeks 1–3 and the
# Hall of Fame Game has fallen in overall week 4.  If your backend’s
# week numbering changes in the future, adjust these values
# accordingly.  Seasons not listed will default to week 4.
SEASON_FIRST_OVERALL_WEEK: dict[int, int] = {
    2020: 4,
    2021: 4,
    2022: 4,
    2023: 4,
    2024: 4,
}


async def ingest_week(client: httpx.AsyncClient, year: int, week: int) -> None:
    """Call the ingest endpoint for a given year and week, scoring games."""
    url = f"{BASE_URL}/api/weeks/{year}/{week}/ingest?score=true"
    try:
        resp = await client.post(url)
    except Exception as exc:
        print(f"Error connecting to {url}: {exc}")
        return
    if resp.status_code != 200:
        print(f"Ingest {year} week {week} failed with status {resp.status_code}: {resp.text}")
    else:
        body = resp.json()
        errors = body.get("errors") or []
        if errors:
            print(f"Ingest {year} week {week} completed with errors: {errors}")
        else:
            print(f"Ingest {year} week {week} successful: saved {len(body.get('saved', []))} games, scored {len(body.get('scored', []))}.")


async def main() -> None:
    async with httpx.AsyncClient(timeout=120) as client:
        for year in range(START_YEAR, END_YEAR + 1):
            # Determine the first overall week to ingest for this season.  This
            # typically corresponds to the Hall of Fame Game (overall week 4)
            # but can be adjusted per season via SEASON_FIRST_OVERALL_WEEK.
            start_week = SEASON_FIRST_OVERALL_WEEK.get(year, 4)
            for week in range(start_week, MAX_WEEK + 1):
                await ingest_week(client, year, week)


if __name__ == "__main__":
    asyncio.run(main())