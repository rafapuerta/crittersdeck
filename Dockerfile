# Base image and dependencies are pinned (digest + hashed lock) so every build is the same.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY crittersdeck ./crittersdeck
# Runs from /app: no build step, so no unpinned build tools in the image.
RUN useradd --system --home /data critters && mkdir -p /data && chown critters /data
USER critters
ENV CRITTERSDECK_DB=/data/crittersdeck.db
# 7420: the board (keep it private). 7421: only POST /ingest (the one to publish).
EXPOSE 7420 7421
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7420/healthz')"
CMD ["python", "-m", "crittersdeck", "serve", "--host", "0.0.0.0", "--port", "7420", "--ingest-port", "7421"]
