FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY README.md /app/README.md
COPY docs /app/docs
COPY outputs /app/outputs
COPY sandbox_tool /app/sandbox_tool

RUN useradd --create-home --uid 1000 agent \
    && mkdir -p /srv/sandbox-tool/runs \
    && chown -R agent:agent /srv/sandbox-tool /app

USER agent
WORKDIR /app

CMD ["python", "outputs/generic_parent_runner.py", "--help"]
