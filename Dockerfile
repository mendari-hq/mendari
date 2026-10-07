# Mendari engine: REST + FHIR + CDS Hooks + review queue + audit + dashboard.
#
# Base images are pinned by digest and the Python packages come from uv.lock (every artifact
# hash-checked), so the image holds what CI tested. To bump a pin, see CONTRIBUTING.md
# ("Pinned actions and images").
FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS build
COPY --from=uv /uv /usr/local/bin/uv
# The venv uses the base image's Python (never a downloaded one), so it runs unchanged in the
# runtime stage, which has the same base.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /src
# Dependencies first, in their own layer: it is rebuilt only when the lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra server --no-editable --no-install-project
COPY README.md LICENSE NOTICE ./
COPY src ./src
COPY packs ./packs
COPY datasets ./datasets
RUN uv sync --frozen --no-dev --extra server --no-editable

FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

COPY --from=build /opt/venv /opt/venv

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
