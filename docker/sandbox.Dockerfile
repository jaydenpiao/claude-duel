# Sandbox image (S17-4): the pinned slim base with the same fixable zlib1g/libzstd1 patches as the app image.
# CI builds and Trivy-scans it with the app image. Running it needs a published digest (DockerSandbox refuses an
# unpinned local tag), so DEFAULT_IMAGE stays the raw base until that is published.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e
FROM ${PYTHON_IMAGE}
# https + no SRV: the runner egress proxy returns 500 for apt SRV targets (198.18.0.1).
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::EnableSrvRecords=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::EnableSrvRecords=false install -y --no-install-recommends --only-upgrade zlib1g libzstd1 \
    && dpkg-query -W zlib1g | grep -F '1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && dpkg-query -W libzstd1 | grep -F '1.5.7+dfsg-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 duel
USER duel
