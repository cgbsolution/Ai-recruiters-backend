"""
Text-to-Speech API using edge-tts (Microsoft Neural TTS).

Provides natural Indian-accented English speech for the AI avatar.
Streams MP3 audio for low-latency playback.
"""

import logging
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
import edge_tts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tts")

# Available Indian English voices
VOICES = {
    "neerja": "en-IN-NeerjaNeural",              # Female, natural
    "neerja-expressive": "en-IN-NeerjaExpressiveNeural",  # Female, expressive
    "prabhat": "en-IN-PrabhatNeural",             # Male, natural
}

DEFAULT_VOICE = "en-IN-NeerjaExpressiveNeural"


@router.get("/speak", summary="Convert text to speech with Indian accent")
async def speak(
    text: str = Query(..., description="Text to speak"),
    voice: str = Query(default="neerja-expressive", description="Voice: neerja, neerja-expressive, prabhat"),
    rate: str = Query(default="+5%", description="Speech rate adjustment"),
    pitch: str = Query(default="+0Hz", description="Pitch adjustment"),
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
