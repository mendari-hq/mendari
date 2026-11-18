# Mendari engine: REST + FHIR + CDS Hooks + review queue + audit + dashboard.
#
# Base images are pinned by digest and the Python packages come from uv.lock (every artifact
# hash-checked), so the image holds what CI tested. To bump a pin, see CONTRIBUTING.md
# ("Pinned actions and images").
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
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

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime

# DSA-6613-1 landed after this base image. Use the signed, dated security archive so
# future rebuilds get the reviewed package. Drop this layer when the next base includes it.
RUN printf '%s\n' 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261118T000000Z/ trixie-security main' > /tmp/mendari-security.list \
    && apt-get -o Dir::Etc::sourcelist=/tmp/mendari-security.list -o Dir::Etc::sourceparts=- -o Acquire::Languages=none update \
    && apt-get -o Dir::Etc::sourcelist=/tmp/mendari-security.list -o Dir::Etc::sourceparts=- install -y --no-install-recommends --only-upgrade 'zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/* /tmp/mendari-security.list

# Ship the patched pip in both the base interpreter and the offline bootstrap used by venv.
ARG PIP_VERSION=26.3
ARG PIP_WHEEL_SHA256=f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13
RUN set -eu; \
    ensure_dir="$(python -c 'from pathlib import Path; import ensurepip; print(Path(ensurepip.__file__).parent)')"; \
    old_version="$(python -c 'import ensurepip; print(ensurepip.version())')"; \
    wheel="$ensure_dir/_bundled/pip-${PIP_VERSION}-py3-none-any.whl"; \
    python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
        "https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/pip-${PIP_VERSION}-py3-none-any.whl" "$wheel"; \
    echo "${PIP_WHEEL_SHA256}  $wheel" | sha256sum -c -; \
    sed -i "s/^_PIP_VERSION = .*/_PIP_VERSION = \"$PIP_VERSION\"/" "$ensure_dir/__init__.py"; \
    if [ "$old_version" != "$PIP_VERSION" ]; then rm "$ensure_dir/_bundled/pip-${old_version}-py3-none-any.whl"; fi; \
    python -m ensurepip --upgrade --default-pip; \
    python -c 'import ensurepip, pip, sys; assert ensurepip.version() == pip.__version__ == sys.argv[1]' "$PIP_VERSION"

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

# A per-pack image serves only its pack: docker build --build-arg PACK=mendari/referral-router .
ARG PACK=""
ENV MENDARI_PACKS=${PACK}
# `licenses` names Mendari's own licence only; the description says the rest is under others.
LABEL org.opencontainers.image.title="Mendari engine" \
      org.opencontainers.image.description="Decision Packs: REST, FHIR, CDS Hooks, review queue, audit, dashboard. Mendari application code: Apache-2.0. Container includes third-party software under additional licenses. See /licenses and /licenses/THIRD_PARTY_NOTICES.md." \
      org.opencontainers.image.licenses="Apache-2.0"

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["mendari", "serve", "--host", "0.0.0.0", "--port", "8000", "--db", "/data/mendari.db"]
