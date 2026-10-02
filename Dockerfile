FROM python:3.11-slim

ARG BUILD_VERSION=0.2.5
ARG BUILD_ARCH=amd64

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN useradd -r -u 1000 -m bridge

WORKDIR /app

COPY requirements-ha.txt .
RUN pip install --no-cache-dir -r requirements-ha.txt

COPY bridge/ bridge/
COPY main.py .
COPY ha_options.py .

# The launcher reads Supervisor's root-readable options file, then drops to
# the unprivileged bridge account before starting the long-lived service.
USER root

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os, sys; sys.exit(0 if os.path.exists('/tmp/bridge.healthy') else 1)"

CMD ["python3", "-u", "ha_options.py"]
