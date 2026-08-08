# The dashboard is a static bundle served by `api`, so it is built here and copied in. Node is not
# present in the runtime image. See docs/07-observability.md#dashboard.
FROM node:20-slim AS dashboard

WORKDIR /dashboard

# The lock file alone drives the install, so this layer survives edits to the dashboard sources.
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

COPY dashboard/ ./
RUN npm run build

# One image for all three processes. `api`, `worker` and `poller` differ only by command; see the
# topology in docs/09-operations.md.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.2 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /app

# Dependencies resolve from the lock file alone, so this layer survives source edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev

# After `COPY . .`, so the built bundle is not overwritten by the sources it was built from.
COPY --from=dashboard /dashboard/dist ./dashboard/dist

EXPOSE 8000
CMD ["uvicorn", "sentinel.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
