# VBMS Manager as a container, for an always-on Linux machine (Raspberry Pi, NAS, home server).
#
#     docker build -t vbms-manager .
#     docker run -d --name vbms-manager --network host -v vbms-data:/data --restart unless-stopped vbms-manager
#     docker logs vbms-manager          # the address to open and the pairing code
#
# or `docker compose up -d` with compose.yaml. Data (boards, history, backups, logs) is in /data.
# Host networking lets it find boards on your network by itself; with port mapping instead
# (-p 8642:8642) add boards by IP. See BUILDING.md for multi-arch builds.

# Pinned by digest; BUILDING.md says how to bump it.
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ARG VERSION=dev
LABEL org.opencontainers.image.title="VBMS Manager" \
      org.opencontainers.image.description="Monitor VESC VBMS32 boards over WiFi and set charge limits" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="$VERSION"

# iproute2: `ip addr` tells the network search which networks to scan.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --user-group --home-dir /data --no-create-home --shell /usr/sbin/nologin vbms \
 && mkdir /data && chown vbms:vbms /data && chmod 700 /data

# App code only (.dockerignore keeps tests, tools and packaging out).
COPY *.py /app/
COPY vesc /app/vesc
COPY cells /app/cells
COPY web /app/web
COPY lisp /app/lisp

ENV VBMS_MANAGER_HOME=/data
VOLUME /data
EXPOSE 8642
USER vbms
# Like Ctrl+C: boards are disconnected cleanly on `docker stop`.
STOPSIGNAL SIGINT
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD ["python", "-I", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8642/', timeout=8)"]
ENTRYPOINT ["python", "-I", "-B", "-u", "/app/vbms_manager.py", "--host", "0.0.0.0", "--no-browser"]
