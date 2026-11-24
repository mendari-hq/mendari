# Mendari engine: REST + FHIR + CDS Hooks + review queue + audit + dashboard.
#
# Base images are pinned by digest and the Python packages come from uv.lock (every artifact
# hash-checked), so the image holds what CI tested. To bump a pin, see CONTRIBUTING.md
# ("Pinned actions and images").
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

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
COPY agents ./agents
RUN uv sync --frozen --no-dev --extra server --no-editable

FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

COPY --from=build /opt/venv /opt/venv

# The notices of every third-party component, written from what the runtime stage holds
# (scripts/image_notices.py: THIRD_PARTY_NOTICES.md, inventory.json, a CycloneDX list of what an
# SBOM scanner cannot see, and the texts licenses/image keeps for bundled libraries and crates),
# in a stage of their own: the image takes only /licenses from it, never the script or the
# manifest, and builds with Docker's classic builder as with BuildKit.
FROM runtime AS notices
COPY scripts/image_notices.py /tmp/image_notices.py
COPY licenses/image /tmp/licences
RUN python3 /tmp/image_notices.py --manifest /tmp/licences --write /licenses

FROM runtime

# Licences: Mendari's LICENSE and NOTICE, where the corresponding source is (SOURCE.md), and the
# notices written above. Debian's own notices stay where the base image keeps them,
# /usr/share/doc/*/copyright and /usr/share/common-licenses: nothing in this file removes files.
# scripts/check_image_licences.py checks all of it against the finished image (CI's docker job,
# and every image a release builds).
COPY LICENSE NOTICE licenses/image/SOURCE.md /licenses/
COPY --from=notices /licenses /licenses

RUN useradd --create-home --uid 10001 mendari && mkdir /data && chown mendari /data
USER mendari
WORKDIR /home/mendari

# An image built for one agent serves only that agent:
# docker build --build-arg AGENT=mendari/portal-message-agent .
ARG AGENT=""
ENV MENDARI_AGENTS=${AGENT}
# `licenses` names Mendari's own licence only; the description says the rest is under others.
LABEL org.opencontainers.image.title="Mendari engine" \
      org.opencontainers.image.description="Governance and evaluation engine for healthcare AI agents: REST, FHIR, CDS Hooks, review queue, audit, dashboard. Mendari application code: Apache-2.0. Container includes third-party software under additional licenses. See /licenses and /licenses/THIRD_PARTY_NOTICES.md." \
      org.opencontainers.image.licenses="Apache-2.0"

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["mendari", "serve", "--host", "0.0.0.0", "--port", "8000", "--db", "/data/mendari.db"]
