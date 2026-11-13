# Agents dashboard, containerized.
#
#   docker build -t agents .
#   docker run --rm -p 5080:5080 -e AGENTS_HOST=0.0.0.0 -e LUMEN_API_KEY=... agents
#
# Usually started by the `deployments` fleet (../deployments), which forwards the
# keys. Secrets are not baked in (see .dockerignore): keys come from env at
# runtime. agents.yaml and per-agent folders ARE included (they're config/code).
FROM python:3.12-slim

ENV AGENTS_HOST=0.0.0.0 \
    AGENTS_PORT=5080 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 5080

# dashboard.py binds AGENTS_HOST:AGENTS_PORT (defaults above).
CMD ["python", "dashboard.py"]
