# syntax=docker/dockerfile:1
#
# Multi-stage build (D-50):
#   1. "uv"      -- just the uv binary, copied from the official image.
#   2. "builder" -- python:3.14-slim (ADR-1); resolves and installs
#                   dependencies with uv, then the project itself, into a
#                   self-contained virtualenv (--no-editable, D-50).
#   3. runtime   -- python:3.14-slim (ADR-1); the built venv and (D-67)
#                   the Google Cloud CLI + Terraform are added, so
#                   `docker exec -it <container> /bin/sh` can run
#                   terraform/gmail/ with the exact same commands as a
#                   local setup -- no separate build target and no
#                   special entry point; CMD stays `dt-agent run`. Runs as
#                   root, no hardening (D-56, accepted risk for this
#                   POC). No HEALTHCHECK: a crashed process stops the
#                   container and the compose restart policy handles it
#                   (ADR-3, D-50).
#
# Base images are pinned by digest (D-56) and updated by hand:
#   docker pull <image> && docker inspect --format '{{index .RepoDigests 0}}' <image>

FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies-only layer first (D-50): cached across source-only changes,
# since it doesn't depend on `src/` at all.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now install the project itself, non-editable, into the same venv, so the
# runtime stage below only needs to copy /app/.venv (no `src/`, no `uv`,
# no pyproject.toml/uv.lock).
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

# Google Cloud CLI + Terraform (D-67, task 33): not used by the app
# itself, only so an operator can `docker exec -it <container> /bin/sh`,
# `cd /app/terraform/gmail` (bind-mounted, see docker-compose.yml) and run
# exactly the same `gcloud auth application-default login` /
# `terraform init/plan/apply` commands documented for a local setup in
# terraform/README.md. Installed from each tool's own official apt
# repository (not Debian's), each with its own signed-by keyring rather
# than the deprecated global apt-key.
#
# $VERSION_CODENAME below resolves to this image's own Debian codename
# (currently "trixie" for python:3.14-slim). This was written without
# running `apt-get update` to confirm HashiCorp's apt repo already
# publishes a release for it; if the second `apt-get update` fails with
# "does not have a Release file" for the hashicorp.list source, hardcode
# "bookworm" instead (HashiCorp's own docs say their packages work across
# Debian releases; bookworm's suite reliably exists there), or drop this
# block's Terraform half and install it from a pinned release zip +
# SHA256SUMS from releases.hashicorp.com instead (the "unzip" package
# below is kept installed for exactly that fallback). Similarly, neither
# `google-cloud-cli` nor `terraform` is pinned to an exact version here
# (no apt/network command was run to check current versions) -- pin
# either with e.g. `terraform=<version>-*` once `apt-cache madison
# terraform` has been checked inside a built image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg unzip \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        -o /etc/apt/keyrings/google-cloud.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/google-cloud.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && curl -fsSL https://apt.releases.hashicorp.com/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/hashicorp.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo "$VERSION_CODENAME") main" \
        > /etc/apt/sources.list.d/hashicorp.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        google-cloud-cli \
        terraform \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runs as root: no user hardening for this POC (D-56, accepted risk).
# No HEALTHCHECK: see ADR-3 / D-50 above.
CMD ["dt-agent", "run"]
