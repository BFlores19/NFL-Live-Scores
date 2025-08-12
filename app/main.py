from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timezone
from app.services.scores import get_scores_cached

app = FastAPI(title="NFL Live Scores (ESPN)")
templates = Jinja2Templates(directory="app/templates")

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
