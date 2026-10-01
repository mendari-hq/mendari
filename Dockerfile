# Mendari engine: REST + FHIR + CDS Hooks + review queue + audit + dashboard.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY packs ./packs
COPY datasets ./datasets
RUN pip install ".[server]" && rm -rf /src

RUN useradd --create-home --uid 10001 mendari && mkdir /data && chown mendari /data
USER mendari
WORKDIR /home/mendari

# A per-pack image serves only its pack: docker build --build-arg PACK=mendari/referral-router .
ARG PACK=""
ENV MENDARI_PACKS=${PACK}
LABEL org.opencontainers.image.title="Mendari engine" \
      org.opencontainers.image.description="Decision Packs: REST, FHIR, CDS Hooks, review queue, audit, dashboard" \
      org.opencontainers.image.licenses="Apache-2.0"

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["mendari", "serve", "--host", "0.0.0.0", "--port", "8000", "--db", "/data/mendari.db"]
