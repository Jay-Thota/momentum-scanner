FROM python:3.11-slim

# Install system libraries including libstdc++6
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libstdc++6 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Shell form so $PORT expands; falls back to 8000 locally
CMD python -m uvicorn backend.api.app:app --host 0.0.0.0 --port ${PORT:-8000}
