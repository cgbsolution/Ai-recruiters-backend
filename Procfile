web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --ws-max-size 4194304 --proxy-headers --forwarded-allow-ips="*"
# Talent Agent: serves the Inngest functions in their own process (see app/agent/worker.py).
worker: uvicorn app.agent.worker:app --host 0.0.0.0 --port ${TALENT_WORKER_PORT:-8001} --no-access-log --proxy-headers --forwarded-allow-ips="*"
