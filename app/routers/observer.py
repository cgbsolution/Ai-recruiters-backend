"""Live-observer endpoints.

Three transports:

1. REST  GET  /observe/{token}                   resolve token → connection info
2. SSE   GET  /observe/{token}/stream            transcript + status feed
3. WS    /observe/{token}/ws                     observer signal channel (subscriber)
4. WS    /observe/publish/{session_id}            candidate signal channel (publisher)

The observer link is intentionally token-only (no login). Observers cannot send
audio, video, chat, or any other data into the candidate's interview — the
publisher RTCPeerConnection is configured browser-side with sendonly tracks
and the observer side rejects all addTrack attempts.

The candidate authenticates the publish WS by passing their public
`InterviewToken.token` as a query string. We verify it matches the session's
job before accepting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.models.candidate import Candidate
from app.models.interview import (
    CodingSubmission,
    InterviewMessage,
    InterviewSession,
    InterviewToken,
    SessionStatus,
)
from app.models.job import Job
from app.models.observer import ObserverToken
from app.services.observer_broker import broker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/observe", tags=["Live Observer"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ice_servers() -> list[dict]:
    """Build the ICE server list given to both publisher and observer browsers."""
    servers: list[dict] = []
    stun_urls = [u.strip() for u in (settings.OBSERVER_STUN_URLS or "").split(",") if u.strip()]
    if stun_urls:
        servers.append({"urls": stun_urls})
    if settings.OBSERVER_TURN_URL and settings.OBSERVER_TURN_USERNAME:
        servers.append({
            "urls": settings.OBSERVER_TURN_URL,
            "username": settings.OBSERVER_TURN_USERNAME,
            "credential": settings.OBSERVER_TURN_CREDENTIAL,
        })
    return servers


def _resolve_token(token: str, db: Session) -> ObserverToken:
    obj = db.query(ObserverToken).filter(ObserverToken.token == token).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="Observer link not found")
    if obj.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Observer link has been revoked")
    if obj.expires_at and obj.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Observer link has expired")
    return obj


def _active_session_for(observer: ObserverToken, db: Session) -> Optional[InterviewSession]:
    """Return the candidate's interview session if one is in progress / completed.

    Observer tokens are minted at scheduling time (before the session exists),
    so we look up the candidate's most recent session every time. This way the
    same observer link works once the candidate finally starts.
    """
    if observer.session_id:
        s = db.query(InterviewSession).filter(InterviewSession.id == observer.session_id).first()
        if s is not None:
            return s
    return (
        db.query(InterviewSession)
        .filter(InterviewSession.candidate_id == observer.candidate_id)
        .order_by(InterviewSession.created_at.desc())
        .first()
    )


def _serialize_message(m: InterviewMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "question_index": m.question_index,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _serialize_session_state(s: InterviewSession) -> dict:
    return {
        "session_id": s.id,
        "status": s.status.value if hasattr(s.status, "value") else str(s.status),
        "current_question_index": s.current_question_index or 0,
        "total_questions": s.total_questions or 0,
        "violation_count": s.violation_count or 0,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
    }


# ── 1. REST resolve ──────────────────────────────────────────────────────────

@router.get("/{token}")
def get_observer_info(token: str, db: Session = Depends(get_db)):
    """Resolve an observer token. Returns candidate display info, current
    session state (if any), and the ICE server config the observer's browser
    needs to set up its peer connection."""
    obs = _resolve_token(token, db)
    obs.view_count = (obs.view_count or 0) + 1
    db.commit()

    candidate = db.query(Candidate).filter(Candidate.id == obs.candidate_id).first()
    job = db.query(Job).filter(Job.id == obs.job_id).first()
    session = _active_session_for(obs, db)

    # Interview mode (avatar / manual / default), so the observer page can
    # mirror exactly what the candidate sees.
    settings_obj = job.interview_settings if job else None
    mode = "default"
    if settings_obj is not None:
        if settings_obj.virtual_avatar:
            mode = "avatar"
        elif settings_obj.manual_interview:
            mode = "manual"
    code_editor_required = bool(settings_obj.code_editor_required) if settings_obj else False

    # Latest coding submission (if any) — gives observer immediate context if
    # they joined mid-coding-challenge.
    latest_coding = None
    if session is not None:
        cs = (
            db.query(CodingSubmission)
            .filter(CodingSubmission.session_id == session.id)
            .order_by(CodingSubmission.submitted_at.desc())
            .first()
        )
        if cs is not None:
            latest_coding = {
                "question_index": cs.question_index,
                "title": cs.title,
                "description": cs.description,
                "difficulty": cs.difficulty,
                "language": cs.language,
                "code": cs.code,
                "test_cases_total": cs.test_cases_total,
                "test_cases_passed": cs.test_cases_passed,
                "submitted_at": cs.submitted_at.isoformat() if cs.submitted_at else None,
            }

    return {
        "candidate": {
            "name": candidate.name if candidate else "Candidate",
            "role": candidate.role if candidate else None,
            "interview_status": candidate.interview_status if candidate else None,
        },
        "job": {
            "title": job.title if job else "Interview",
            "id": obs.job_id,
        },
        "session": _serialize_session_state(session) if session else None,
        "room_name": obs.room_name,
        "publisher_present": broker.publisher_present(session.id) if session else False,
        "frame_publisher_present": broker.frame_publisher_present(session.id) if session else False,
        "ice_servers": _ice_servers(),
        "interview_mode": mode,
        "code_editor_required": code_editor_required,
        "latest_coding_submission": latest_coding,
    }


# ── 2. SSE transcript + status stream ────────────────────────────────────────

@router.get("/{token}/stream")
async def stream(token: str, request: Request, db: Session = Depends(get_db)):
    obs = _resolve_token(token, db)
    session = _active_session_for(obs, db)
    if session is None:
        # Candidate hasn't started yet. Poll-friendly empty stream that
        # waits and re-checks every few seconds, so the observer page can
        # subscribe early and start receiving once the candidate joins.
        async def waiting_gen():
            yield "event: waiting\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(3)
                with SessionLocal() as db2:
                    obs2 = db2.query(ObserverToken).filter(ObserverToken.id == obs.id).first()
                    if obs2 is None or obs2.revoked_at is not None:
                        yield "event: revoked\ndata: {}\n\n"
                        return
                    s = _active_session_for(obs2, db2)
                    if s is not None:
                        # Hand off to the live generator by returning a
                        # synthetic 'session_ready' event — the client will
                        # reconnect to pick up the live stream.
                        yield f"event: session_ready\ndata: {json.dumps({'session_id': s.id})}\n\n"
                        return
                yield ": ping\n\n"
        return StreamingResponse(waiting_gen(), media_type="text/event-stream")

    session_id = session.id
    queue = await broker.subscribe_events(session_id)

    # Take a snapshot of existing messages so the observer sees everything
    # that's happened so far, not just future messages.
    snapshot_messages = [
        _serialize_message(m)
        for m in db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session_id)
        .order_by(InterviewMessage.created_at)
        .all()
    ]
    snapshot_state = _serialize_session_state(session)

    obs_id = obs.id
    async def event_gen():
        ticks_since_revoke_check = 0
        try:
            yield f"event: snapshot\ndata: {json.dumps({'state': snapshot_state, 'messages': snapshot_messages})}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    # Keep-alive — proxies often drop idle SSE after 30-60s.
                    yield ": keepalive\n\n"
                    ticks_since_revoke_check += 1
                    # Every ~30s, re-read the token from DB to honour revoke.
                    if ticks_since_revoke_check >= 3:
                        ticks_since_revoke_check = 0
                        with SessionLocal() as db2:
                            row = db2.query(ObserverToken).filter(ObserverToken.id == obs_id).first()
                            if row is None or row.revoked_at is not None or (
                                row.expires_at and row.expires_at < datetime.utcnow()
                            ):
                                yield "event: revoked\ndata: {}\n\n"
                                return
                    continue
                evt_type = evt.get("type", "message")
                yield f"event: {evt_type}\ndata: {json.dumps(evt)}\n\n"
        finally:
            await broker.unsubscribe_events(session_id, queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ── Helper: WS-side token check (raise → close 4xxx) ────────────────────────

async def _ws_resolve_observer(websocket: WebSocket, token: str) -> Optional[ObserverToken]:
    with SessionLocal() as db:
        obj = db.query(ObserverToken).filter(ObserverToken.token == token).first()
        if obj is None:
            await websocket.close(code=4404, reason="observer link not found")
            return None
        if obj.revoked_at is not None:
            await websocket.close(code=4410, reason="revoked")
            return None
        if obj.expires_at and obj.expires_at < datetime.utcnow():
            await websocket.close(code=4410, reason="expired")
            return None
        # Detach so the caller can use it after the session closes.
        db.expunge(obj)
        return obj


async def _ws_session_for_observer(observer: ObserverToken) -> Optional[InterviewSession]:
    with SessionLocal() as db:
        if observer.session_id:
            s = db.query(InterviewSession).filter(InterviewSession.id == observer.session_id).first()
            if s is not None:
                db.expunge(s)
                return s
        s = (
            db.query(InterviewSession)
            .filter(InterviewSession.candidate_id == observer.candidate_id)
            .order_by(InterviewSession.created_at.desc())
            .first()
        )
        if s is not None:
            db.expunge(s)
        return s


# ── 3. Observer signal WS (subscribe-only) ──────────────────────────────────

@router.websocket("/{token}/ws")
async def observer_ws(websocket: WebSocket, token: str):
    """Observer's WebRTC signalling socket.

    Protocol (JSON messages, both directions):
      → server sends   {"type":"ready", "observer_id":"..."}
      → server sends   {"type":"offer"|"answer"|"ice", "sdp"|"candidate":...}    (forwarded from publisher)
      → server sends   {"type":"publisher_gone"}
      ← client sends   {"type":"answer"|"ice", ...}                              (forwarded to publisher)

    The server enforces direction: an observer can ONLY send {answer, ice}.
    Anything else is dropped silently. This guarantees they cannot inject
    media, data-channel content, or fake "offer" messages.
    """
    await websocket.accept()
    obs = await _ws_resolve_observer(websocket, token)
    if obs is None:
        return

    session = await _ws_session_for_observer(obs)
    if session is None:
        await websocket.send_json({"type": "no_session"})
        await websocket.close(code=4040, reason="candidate not in session")
        return

    session_id = session.id
    observer_id = secrets.token_hex(8)
    inbox = await broker.register_observer(session_id, observer_id)
    await websocket.send_json({"type": "ready", "observer_id": observer_id})

    async def relay_to_publisher():
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                if t not in ("answer", "ice"):
                    continue  # silently drop anything else (read-only enforcement)
                broker.deliver_to_publisher(session_id, {
                    "type": t,
                    "from_observer": observer_id,
                    "payload": msg.get("payload"),
                })
        except WebSocketDisconnect:
            pass

    async def relay_to_observer():
        try:
            while True:
                msg = await inbox.get()
                await websocket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass

    t1 = asyncio.create_task(relay_to_publisher())
    t2 = asyncio.create_task(relay_to_observer())
    try:
        # Whichever task ends first signals the connection is dead — cancel
        # the other so the finally block (and observer_leave broadcast) runs
        # immediately.
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        for p in pending:
            try:
                await p
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.exception("observer ws error: %s", e)
    finally:
        await broker.unregister_observer(session_id, observer_id)
        try:
            await websocket.close()
        except Exception:
            pass


# ── 4. Publisher (candidate) signal WS ───────────────────────────────────────

@router.websocket("/publish/{session_id}")
async def publisher_ws(websocket: WebSocket, session_id: int, interview_token: str = Query(...)):
    """Candidate's WebRTC signalling socket. Authenticated by the public
    interview token they're already holding."""
    await websocket.accept()

    with SessionLocal() as db:
        sess = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
        if sess is None:
            await websocket.close(code=4404, reason="session not found")
            return
        tok = (
            db.query(InterviewToken)
            .filter(InterviewToken.token == interview_token, InterviewToken.is_active == True)
            .first()
        )
        if tok is None or tok.job_id != sess.job_id:
            await websocket.close(code=4401, reason="bad interview token")
            return

    inbox = await broker.register_publisher(session_id)
    await websocket.send_json({"type": "ready", "ice_servers": _ice_servers()})

    async def relay_to_observers():
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                if t not in ("offer", "ice"):
                    continue
                target = msg.get("to_observer")
                if not target:
                    continue
                broker.deliver_to_observer(session_id, target, {
                    "type": t,
                    "payload": msg.get("payload"),
                })
        except WebSocketDisconnect:
            pass

    async def relay_to_publisher():
        try:
            while True:
                msg = await inbox.get()
                if msg.get("type") == "_replaced":
                    return
                await websocket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass

    t1 = asyncio.create_task(relay_to_observers())
    t2 = asyncio.create_task(relay_to_publisher())
    try:
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        for p in pending:
            try:
                await p
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.exception("publisher ws error: %s", e)
    finally:
        await broker.unregister_publisher(session_id, inbox)
        try:
            await websocket.close()
        except Exception:
            pass


# ── 5. JPEG frame relay (always-works fallback to WebRTC) ──────────────────
#
# The candidate's browser captures webcam frames (~3-5 fps, ~30 KB JPEG each)
# and sends them as binary WebSocket messages. The server fans them out to
# every connected observer. Works through any firewall — no NAT traversal,
# no TURN, no third-party. Bandwidth: ~150 KB/s × N observers on the server.
# Trade-off: lower frame rate than WebRTC, no audio.


@router.websocket("/publish-frames/{session_id}")
async def publish_frames_ws(
    websocket: WebSocket,
    session_id: int,
    interview_token: str = Query(...),
):
    """Candidate frame publisher. Receives binary JPEG blobs and broadcasts
    them to all observers on this session."""
    await websocket.accept()

    with SessionLocal() as db:
        sess = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
        if sess is None:
            await websocket.close(code=4404, reason="session not found")
            return
        tok = (
            db.query(InterviewToken)
            .filter(InterviewToken.token == interview_token, InterviewToken.is_active == True)
            .first()
        )
        if tok is None or tok.job_id != sess.job_id:
            await websocket.close(code=4401, reason="bad interview token")
            return

    await broker.set_frame_publisher_present(session_id, True)
    try:
        while True:
            msg = await websocket.receive()
            # Accept either binary frames (preferred) or a text "ping".
            if "bytes" in msg and msg["bytes"] is not None:
                # Broker errors MUST NOT disconnect the candidate. The frame
                # relay is best-effort eye-candy for observers; the
                # candidate's own interview (audio/video recording, Q&A,
                # screenshots) does not depend on this.
                try:
                    broker.broadcast_frame(session_id, msg["bytes"])
                except Exception as broker_exc:
                    logger.warning(
                        "publish_frames broker.broadcast_frame failed for "
                        "session %s (ignoring, candidate keeps going): %s",
                        session_id, broker_exc,
                    )
            elif "text" in msg and msg["text"] is not None:
                # Ignore — used as keepalive only.
                continue
            elif msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("publish_frames ws error: %s", e)
    finally:
        await broker.set_frame_publisher_present(session_id, False)
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/{token}/frames")
async def observer_frames_ws(websocket: WebSocket, token: str):
    """Observer frame subscriber. Receives binary JPEG blobs broadcast by the
    candidate."""
    await websocket.accept()
    obs = await _ws_resolve_observer(websocket, token)
    if obs is None:
        return

    session = await _ws_session_for_observer(obs)
    if session is None:
        await websocket.send_json({"type": "no_session"})
        await websocket.close(code=4040, reason="candidate not in session")
        return

    session_id = session.id
    queue = await broker.register_frame_subscriber(session_id)

    async def relay_frames():
        try:
            while True:
                frame = await queue.get()
                await websocket.send_bytes(frame)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def watch_disconnect():
        try:
            while True:
                # Block until the client says something or disconnects.
                # Observers don't send anything meaningful — anything we
                # receive is dropped, but we need to detect disconnect.
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            return

    t1 = asyncio.create_task(relay_frames())
    t2 = asyncio.create_task(watch_disconnect())
    try:
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        for p in pending:
            try:
                await p
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.exception("observer frames ws error: %s", e)
    finally:
        await broker.unregister_frame_subscriber(session_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass


# Revoke is handled at /candidates/{id}/observer-link/revoke (auth-protected).
