FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# TLS CA certs for HTTPS to Discord
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
ENV TZ=America/New_York

COPY celebrate_images ./celebrate_images
CMD ["python", "bot.py"]

