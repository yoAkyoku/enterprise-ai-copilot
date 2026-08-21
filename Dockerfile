# Pin the release base image; refresh this digest deliberately during release
# maintenance and rerun the container vulnerability scan.
FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_PROVIDER_MODE=synthetic \
    AGENT_STORAGE_MODE=memory

WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY packages /app/packages
COPY services /app/services
COPY apps /app/apps
COPY agents /app/agents
COPY .agents /app/.agents
COPY mcp /app/mcp
COPY schedules /app/schedules
COPY plugins /app/plugins
COPY data /app/data
COPY infra /app/infra
COPY scripts /app/scripts

RUN python -m pip install --no-cache-dir ".[production]"

RUN mkdir -p /app/.data && addgroup --system app && adduser --system --ingroup app app && chown -R app:app /app
VOLUME ["/app/.data"]
USER app

EXPOSE 8000
CMD ["enterprise-agent", "--serve", "--host", "0.0.0.0", "--port", "8000"]
