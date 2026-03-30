"""
Google OAuth2 + Calendar API service for creating Google Meet links.
Each user connects their own Google account; Meet events are created on their calendar.
"""
import logging
import uuid
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def _client_config() -> dict:
    """Build the OAuth client config dict from app settings."""
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


# ── OAuth Flow ──────────────────────────────────────────────────────────────


def build_oauth_url(state: str) -> str:
    """
    Build the Google OAuth2 authorization URL.
    `state` should be a signed token containing the user ID.
    """
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url


def exchange_code_for_tokens(code: str) -> dict:
    """
    Exchange the authorization code for access + refresh tokens.
    Returns {"access_token", "refresh_token", "email"}.
    """
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    flow.fetch_token(code=code)

    creds = flow.credentials
    # Fetch user email via userinfo
    user_service = build("oauth2", "v2", credentials=creds)
    user_info = user_service.userinfo().get().execute()

    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "email": user_info.get("email", ""),
    }


def _get_credentials(access_token: str, refresh_token: str) -> Credentials:
    """Build a Credentials object from stored tokens."""
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )


def refresh_access_token(refresh_token: str) -> str:
    """Use the refresh token to get a new access token."""
    import google.auth.transport.requests

    creds = _get_credentials("", refresh_token)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


# ── Calendar / Meet ─────────────────────────────────────────────────────────


def create_meet_event(
    access_token: str,
    refresh_token: str,
    summary: str,
    description: str,
    start_dt: datetime,
    end_dt: datetime | None = None,
    attendee_email: str = "",
    timezone: str = "Asia/Kolkata",
) -> dict:
    """
    Create a Google Calendar event with an auto-generated Google Meet link.
    Returns {"meet_link", "event_id", "new_access_token" (if refreshed)}.
    """
    if end_dt is None:
        end_dt = start_dt + timedelta(hours=1)

    creds = _get_credentials(access_token, refresh_token)
    new_access_token = None

    # Build event body
    event_body = {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": timezone,
        },
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    if attendee_email:
        event_body["attendees"] = [{"email": attendee_email}]

    try:
        service = build("calendar", "v3", credentials=creds)
        event = (
            service.events()
            .insert(
                calendarId="primary",
                body=event_body,
                conferenceDataVersion=1,
                sendUpdates="all",
            )
            .execute()
        )
    except HttpError as e:
        if e.resp.status == 401:
            # Token expired — refresh and retry
            logger.info("Google token expired, refreshing...")
            new_token = refresh_access_token(refresh_token)
            new_access_token = new_token
            creds = _get_credentials(new_token, refresh_token)
            service = build("calendar", "v3", credentials=creds)
            event = (
                service.events()
                .insert(
                    calendarId="primary",
                    body=event_body,
                    conferenceDataVersion=1,
                    sendUpdates="all",
                )
                .execute()
            )
        else:
            raise

    # Extract Meet link from the created event
    meet_link = ""
    conference_data = event.get("conferenceData", {})
    entry_points = conference_data.get("entryPoints", [])
    for ep in entry_points:
        if ep.get("entryPointType") == "video":
            meet_link = ep.get("uri", "")
            break

    result = {
        "meet_link": meet_link,
        "event_id": event.get("id", ""),
    }
    if new_access_token:
        result["new_access_token"] = new_access_token

    logger.info("Google Meet event created: %s (meet: %s)", event.get("id"), meet_link)
    return result
