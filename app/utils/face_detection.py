"""Server-side face detection for proctoring (screenshots + secondary camera).

This replaces the old in-browser MediaPipe face detector, which loaded ~300 MB
of WASM + model INTO the candidate's tab and was the primary driver of the
'Aw, Snap! STATUS_BREAKPOINT' out-of-memory crash. Running detection
server-side keeps the candidate's tab light while preserving the proctoring
signal (face count per screenshot).

The Haar cascade ships inside the OpenCV wheel, so there is no model to
download — but only up to OpenCV 4.x. **OpenCV 5.0 removed
``cv2.CascadeClassifier`` and stopped shipping the haarcascade XMLs**, so an
unpinned `opencv-python-headless` upgrade silently broke detection in
production and logged

    [Proctor] server-side face detection failed: module 'cv2' has no attribute
    'CascadeClassifier'

once per screenshot — roughly every 14 seconds, for the length of every
interview. requirements.txt now pins `<5`; this module additionally degrades
gracefully: the classifier is built once, and if it cannot be built the reason
is logged ONCE and detection reports "unknown" (None) from then on instead of
raising per frame.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Sequence

logger = logging.getLogger("app.proctor")

_cascade = None
_cascade_unavailable = False
_lock = threading.Lock()


def get_face_cascade():
    """The shared Haar cascade classifier, or None if OpenCV can't provide one.

    Built lazily on first use (so it doesn't slow cold starts) and guarded by a
    lock, since screenshots are analysed from a threadpool.
    """
    global _cascade, _cascade_unavailable

    if _cascade is not None or _cascade_unavailable:
        return _cascade

    with _lock:
        if _cascade is not None or _cascade_unavailable:
            return _cascade
        try:
            import cv2

            xml_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(xml_path)
            if cascade.empty():
                raise RuntimeError(f"cascade file missing or unreadable: {xml_path}")
            _cascade = cascade
        except Exception as exc:
            _cascade_unavailable = True
            logger.warning(
                "[Proctor] server-side face detection unavailable (%s: %s). Screenshots "
                "are still captured and stored; face_count stays unknown. Requires "
                "opencv-python-headless<5 — OpenCV 5 dropped CascadeClassifier and the "
                "bundled haarcascade XMLs.",
                type(exc).__name__,
                exc,
            )
        return _cascade


def detect_faces(
    gray, min_neighbors: int = 6, min_size: tuple[int, int] = (60, 60)
) -> Optional[Sequence]:
    """Face bounding boxes in a grayscale image, or None if detection couldn't run.

    Defaults are a stricter pass than OpenCV's (minNeighbors=6, minSize=60) to
    keep false positives low, so background objects aren't miscounted as faces.
    """
    cascade = get_face_cascade()
    if cascade is None:
        return None
    try:
        return cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=min_neighbors, minSize=min_size
        )
    except Exception as exc:
        logger.warning("[Proctor] face detection failed on frame: %s", exc)
        return None


def count_faces_in_image(path: str) -> Optional[int]:
    """Count faces in a screenshot file. Returns an int, or None if detection
    couldn't run (so the caller can fall back to 'unknown')."""
    if get_face_cascade() is None:
        return None
    try:
        import cv2

        img = cv2.imread(path)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    except Exception as exc:
        logger.warning("[Proctor] could not read screenshot %s: %s", path, exc)
        return None

    faces = detect_faces(gray)
    return None if faces is None else int(len(faces))
