# Correctarr — finds and fixes what Radarr and Sonarr leave lying around.
#
# Two stages: the first builds the dependencies into their own venv, the second
# takes only the finished venv along. That keeps the dependency layer cached, so
# a rebuild after a code change takes seconds.

FROM python:3.13-slim-trixie AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# build-essential is only needed here: a few packages ship no prebuilt wheel for
# arm64 and have to be compiled. None of it ends up in the final image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install --upgrade pip setuptools wheel \
 && /venv/bin/pip install -r /tmp/requirements.txt


FROM python:3.13-slim-trixie

ARG VERSION=dev
ARG BUILT_AT=unknown
ARG COMMIT=unknown

LABEL org.opencontainers.image.title="Correctarr" \
      org.opencontainers.image.description="Finds and fixes what Radarr, Sonarr, SABnzbd and Prowlarr leave lying around" \
      org.opencontainers.image.source="https://github.com/flizzy27/Correctarr" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}"

ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    CONFIG_DIR=/config \
    VERSION=${VERSION} \
    BUILT_AT=${BUILT_AT} \
    COMMIT=${COMMIT} \
    HOST=0.0.0.0 \
    PORT=8099 \
    AUTH=on \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    TZ=Etc/UTC

# curl for the health check, tini as a proper init, gosu so the entrypoint can
# drop privileges.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tini gosu tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 100 -o correctarr 2>/dev/null || true \
 && useradd -u 99 -g 100 -M -d /config -s /usr/sbin/nologin correctarr 2>/dev/null || true

COPY --from=builder /venv /venv

WORKDIR /app
COPY app/ /app/app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config && chown -R 99:100 /config /app

VOLUME ["/config"]
EXPOSE 8099

# /api/alive answers without a single outbound call. The previous check hung off
# the status endpoint, and that queries every configured service — one that
# swallows packets instead of refusing them would have had the container
# reported as unhealthy while it was working perfectly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/alive" >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
