from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    # Sized for concurrent interview turns. conduct_turn now releases its
    # connection during the LLM await (commit before the call), so each turn
    # only holds a connection for milliseconds of DB work — but keep headroom
    # for screenshot/violation/recording traffic spikes.
    pool_size=15,
    max_overflow=30,
    pool_timeout=30,
    # Recycle idle connections before typical LB/Postgres idle kill windows
    # so a stale connection never surfaces as a mid-interview 500.
    pool_recycle=1800,
    # Keep bound values (session access tokens, password hashes, resume text)
    # out of exception messages, which end up in logs and error reports.
    hide_parameters=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
