# Talent Agent

An always-on agent over the Resume Database. It runs four loops:

| Loop | What it does | Switch |
|---|---|---|
| **A · Refresh** | Asks candidates to update their profile every 3 months by default (Settings: 30–730 days; 3 messages at most, stops on any answer, opt-out or bounce; someone who doesn't answer is not asked again until the next window) | `refresh` (+ `refresh_rolling`) |
| **B · Match** | When a job is created or changed: SQL filter → vector similarity → Claude rerank of redacted profiles | `match` |
| **C · Invite** | Invites the top matches. **Copilot** jobs wait for a recruiter's approval; **autopilot** jobs invite automatically | `invite` |
| **D · Interview** | Offers times, sends reminders, runs an async (text or voice) AI interview, scores it against a rubric with quotes, builds a shortlist order | `interview` (+ `live_voice`) |

The AI orders the queue; people decide outcomes. Submitting a candidate to the client and sending a "not moving forward" message always need a recruiter's recorded decision, in every autonomy mode. Offers and salary discussion are not part of the agent.

Built inside the existing FastAPI backend (Python, SQLAlchemy, Alembic) with the **Inngest Python SDK** for durable workflows. The UI lives in the Next.js frontend under `/talent` (admin) and `/t/...` (candidate pages).

---

## Architecture

```
app/talent/      services (plain Python, testable without Inngest)
app/agent/       Inngest functions, one per file + worker.py (ASGI app serving them)
app/routers/talent_admin.py     settings, consent, campaigns, deliverability, audit, erasure
app/routers/talent_jobs.py      job settings, matching, matches, ratings, precision@10, backfill
app/routers/talent_pipeline.py  approvals, invites, interviews, review, overrides, submit/reject
app/routers/talent_public.py    candidate pages (magic links): refresh, invite, interview, unsubscribe, my data, deliverability webhook
```

### Inngest functions

| Function | Trigger | Does |
|---|---|---|
| `refresh-sweep` | cron 10:00 IST daily, `talent/refresh.sweep.requested` | enrol due profiles (rolling), dispatch within today's budget, re-dispatch lost starts, re-queue lost CV versions |
| `refresh-touch` | `talent/refresh.member.dispatched` | touch 1 → 3 days → touch 2 → 4 days → touch 3 → 4 days → no response; stops on `talent/member.stopped` |
| `refresh-respond` | `talent/refresh.responded`, `talent/cv.version.pending` | parse the new CV version (Claude, with diff), embed it |
| `refresh-reengage` | cron daily | fresh profiles past the refresh window become stale again |
| `refresh-dead-letter` | cron daily | catch-up: repeatedly bounced profiles → unreachable |
| `match-job` | `talent/job.updated` (debounced 2 min/job), `talent/match.requested` | Stage 1-2, rerank in chunks of 10, top K, stale strong matches → refresh first |
| `cv-backfill` | `talent/backfill.requested`, every 15 min | v1 baselines and embeddings (scheduled: every organization with Match on, so new uploads become matchable). No bulk Claude re-parse: profiles keep their fast-parse metadata |
| `invite-plan` | `talent/match.completed`, `talent/invite.plan.requested` | autopilot: invite top K; copilot: approval requests |
| `invite-send` | `talent/invite.send` | invite → 3 days → reminder → 3 days → close |
| `interview-schedule` | `talent/interview.offered` | email the 3 slots, wait 4 days for a booking, then expire + flag |
| `interview-reminders` | `talent/interview.booked` (cancelled on reschedule) | confirmation, 24 h and 1 h reminders, no-show check (1 automatic re-offer, then flag) |
| `interview-prepare` | `talent/interview.booked` | generate the 8-12 questions (Question Bank + Claude) |
| `interview-conduct` | `talent/interview.started` | finish an interview still open at its time limit |
| `interview-score` | `talent/interview.completed` | retry failed transcriptions, score against the rubric |
| `shortlist-build` | `talent/shortlist.requested` | recompute the shortlist sort order |
| `live-voice-sync` | cron every 10 min | import finished live voice sessions for scoring |

Event payloads carry **ids only** (`app.agent.client.emit` refuses anything else). No function's correctness depends on an event arriving: every send re-checks the database, and the daily sweeps catch up on anything an outage left behind.

### Invariants worth knowing before you change anything

- **One send path.** Every message goes through `app.talent.outbox.send_touch`. Transports demand a `ContactPermit`, which only `consent.can_contact()` issues (consent for that channel and purpose, global suppression list, reachability), checked at the moment of sending. `tests_talent/test_phase1_refresh.py::test_only_the_outbox_talks_to_transports` fails if any other module reaches a transport.
- **No duplicate sends.** The touch row (unique per profile/campaign/touch number) is written first, and only the caller that moves it `queued → sending` sends. A send that may have been delivered (the connection dropped during SMTP DATA) is recorded as `unknown` and never retried.
- **Humans decide outcomes.** `approvals.require()` gates copilot invites, submissions and rejections. `decisions.send_rejection` is the only code that can send a rejection, and it refuses without an approval decided by a user.
- **Fair matching.** Ranking prompts and embeddings see only allowlisted, job-relevant fields (`app.talent.redaction`), with free text scrubbed of names, contact details, labelled personal data, addresses, ages, gender, marital status, religion, caste and nationality terms, and gendered pronouns. Every score records `fields_sent`.
- **Audit everything.** Every message, AI score, human decision, consent change and deletion writes `audit_log` (ids, counts, field names; no contact details). An `AFTER DELETE` trigger logs every deleted profile, whatever deleted it.

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `TALENT_AGENT_ENABLED` | `false` | **Master switch.** Off: nothing is sent, matched or scored, whatever the per-organization switches say. Candidate pages (unsubscribe, my data) keep working. |
| `INNGEST_EVENT_KEY` | — | Sending events to Inngest (production) |
| `INNGEST_SIGNING_KEY` | — | Required to serve functions in production |
| `INNGEST_DEV` | — | `1` for the local dev server |
| `INNGEST_APP_ID` | `anytimehire-talent-agent` | |
| `TALENT_SERVE_IN_API` | `false` | Serve the functions inside the API process itself (not recommended: agent work then shares the API's event loop with live interviews) |
| `TALENT_EMBEDDED_WORKER` | `true` | `start.sh` runs the worker as a second process in the API container and the API forwards `/api/inngest` to it. `false` when the worker is deployed as its own service |
| `TALENT_WORKER_URL` | `http://127.0.0.1:8001` | Where the API forwards `/api/inngest` |
| `INNGEST_MAX_CONCURRENCY` | `5` | Your Inngest plan's per-function concurrency limit (Hobby: 5). Cloud refuses to sync functions that ask for more; raise it on a paid plan (refresh sends want 25, invite sends 20) |
| `TALENT_LLM_PROVIDER` | `ollama` | Who serves the agent's AI (match scoring, CV parsing, agent interviews): `ollama` (self-hosted, no Anthropic credits) or `anthropic` (Claude). The main product's live interviews don't use this |
| `TALENT_OLLAMA_MODEL` | `qwen2.5:7b-instruct` | The one Ollama chat model for every agent task (~5.5 GB RAM). `qwen2.5:3b-instruct` is ~2x faster with flatter scores |
| `TALENT_OLLAMA_RERANK_POOL` / `TALENT_OLLAMA_RERANK_CHUNK` | `20` / `2` | Candidates AI-scored per job, and per durable step (a CPU 7B model takes ~1-2 min per candidate) |
| `TALENT_OLLAMA_NUM_CTX` / `TALENT_OLLAMA_TIMEOUT_SECONDS` / `TALENT_OLLAMA_PARALLEL` | `8192` / `900` / `1` | Context, per-call timeout, concurrent requests |
| `TALENT_OLLAMA_FOLLOWUPS` | `false` | Interview follow-up questions are generated while the candidate waits — leave off on a CPU-only Ollama |
| `TALENT_PARSE_MODEL` / `TALENT_RERANK_MODEL` / `TALENT_SCORER_MODEL` / `TALENT_INTERVIEW_MODEL` | `claude-opus-5-5` | Model per task with `TALENT_LLM_PROVIDER=anthropic` |
| `TALENT_PARSE_EFFORT` / `_RERANK_` / `_SCORER_` / `_INTERVIEW_EFFORT` | `low` / `medium` / `high` / `low` | `output_config.effort` (sent only to models that accept it) |
| `TALENT_LLM_TIMEOUT_SECONDS` | `180` | |
| `TALENT_LLM_REFUSAL_FALLBACK` | `true` | Server-side refusal fallback on realtime calls |
| `TALENT_RERANK_CONCURRENCY` | `4` | Parallel rerank calls per chunk |
| `TALENT_RERANK_POOL` | `200` | Profiles kept by Stage 2 |
| `EMBEDDING_PROVIDER` | — | `ollama` (self-hosted, no API key) \| `voyage` \| `gemini` \| `openai` \| `hash` (tests only). Unset: keyword similarity |
| `EMBEDDING_MODEL` | per provider | `bge-m3`, `voyage-3.5`, `gemini-embedding-001`, `text-embedding-3-small` — must make 1024-number vectors |
| `EMBEDDING_DIM` | `1024` | Must match the column (`vector(1024)`) |
| `VOYAGE_API_KEY` | — | |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server for `EMBEDDING_PROVIDER=ollama`; in Dokploy `http://ollama:11434` |
| `OLLAMA_NUM_CTX` / `OLLAMA_TIMEOUT_SECONDS` | `4096` / `600` | Context per text; timeout per batch of 64 |
| `TALENT_EMAIL_TRANSPORT` | `smtp` | `smtp` (existing `SMTP_*`), `memory` (tests), `disabled` |
| `TALENT_SEND_RAMP` | `500,2000,5000` | Daily caps per sender domain, one step every `TALENT_SEND_RAMP_STEP_DAYS` (7) |
| `TALENT_BULK_SHARE` | `0.7` | Share of a day's cap refresh mail may use (the rest: invites, reminders) |
| `TALENT_ENROLL_FRACTION` | `0.4` | Share of the bulk budget new refresh sequences may take per day |
| `TALENT_BOUNCE_PAUSE_RATE` / `TALENT_COMPLAINT_PAUSE_RATE` | `0.03` / `0.001` | Auto-pause thresholds |
| `TALENT_HEALTH_MIN_SAMPLE` | `100` | Denominator floor for the rates |
| `TALENT_SOFT_BOUNCES_UNREACHABLE` | `3` | |
| `TALENT_DELIVERABILITY_WEBHOOK_SECRET` | — | HMAC secret for `POST /talent/public/webhooks/deliverability` |
| `TALENT_SUPPRESSION_KEY` | `SECRET_KEY` | Key for suppression hashes. **Never rotate** — it orphans the list |
| `TALENT_PUBLIC_BASE_URL` | `FRONTEND_BASE_URL` | Candidate pages |
| `TALENT_PUBLIC_API_URL` | — | This API's public URL — the `List-Unsubscribe` one-click target (Gmail/Yahoo bulk-sender rule) |
| `TALENT_SENDER_POSTAL_ADDRESS` | — | Footer of outreach email |
| `TALENT_WHATSAPP_TEMPLATES` | — | JSON `{"refresh_touch_3": "<approved template>"}`; kinds without an approved template are never sent on WhatsApp |
| `TALENT_REFRESH_TOUCH_GAPS_DAYS` | `3,4` | |
| `TALENT_INVITE_REMINDER_DAYS` / `TALENT_INVITE_CLOSE_DAYS` | `3` / `3` | |
| `TALENT_*_LINK_DAYS` | 14 / 10 / 14 / 30 | Refresh, invite, interview, my-data link lifetimes |
| `TALENT_AUDIO_MAX_MB` | `15` | |
| `CLAMAV_HOST` / `CLAMAV_PORT` / `CLAMAV_TIMEOUT_SECONDS` | — / `3310` / `30` | Virus scanning of every uploaded resume (clamd). Set: uploads are refused while the scanner is unreachable |

Existing settings it uses: `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` (voice answers), `SMTP_*`, `EMAIL_FROM`, `WHATSAPP_*`, `GOOGLE_*` (panel calendars), `SECRET_KEY`.

## Switches, and how to turn each loop off

Per organization, under **Talent Agent → Settings** (admins), stored in `talent_settings.flags`:

| Switch | Off means |
|---|---|
| `refresh` | No refresh messages. Running sequences stop at their next step (the touch is recorded as skipped). |
| `refresh_rolling` | Only pilot campaigns send; the sweep enrols nobody new. |
| `match` | Job changes don't trigger matching; "Run matching" returns without work. |
| `invite` | No invites or reminders; planning makes no approvals. |
| `interview` | No interview offers, confirmations or reminders. |
| `live_voice` | Live voice jobs interview in async voice mode instead. |
| `retention` | Expired profiles are only reported, not deleted. |

Without the UI:

```sql
UPDATE talent_settings SET flags = flags || '{"refresh": false}'::jsonb WHERE organization_id = 42;
```

**Emergency stop for everything:** set `TALENT_AGENT_ENABLED=false` and restart the worker and API. **Stop one campaign:** Campaigns → Pause (sends wait; nothing is lost). **Stop all mail from a domain:** the domain auto-pauses on a bounce/complaint spike. To force it: `UPDATE sender_domains SET paused = true WHERE domain = '...'`.

---

## Deployment

1. **Migrations** — `alembic upgrade head` (`start.sh` already runs it). Revisions `q1r2s3t4u5v6`, `r2s3t4u5v6w7`, `s3t4u5v6w7x8`. They are purely additive.
2. **pgvector (optional, recommended)** — if the database server has the extension available, the migration enables it and embeddings use `vector(1024)`. Without it they are `double precision[]` and Stage 2 runs in numpy over the Stage-1 pool (fine at 40K). After installing pgvector later: `python -m app.talent.vectors upgrade` (then `... status`).
3. **Ollama (the agent's AI, free)** — deploy `docker-compose.ollama.dokploy.yml` as a Dokploy Compose service (it pulls `bge-m3` for embeddings and `qwen2.5:7b-instruct` for match scoring, CV parsing and agent interviews; it needs ~8 GB RAM free), then set `EMBEDDING_PROVIDER=ollama` and `OLLAMA_BASE_URL=http://ollama:11434` on the API (the worker runs in the same container and shares its environment). Profile text never leaves your servers. CPU-only it is slow but one-off: about 2.4 profiles/s on a 4-core laptop CPU (40K in ~5 h, capped at 2 CPUs so live interviews stay responsive); after that only new or changed CVs are embedded. The Settings page warns if Ollama is unreachable or the model is missing.
4. **Antivirus (recommended)** — deploy `docker-compose.clamav.dokploy.yml` as a Dokploy Compose service and set `CLAMAV_HOST=clamav` on the API (the worker shares its environment). Every resume upload is already checked for dangerous content (PDF scripts and embedded files, Word macros, remote templates, zip bombs, encrypted files — `app/services/file_safety.py`); ClamAV adds signature-based virus scanning. The Settings page warns while it isn't configured.
5. **Worker** — nothing to deploy separately. The Docker image's `start.sh` runs the worker (`app.agent.worker`) as a second process in the API container on `127.0.0.1:8001`, restarts it if it exits, and the API forwards `/api/inngest` to it (`app/agent/proxy.py`). So it shares the API's environment variables and its `uploads/` volume (it reads uploaded CVs and writes interview audio), while agent work still runs outside the API's event loop. It does not import `app.main`, so it runs no migrations, sweepers or bulk-upload queue. To run it as its own service instead: `uvicorn app.agent.worker:app --port 8001` (Procfile `worker`) with the same env and the same uploads volume, and `TALENT_EMBEDDED_WORKER=false` on the API.
6. **Inngest** — set `INNGEST_EVENT_KEY` and `INNGEST_SIGNING_KEY` on the API, then in Inngest Cloud → Apps → **Sync new app** enter `https://<API address>/api/inngest` (production: `https://anytimehire-api-prod.epfassist.com/api/inngest`). The worker checks in as soon as the sync succeeds and every 10 minutes after; until then Settings shows "worker has never checked in" with the exact URL to sync. Locally: run the worker on 8001 and `npx inngest-cli@latest dev -u http://localhost:8001/api/inngest` with `INNGEST_DEV=1` (or `-u http://localhost:8000/api/inngest` to go through the API like production).
7. **Email** — Gmail/Workspace SMTP is not suitable for thousands of messages a day. Use a provider with bounce and complaint webhooks (SES, SendGrid, Postmark…). Put a small adapter in front of `POST /talent/public/webhooks/deliverability` that maps its events to `{"events":[{"id","type":"bounce|complaint|delivered|unsubscribe","bounce_type":"hard|soft","message_id"}]}`, signed with `TALENT_DELIVERABILITY_WEBHOOK_SECRET` (`X-Talent-Signature` = hex HMAC-SHA256 of the body). Set up SPF, DKIM and DMARC for the sender domain, and set `TALENT_PUBLIC_API_URL` so one-click unsubscribe works.
8. **Turn it on** — `TALENT_AGENT_ENABLED=true`, then per organization in the UI.

## Runbooks

**First rollout (recommended order).**
1. Phase 0: Talent Agent → Campaigns → *Check all profiles*. Review duplicates and *Review duplicate merge* (every deleted row is in the audit log).
2. Record consent. The existing CVs have **no recorded contact consent**, so nothing will be sent until you decide on the lawful basis and record it: `POST /talent/consent/import` (matched by email, with the evidence `source` and `basis`), or `POST /talent/consent/bulk` for a set of profiles on a stated basis. Bulk recording never overrides a candidate's own withdrawal or a suppression.
3. Switch on `refresh` only, create a 2,000-person pilot (stratified by CV age: under 12, 12-24, over 24 months), and watch the funnel and bounce/complaint health for a week or two of the ramp.
4. Backfill embeddings (*Matching readiness → Embed profiles*; afterwards new uploads are embedded every 15 minutes), then switch on `match` and rate the top 10 for about 10 jobs to get a precision@10 baseline.
5. Then `invite` with jobs on copilot, then `interview`.

**Bounce or complaint spike.** The campaign (and possibly the domain) auto-pauses; the console shows why. Look at the bounced addresses' source (old CVs bounce most), fix the list (Phase 0 email check with MX lookup: `{"check_mx": true}`), then *Resume* the campaign and, if paused, *Resume sending* for the domain.

**A candidate asks to be deleted.** They can do it themselves from the *your data* link in any message. Or: `DELETE /talent/profiles/{id}` (admins). This removes the profile, every CV version and file, Talent Agent interviews with recordings and transcripts, and their applications in that organization with interview sessions, transcripts and recordings. Their email and phone stay on the suppression list as keyed hashes so a re-imported CV is never contacted. `profile.erased` and `profile.deleted` audit rows remain (ids only).

**A candidate says they keep getting messages.** Audit view → filter by their profile id. Every message, skip reason and consent change is there.

**Matching looks wrong for a job.** Job page → Settings: set explicit must-haves, budget, notice and locations; *Run matching*. "Show how this was scored" lists exactly what the model saw. Rate matches; the precision@10 report tracks quality.

**Profile-update emails.** Branded per organization under *Talent Agent → Settings → Email branding*: sender name, logo (an https image link), button colour, sign-off and an opening paragraph in the organization's own words; *Preview* renders all three messages. Each email shows the candidate what is on file (role, top skills, last updated) and links to their personal page, where they confirm or edit their details and can upload a newer CV (PDF or DOCX, checked and virus-scanned before it is stored). Replies by email are not processed: a link is tied to one profile, while a reply's From address can be forged.

**A newer resume for someone already in the database.** Whatever the upload path (Resume Database, upload to a job, the refresh page), the profile keeps its id and every job candidate linked to it moves to the new file in the same transaction; with the agent tracking the profile, the change becomes its new current CV version (the old file stays as history until its retention date). The old file is deleted only when nothing references it, and the File Manager refuses to delete a resume that is still in use. To repair job links broken before this existed: `python -m app.services.resume_links repair` (dry run), then `... repair --apply`.

**Inngest outage.** Nothing is lost: candidate pages, approvals and bookings keep working (they write the database directly). Events not delivered are caught up by the daily sweep (lost refresh starts, pending CV versions). Interview reminders for interviews booked during the outage may be missed — check *Interviews*.

**Re-score after a prompt change.** Bump the `*_VERSION` in `app/talent/prompts.py`. The rerank re-scores matches whose prompt version changed the next time the job is matched; interviews are re-scored by re-emitting `talent/interview.completed`.

## Testing

```
set TALENT_TEST_DATABASE_URL=postgresql+psycopg://postgres:pw@localhost:55440/talent_test
.venv\Scripts\python.exe -m pytest tests_talent -q
```

The database name must contain `test`; the schema is rebuilt from the models. Run it against a server **with** pgvector and one **without** (both storage modes are supported). The 40K load test is opt-in: `TALENT_LOAD_TEST=1`.

## Known limits

- Calendar availability: Google Calendar only (Microsoft Graph is not implemented). Without a panel, interviews use business hours (Mon-Fri 10:00-18:00 in the organization's time zone).
- SMS is not implemented; WhatsApp needs Meta-approved templates per message kind.
- Open tracking is link-click based (no tracking pixel).
- Submission to the client is a recorded decision plus a candidate packet (`GET /talent/matches/{id}/packet`); nothing is emailed to the client automatically.
- Current Claude models take no `temperature`; consistency comes from structured outputs and low effort.
