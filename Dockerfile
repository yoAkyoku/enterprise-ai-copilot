FROM python:3.12-slim-bookworm

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

RUN python -m pip install --no-cache-dir ".[production]"

RUN mkdir -p /app/.data && addgroup --system app && adduser --system --ingroup app app && chown -R app:app /app
VOLUME ["/app/.data"]
USER app

EXPOSE 8000
CMD ["enterprise-agent", "--serve", "--host", "0.0.0.0", "--port", "8000"]
