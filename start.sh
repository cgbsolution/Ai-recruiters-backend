#!/bin/sh
# Container start (the Dockerfile's CMD).
#
# 1. Migrate first (Alembic owns the schema), then startup.py as a backstop for
#    columns older deployments created outside Alembic. Running startup.py
#    FIRST is what used to break the deploy: it created columns that the
#    pending migrations then tried to add again, and `alembic upgrade head`
#    died on DuplicateColumn before uvicorn ever started.
# 2. The Talent Agent worker (app/agent/worker.py) as a second process on
#    127.0.0.1:8001, restarted if it ever exits. The API forwards /api/inngest
#    to it (app/agent/proxy.py), so Inngest syncs https://<API>/api/inngest and
#    agent work never runs on the API's event loop. Only when the agent is on
#    and TALENT_EMBEDDED_WORKER isn't false (false = worker deployed as its own
#    service). The decision uses the app's own settings, so values parse the
#    same way as everywhere else.
# 3. The API in the foreground (exec: it receives the container's stop signal).
#    --ws-max-size: the largest WebSocket message is a 1 MB observer JPEG frame
#    (capped in app/routers/observer.py); uvicorn's 16 MB default let one client
#    pin ~16 MB per queued message.
set -e

alembic upgrade head
python startup.py

if python -c "
import sys
from app.talent.config import talent_config as c
sys.exit(0 if c.TALENT_AGENT_ENABLED and c.TALENT_EMBEDDED_WORKER and not c.TALENT_SERVE_IN_API else 1)
"; then
  (
    set +e
    while true; do
      uvicorn app.agent.worker:app --host 127.0.0.1 --port 8001 --log-level warning --no-access-log \
        --proxy-headers --forwarded-allow-ips=127.0.0.1
      echo "Talent Agent worker stopped (exit $?); restarting in 5 s" >&2
      sleep 5
    done
  ) &
  echo "Talent Agent worker starting on 127.0.0.1:8001 (Inngest URL: <API>/api/inngest)"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --log-level warning --no-access-log \
  --ws-max-size 4194304 --proxy-headers --forwarded-allow-ips="*"
