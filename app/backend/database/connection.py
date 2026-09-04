from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # this module can be imported before src.main runs load_dotenv

# Get the backend directory path
BACKEND_DIR = Path(__file__).parent.parent
DATABASE_PATH = BACKEND_DIR / "hedge_fund.db"

# Database configuration: DATABASE_URL env (e.g. Neon Postgres) wins;
# fall back to a local SQLite file.
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{DATABASE_PATH}"

# Create SQLAlchemy engine
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False}  # Needed for SQLite
    )
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class for models
Base = declarative_base()

# Dependency for FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close() 