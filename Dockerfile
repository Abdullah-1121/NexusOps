# Stage 1 — build the website. Needs the Node toolchain, which we throw away
# afterwards: the runtime image ships only the finished build (smaller box,
# less attack surface — no Node at runtime).
FROM node:22-alpine AS frontend
WORKDIR /build
# package*.json first: npm ci is deterministic (lockfile present) and its layer
# is cached unless the manifest changes.
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# `tsc -b && vite build` (frontend/package.json) -> dist/
RUN npm run build

# Stage 2 — the runtime image. Python 3.12 matches the repo's dev interpreter.
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Least privilege: the console binds a high port and talks to Redis — root is
# never needed. Containers must OPT INTO non-root, not inherit it by luck.
RUN useradd --create-home --uid 10001 appuser

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY --from=frontend /build/dist ./frontend/dist/

USER appuser
EXPOSE 8137

# The lifeguard IS the entrypoint: it runs the console and supervises it.
# Everything after `scripts.run_guard` is the console's own flags.
ENTRYPOINT ["python", "-m", "scripts.run_guard"]