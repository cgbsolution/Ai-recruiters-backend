"""Re-score existing candidates with the current fit-score engine.

Scores are stored on the candidate row, so candidates scored by the previous
engine keep its numbers forever unless something re-runs them. That engine asked
the LLM for one overall percentage and the prompt anchored it, so most in-domain
applicants to a job came back with the identical value (82). This backfills them.

DRY RUN BY DEFAULT — nothing is written unless you pass --apply.

    # See what would change for one job, no writes:
    python rescore_candidates.py --job-id 124

    # Same, but actually write:
    python rescore_candidates.py --job-id 124 --apply

    # Everything (careful — one LLM call per candidate):
    python rescore_candidates.py --all --apply

Each candidate costs one Sonnet call. Run a single job first and eyeball the
before/after table before doing the whole database.
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.candidate import Candidate  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("app.routers.candidates").setLevel(logging.INFO)
log = logging.getLogger("rescore")

# The LLM path is rate-limited by the provider; a handful at a time is plenty
# and keeps us well clear of 429s on a large backfill.
CONCURRENCY = 4


def _current_pct(candidate: Candidate) -> str:
    """Best-effort read of the stored percentage, for the before/after table."""
    import json

    if candidate.fit_score_reasoning:
        try:
            val = json.loads(candidate.fit_score_reasoning).get("fit_percentage")
            if val is not None:
                return str(val)
        except Exception:
            pass
    raw = candidate.resume_fit_score
    if raw:
        return str(raw).replace("%", "")
    return "-"


async def _rescore_one(candidate_id: int, apply: bool) -> tuple[int, str, str, str, str]:
    """Re-score one candidate in its own session. Returns a report row."""
    from app.routers.candidates import _ai_fit_score_for_candidate

    db: Session = SessionLocal()
    try:
        candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
        if not candidate:
            return (candidate_id, "?", "-", "-", "not found")

        name = (candidate.name or candidate.email or "?")[:28]
        before = _current_pct(candidate)

        # _ai_fit_score_for_candidate COMMITS internally. Rolling back after it
        # returns is useless — the transaction is already gone — so a dry run
        # has to stop the commit from happening in the first place. Neutralise
        # commit for the duration of the call and roll the whole thing back at
        # the end; the helper's db.refresh() still works inside the open
        # transaction, so nothing else changes.
        real_commit = db.commit
        if not apply:
            db.commit = lambda: None  # type: ignore[method-assign]
        try:
            result = await _ai_fit_score_for_candidate(candidate, db)
        finally:
            db.commit = real_commit  # type: ignore[method-assign]

        if not result:
            # No resume text or no job — leave the existing score untouched.
            db.rollback()
            return (candidate_id, name, before, "-", "skipped (no resume/job)")

        after = str(result.get("fit_percentage", "?"))
        level = str(result.get("fit_level", "?"))

        if apply:
            db.commit()
            status = f"written ({level})"
        else:
            db.rollback()
            status = f"dry-run ({level})"

        return (candidate_id, name, before, after, status)
    except Exception as exc:
        db.rollback()
        log.exception("candidate=%s failed", candidate_id)
        return (candidate_id, "?", "-", "-", f"ERROR {type(exc).__name__}: {exc}"[:60])
    finally:
        db.close()


async def main() -> int:
    ap = argparse.ArgumentParser(description="Re-score candidates with the current fit-score engine")
    group = ap.add_argument_group("scope")
    group.add_argument("--job-id", type=int, help="only candidates on this job")
    group.add_argument("--all", action="store_true", help="every candidate in the database")
    ap.add_argument("--limit", type=int, help="stop after N candidates")
    ap.add_argument("--only-scored", action="store_true",
                    help="skip candidates that have no score yet (re-score existing ones only)")
    ap.add_argument("--apply", action="store_true", help="write the new scores (default: dry run)")
    args = ap.parse_args()

    if not args.job_id and not args.all:
        ap.error("pass --job-id N or --all")

    db = SessionLocal()
    try:
        q = db.query(Candidate.id)
        if args.job_id:
            q = q.filter(Candidate.job_id == args.job_id)
        else:
            q = q.filter(Candidate.job_id.isnot(None))
        if args.only_scored:
            q = q.filter(Candidate.fit_score_reasoning.isnot(None))
        q = q.order_by(Candidate.id)
        if args.limit:
            q = q.limit(args.limit)
        ids = [row[0] for row in q.all()]
    finally:
        db.close()

    if not ids:
        print("No candidates matched.")
        return 0

    mode = "APPLY (writes)" if args.apply else "DRY RUN (no writes)"
    print(f"{len(ids)} candidate(s) — {mode}, {CONCURRENCY} at a time\n")

    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def run(cid: int):
        nonlocal done
        async with sem:
            row = await _rescore_one(cid, args.apply)
            done += 1
            print(f"  [{done}/{len(ids)}] id={row[0]} {row[1]}: {row[2]} -> {row[3]}", flush=True)
            return row

    rows = await asyncio.gather(*(run(c) for c in ids))

    print("\n" + "=" * 78)
    print(f"{'id':>6} {'candidate':30s} {'before':>7} {'after':>6}  status")
    print("=" * 78)
    for cid, name, before, after, status in rows:
        print(f"{cid:>6} {name:30s} {before:>7} {after:>6}  {status}")
    print("=" * 78)

    changed = [r for r in rows if r[3] not in ("-", r[2])]
    afters = sorted({r[3] for r in rows if r[3] != "-"})
    print(f"{len(rows)} processed, {len(changed)} would change" if not args.apply
          else f"{len(rows)} processed, {len(changed)} changed")
    print(f"distinct new scores: {len(afters)} -> {', '.join(afters)}")
    if not args.apply:
        print("\nDry run — nothing was written. Re-run with --apply to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
