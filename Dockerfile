FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create upload directories
RUN mkdir -p uploads/jd uploads/resumes uploads/recordings models

# Run migrations (stamp first if DB was created outside alembic) and start server
CMD alembic upgrade head 2>/dev/null || (alembic stamp head && echo "Stamped DB to head") ; uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
