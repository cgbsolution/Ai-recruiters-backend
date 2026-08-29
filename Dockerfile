FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create upload directories (also created at runtime from the mounted volume)
RUN mkdir -p uploads/jd uploads/resumes uploads/recordings uploads/photos uploads/screenshots models

# Migrate first (Alembic owns the schema), then run startup.py as a backstop
# for columns older deployments created outside Alembic, then start the server.
# Running startup.py FIRST is what used to break the deploy: it created columns
# that the pending migrations then tried to add again, and `alembic upgrade
# head` died on DuplicateColumn before uvicorn ever started.
CMD alembic upgrade head && python startup.py && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level warning --no-access-log --proxy-headers --forwarded-allow-ips="*"
