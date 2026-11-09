# Runs as a non-root user; code is root-owned and read-only to it. Run with a read-only rootfs:
#   docker run --read-only --cap-drop ALL --security-opt no-new-privileges \
#     --tmpfs /tmp --tmpfs /app/.duel:uid=10001,gid=10001,mode=0700 claude-duel ...
FROM python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e
# zlib1g CVE-2026-76844 / CVE-2026-85091 (fixable); base pin not yet rebuilt.
RUN apt-get update     && apt-get install -y --no-install-recommends --only-upgrade zlib1g     && rm -rf /var/lib/apt/lists/*
RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 duel
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY configs ./configs
# pip/setuptools are build-time only; removing them drops their vendored (often vulnerable) deps.
RUN pip install --no-cache-dir . \
    && python -m pip uninstall -y -q setuptools wheel pip \
    && install -d -o 10001 -g 10001 -m 0700 /app/.duel
USER 10001:10001
ENV DUEL_STORE_DIR=/app/.duel DUEL_SANDBOX=subprocess PYTHONDONTWRITEBYTECODE=1
HEALTHCHECK NONE
ENTRYPOINT ["duel"]
CMD ["--help"]
