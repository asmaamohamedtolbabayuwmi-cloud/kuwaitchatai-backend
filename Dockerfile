FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Ensure Python/OpenSSL has the normal public root store. The KU-specific
# intermediate augments, but never replaces, these trust anchors.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .
RUN test -r /app/app/certs/sectigo-public-server-authentication-ca-dv-r36.pem

# Create non-root user for security
RUN useradd -m appuser && chown -R appuser /app
USER appuser

# Railway injects PORT automatically; default to 8080 for local testing
EXPOSE 8080

# Use exec form so uvicorn receives SIGTERM for graceful shutdown
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
