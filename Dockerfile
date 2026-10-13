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

# Migrate, start the Talent Agent worker next to the API, then the API itself —
# see start.sh for the order and why.
CMD ["sh", "./start.sh"]
