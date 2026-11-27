FROM ghcr.io/astral-sh/uv:0.13.10 AS uv
FROM python:3.11-slim

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    RADAR_DATA_DIR=/data PATH="/app/.venv/bin:${PATH}"

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --extra ui --extra embedding
COPY research_radar ./research_radar
COPY streamlit_app.py ./

RUN groupadd --gid 10001 radar && useradd --uid 10001 --gid 10001 --create-home radar \
    && install -d -o radar -g radar /data
USER radar
CMD ["python", "-m", "research_radar.cli", "scheduler"]
