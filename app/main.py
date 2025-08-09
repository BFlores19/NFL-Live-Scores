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
async def scores(date: str | None = None):
    # optional ?date=YYYYMMDD
    if date and (len(date) != 8 or not date.isdigit()):
        raise HTTPException(status_code=400, detail="date must be YYYYMMDD")
    data = await get_scores_cached(date)
    return JSONResponse(data)
