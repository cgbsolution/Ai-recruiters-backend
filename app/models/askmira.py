from sqlalchemy import Column, DateTime, String

from app.database import Base


class AskmiraLaunch(Base):
    """A launch token from AskMira that has been used.

    Each AskMira launch link signs one person in once: its `jti` is recorded
    here, and a second use is refused. Rows past `expires_at` are only kept
    until the next launch prunes them — the token itself is dead by then.
    """

    __tablename__ = "askmira_launches"

    jti = Column(String(64), primary_key=True)
    expires_at = Column(DateTime, nullable=False, index=True)
