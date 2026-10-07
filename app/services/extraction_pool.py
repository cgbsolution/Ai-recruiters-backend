"""
Resume text extraction in separate worker processes, with a hard timeout.

Bulk uploads used to read PDFs in a thread of the API process. pdfminer is
pure Python, so every PDF held the GIL away from the event loop that serves
live interviews, and a malformed PDF could spin for many minutes — threads
cannot be killed, so one bad file pinned a worker (and, in AI mode, the only
parse slot) until the whole upload looked frozen.

Here each file is read in a child process. A file that runs past its timeout
gets its worker process killed and the pool rebuilt; a file that crashes its
worker breaks only the pool, never the API server.

Single-process assumption: the pool lives in the uvicorn process (the app
runs one worker), like the bulk queue in app.routers.resume_database.
"""

import asyncio
import concurrent.futures
import logging
import multiprocessing
import os
import threading
from concurrent.futures.process import BrokenProcessPool
from typing import Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# Recycle each worker after this many files, so memory pdfminer keeps between
# documents never accumulates for the life of the server.
_MAX_TASKS_PER_CHILD = 200

_executor: Optional[concurrent.futures.ProcessPoolExecutor] = None


def _init_worker() -> None:
    # Malformed fonts make pdfminer log a WARNING per glyph lookup ("Could not
    # get FontBBox ..."); the text still extracts, the lines are pure noise.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    # Lower priority: reading resumes must never compete with live interviews.
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass
    # Exit with the API process. If it dies without running its shutdown hook
    # (hard kill, dev auto-reload), an idle worker would otherwise wait on its
    # task queue forever, holding its memory.
    parent = multiprocessing.parent_process()
    if parent is not None:
        def _exit_with_parent() -> None:
            parent.join()
            os._exit(0)

        threading.Thread(target=_exit_with_parent, daemon=True).start()


def _extract(file_path: str, fast_parse: bool) -> Tuple[str, Optional[dict]]:
    """Runs in the child: the file's text, plus the regex parse when asked
    (fast mode — keeps the regex work out of the API process too)."""
    from app.services.resume_service import extract_text

    raw_text = extract_text(file_path)
    parsed = None
    if fast_parse and raw_text and len(raw_text.strip()) >= 20:
        from app.services.fast_resume_parser import fast_parse_resume

        parsed = fast_parse_resume(raw_text)
    return raw_text, parsed


def _get_executor() -> concurrent.futures.ProcessPoolExecutor:
    global _executor
    if _executor is None:
        _executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=max(1, settings.RESUME_EXTRACT_WORKERS),
            # spawn, not fork: forking the multi-threaded API process can
            # deadlock the child on a lock another thread held at fork time.
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_worker,
            max_tasks_per_child=_MAX_TASKS_PER_CHILD,
        )
    return _executor


def _discard_executor(executor: concurrent.futures.ProcessPoolExecutor) -> None:
    """Kill every worker of `executor` and forget it; the next call builds a
    fresh pool. ProcessPoolExecutor cannot stop one running task, so a stuck
    file costs the in-flight siblings their worker — extract_text_isolated
    retries those once."""
    global _executor
    if _executor is executor:
        _executor = None
    for proc in list((getattr(executor, "_processes", None) or {}).values()):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


async def extract_text_isolated(file_path: str, fast_parse: bool = False) -> Tuple[str, Optional[dict]]:
    """Text of a resume file (and its regex parse when `fast_parse`), read in
    a worker process. Raises RuntimeError with a user-readable message on a
    timeout or a crashed reader; the extractor's own RuntimeError/ValueError
    messages pass through unchanged."""
    timeout = max(5, settings.RESUME_EXTRACT_TIMEOUT_SECONDS)
    for attempt in (1, 2):
        executor = _get_executor()
        try:
            future = executor.submit(_extract, file_path, fast_parse)
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except asyncio.TimeoutError:
            logger.warning("Resume extraction timed out after %ss: %s", timeout, os.path.basename(file_path))
            _discard_executor(executor)
            raise RuntimeError(
                "Reading this file took too long. It may be damaged or unusually complex — "
                "try saving it again as a PDF or DOCX."
            )
        except BrokenProcessPool:
            # This file's worker died — or a sibling's timeout killed the pool.
            _discard_executor(executor)
            if attempt == 2:
                logger.warning("Resume extraction crashed twice: %s", os.path.basename(file_path))
                raise RuntimeError("This file could not be read. It may be damaged.")
    raise RuntimeError("This file could not be read.")  # unreachable


def shutdown_extraction_pool() -> None:
    """Kill the workers on server shutdown, so a stuck file cannot hold up exit."""
    if _executor is not None:
        _discard_executor(_executor)
