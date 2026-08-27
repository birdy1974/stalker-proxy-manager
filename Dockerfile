# ============================================================================
# Stalker Proxy Manager - all-in-one image
# ----------------------------------------------------------------------------
# * Debian trixie ships ffmpeg 7.x (latest stable series)
# * VAAPI / QSV enabled out of the box; the Synology DS918+ (Apollo Lake,
#   Gen9.5 graphics) is accelerated through the iHD driver package + /dev/dri
# * Python 3.12 runtime, single-process uvicorn (in-process MAC occupancy
#   and stream registry make multi-worker setups meaningless)
# ============================================================================
FROM python:3.12-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg + Intel media drivers (i915 Gen9/iHD) + Intel VA-API runtime.
# intel-media-va-driver-non-free is required for full H.264/HEVC encode on
# Apollo Lake (the free driver only decodes). It lives in the `non-free` apt
# component, which the base slim image does not enable — so we add it first.
RUN set -eux; \
    [ -f /etc/apt/sources.list.d/debian.sources ] || exit 1; \
    sed -i -E 's/^Components: main( non-free-firmware)?$/Components: main non-free\1/' \
        /etc/apt/sources.list.d/debian.sources; \
    echo 'APT::Get::Update::SourceListWarnings::NonFreeFirmware "false";' \
        > /etc/apt/apt.conf.d/no-bookworm-firmware.conf; \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        vainfo \
        intel-media-va-driver-non-free \
        libva2 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# application code (templates + static assets included)
COPY app ./app

# runtime layout: /config = sqlite DB + settings, /media = local video files
RUN useradd -r -u 2000 -m spm \
    && mkdir -p /config /media \
    && chown -R spm:spm /config /media /app

USER spm

ENV SPM_DATA_DIR=/config \
    SPM_MEDIA_ROOT=/media \
    SPM_VAAPI_DEVICE=/dev/dri/renderD128 \
    SPM_PORT=8880 \
    SPM_MOCK_PORTAL=0

EXPOSE 8880

# container marks healthy once the login page answers (app fully booted)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8880/login >/dev/null || exit 1

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8880"]
