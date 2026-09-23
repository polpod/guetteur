# syntax=docker/dockerfile:1

# --- Étape 1 : construction de l'environnement virtuel avec uv ------------------------------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# WITH_WHISPER=1 ajoute faster-whisper (secours de transcription, image plus lourde).
ARG WITH_WHISPER=0
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    EXTRA=$([ "$WITH_WHISPER" = "1" ] && echo "--extra whisper" || true) && \
    uv sync --frozen --no-dev --no-install-project $EXTRA

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    EXTRA=$([ "$WITH_WHISPER" = "1" ] && echo "--extra whisper" || true) && \
    uv sync --frozen --no-dev --no-editable $EXTRA

# --- Étape 2 : image d'exécution ------------------------------------------------------------
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 guetteur

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config.toml /app/config.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    GUETTEUR_CONFIG=/app/config.toml \
    HF_HOME=/app/data/hf-cache

RUN mkdir -p /app/data && chown guetteur:guetteur /app/data
USER guetteur
VOLUME ["/app/data"]

ENTRYPOINT ["tini", "--", "guetteur"]
CMD ["run"]
