FROM ghcr.io/astral-sh/uv:0.13.10 AS uv

FROM node:22-bookworm-slim AS frontend
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/index.html web/tsconfig.json web/vite.config.ts ./
COPY web/src ./src
RUN npm run build

FROM python:3.11-slim

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    RADAR_DATA_DIR=/data PATH="/app/.venv/bin:${PATH}"

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --extra embedding
COPY research_radar ./research_radar
COPY --from=frontend /web/dist ./web/dist

RUN groupadd --gid 10001 radar && useradd --uid 10001 --gid 10001 --create-home radar \
    && install -d -o radar -g radar /data
USER radar
CMD ["python", "-m", "research_radar.cli", "scheduler"]
