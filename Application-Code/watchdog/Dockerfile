FROM python:3.9-slim

WORKDIR /app

# Install required system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    docker \
    google-generativeai \
    pydantic \
    requests

# Copy the watchdog code
COPY watchdog.py .

# Expose port
EXPOSE 8080

# Execute
CMD ["python", "watchdog.py"]
