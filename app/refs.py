"""
Opaque, unforgeable references for object ids in page URLs.

The admin UI used to put raw sequential ids in the address bar
(/job-openings/1/candidates, /evaluation/48), which invites people to change
the number. A ref encodes (kind, id) as 18 base62 characters:

    9-char tag  = HMAC(key, kind|id) reduced mod 62**9   (~53 bits)
    9-char body = (id + pad) mod 62**9, pad derived from the tag

so neighbouring ids produce unrelated-looking refs, a job ref can't be used as
a candidate ref, and a ref can't be produced without the server key.

Refs are presentation only — authorization is still enforced on every request
by app.authz. The API accepts either a ref or a plain numeric id for the same
parameter, so an older frontend keeps working during a deploy.
"""
import hashlib
import hmac
from typing import Annotated, Optional

from pydantic import BeforeValidator

from app.config import settings

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
_PART_CHARS = 9
_SPACE = 62 ** _PART_CHARS  # both halves use the full 9-char range, so no tell-tale leading zeros
REF_LENGTH = 2 * _PART_CHARS
_MAX_ID = 1 << 48

KINDS = ("job", "candidate", "org")


def _key() -> bytes:
    raw = settings.URL_REF_KEY or settings.SECRET_KEY
    return hashlib.sha256(b"anytimehire:url-ref:v1\x00" + raw.encode("utf-8")).digest()


def _mac(data: bytes) -> bytes:
    return hmac.new(_key(), data, hashlib.sha256).digest()


def _b62(n: int, width: int) -> str:
    out = []
    while n:
        n, r = divmod(n, 62)
        out.append(_ALPHABET[r])
    s = "".join(reversed(out)) or "0"
    return s.rjust(width, "0")


def _unb62(s: str) -> Optional[int]:
    n = 0
    for c in s:
        v = _INDEX.get(c)
        if v is None:
            return None
        n = n * 62 + v
    return n


def _tag(kind: str, obj_id: int) -> str:
    n = int.from_bytes(_mac(f"tag|{kind}|{obj_id}".encode())[:8], "big") % _SPACE
    return _b62(n, _PART_CHARS)


def _pad(kind: str, tag: str) -> int:
    return int.from_bytes(_mac(f"pad|{kind}|{tag}".encode())[:8], "big") % _SPACE


def encode_ref(kind: str, obj_id: Optional[int]) -> Optional[str]:
    """Ref for (kind, id); None when id is None."""
    if obj_id is None:
        return None
    if kind not in KINDS:
        raise ValueError(f"unknown ref kind: {kind}")
    if isinstance(obj_id, bool) or not isinstance(obj_id, int) or not 0 <= obj_id < _MAX_ID:
        raise ValueError("id out of range for a ref")
    tag = _tag(kind, obj_id)
    return tag + _b62((obj_id + _pad(kind, tag)) % _SPACE, _PART_CHARS)


def decode_ref(kind: str, ref: Optional[str]) -> Optional[int]:
    """The id a ref encodes, or None when the ref is malformed or forged."""
    if kind not in KINDS or not isinstance(ref, str) or len(ref) != REF_LENGTH:
        return None
    tag = ref[:_PART_CHARS]
    body = _unb62(ref[_PART_CHARS:])
    if _unb62(tag) is None or body is None:
        return None
    obj_id = (body - _pad(kind, tag)) % _SPACE
    if obj_id >= _MAX_ID or not hmac.compare_digest(tag, _tag(kind, obj_id)):
        return None
    return obj_id


def _id_or_ref(kind: str):
    def _coerce(value):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            s = value.strip()
            if s.isascii() and s.isdigit():
                return int(s)  # legacy numeric id — still authorized per request
            decoded = decode_ref(kind, s)
            if decoded is not None:
                return decoded
        raise ValueError(f"Invalid {kind} reference")

    return BeforeValidator(_coerce)


# Use these as the type of path/query/body parameters that identify an object.
JobId = Annotated[int, _id_or_ref("job")]
CandidateId = Annotated[int, _id_or_ref("candidate")]
OrgId = Annotated[int, _id_or_ref("org")]
