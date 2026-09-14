"""
Text-to-Speech API using edge-tts (Microsoft Neural TTS).

Provides natural Indian-accented English speech for the AI avatar.
Streams MP3 audio for low-latency playback.

This is an anonymous proxy to a third-party service, so it is rate-limited per
client and the text is length-capped. (The interview UI speaks through
/interview/tts, not this route.)
"""

import logging
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
import edge_tts

logger = logging.getLogger(__name__)
limiter = Limiter(key_func=get_remote_address)

router = APIRouter(prefix="/tts")

# Available Indian English voices
VOICES = {
    "neerja": "en-IN-NeerjaNeural",              # Female, natural
    "neerja-expressive": "en-IN-NeerjaExpressiveNeural",  # Female, expressive
    "prabhat": "en-IN-PrabhatNeural",             # Male, natural
}

DEFAULT_VOICE = "en-IN-NeerjaExpressiveNeural"


@router.get("/speak", summary="Convert text to speech with Indian accent")
@limiter.limit("30/minute")
async def speak(
    request: Request,
    text: str = Query(..., min_length=1, max_length=1000, description="Text to speak"),
    voice: str = Query(default="neerja-expressive", max_length=40, description="Voice: neerja, neerja-expressive, prabhat"),
    rate: str = Query(default="+5%", pattern=r"^[+-]\d{1,3}%$", description="Speech rate adjustment"),
    pitch: str = Query(default="+0Hz", pattern=r"^[+-]\d{1,3}Hz$", description="Pitch adjustment"),
):
    """
    Stream MP3 audio of the given text spoken in Indian-accented English.
    Returns audio/mpeg stream that can be played directly.
    """
    voice_id = VOICES.get(voice, DEFAULT_VOICE)

    async def audio_stream():
        comm = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]

    return StreamingResponse(
        audio_stream(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache",
            "Content-Disposition": "inline",
        },
    )


@router.get("/voices", summary="List available TTS voices")
async def list_voices():
    """Return available voice options."""
    return {
        "voices": [
            {"id": "neerja", "name": "Neerja", "gender": "Female", "style": "Natural"},
            {"id": "neerja-expressive", "name": "Neerja Expressive", "gender": "Female", "style": "Expressive"},
            {"id": "prabhat", "name": "Prabhat", "gender": "Male", "style": "Natural"},
        ],
        "default": "neerja-expressive",
    }
