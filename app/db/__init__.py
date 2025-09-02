# app/db/__init__.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from dotenv import load_dotenv

# Load env vars (DATABASE_URL)
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

# Engine / Session (SQLAlchemy 2.x style)
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)

# Shared Base for ORM models
class Base(DeclarativeBase):
    pass

# Dependency helper (FastAPI)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
