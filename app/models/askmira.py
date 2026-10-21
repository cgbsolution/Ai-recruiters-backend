from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

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


class AskmiraOrgConnection(Base):
    """An organization's connection to one AskMira workspace (app.routers.askmira).

    Made when an admin of the organization allows AskMira's connect request.
    Only the nonce is stored: the secret is derived from SECRET_KEY and it, so a
    copy of the database holds no usable credential. One per organization.
    """

    __tablename__ = "askmira_org_connections"

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    secret_nonce = Column(String(64), nullable=False)
    #: Which AskMira workspace, as shown to the admin who allowed it.
    askmira_name = Column(String(200), nullable=True)
    askmira_origin = Column(String(300), nullable=True)
    connected_at = Column(DateTime, nullable=False)
    connected_by_user_id = Column(Integer, nullable=True)


class AskmiraConnectCode(Base):
    """A one-time code from "Allow", good for five minutes, swapped by AskMira's server.

    Only its hash is kept. `challenge` is the SHA-256 of a verifier that only
    AskMira's server holds (PKCE), so a code seen on its way through the browser
    is useless on its own.
    """

    __tablename__ = "askmira_connect_codes"

    code_hash = Column(String(64), primary_key=True)
    organization_id = Column(Integer, nullable=False)
    askmira_tenant_id = Column(String(36), nullable=False)
    askmira_name = Column(String(200), nullable=True)
    askmira_origin = Column(String(300), nullable=True)
    challenge = Column(String(128), nullable=False)
    user_id = Column(Integer, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
