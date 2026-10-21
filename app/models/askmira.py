from sqlalchemy import Column, DateTime, Integer, String

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


class AskmiraConnection(Base):
    """This server's connection to AskMira, made with a connection key — no .env edit.

    One row (id 1). The platform super admin generates a key here; it carries a
    secret derived from SECRET_KEY and a random nonce, and only the nonce is
    stored, so the database never holds the secret itself. The key is "pending"
    until AskMira proves it holds the secret with a signed handshake, and then
    becomes the active connection. ASKMIRA_SHARED_SECRET stays the fallback.
    """

    __tablename__ = "askmira_connection"

    id = Column(Integer, primary_key=True)
    active_nonce = Column(String(64), nullable=True)
    connected_at = Column(DateTime, nullable=True)
    pending_nonce = Column(String(64), nullable=True)
    pending_expires_at = Column(DateTime, nullable=True)
    generated_by_user_id = Column(Integer, nullable=True)
