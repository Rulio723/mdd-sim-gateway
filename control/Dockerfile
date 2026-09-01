# MDD Sim Gateway control-plane image: FastAPI manager + React WebUI, in one container.
#
# Build context = repo ROOT (build with: docker build -f control/Dockerfile -t mdd-sim-gateway/control .)
# Multi-arch (amd64/arm64) and fully from source: the WebUI is compiled in stage 1, pcsc-lite
# and the Python deps in stage 2 — no Node or Python toolchain needed on the host. Stage 3 is
# the shipped image: it inherits NONE of the build toolchain, only the compiled artifacts
# (pcsc-lite runtime lib + site-packages) are copied in.
#
# Runtime model: runs PRIVILEGED with the host Docker socket (/var/run/docker.sock) and the
# host pcscd socket (/run/pcscd) bind-mounted; it launches/《manages》sibling engine containers
# via the Docker SDK and reads the physical reader via pyscard. The repo layout is mirrored
# under /app so run.py (`from app import ...`) resolves.

# ---------- stage 1: build the WebUI ----------
# The output is architecture-independent static content. Keep this stage on the native build
# platform so an amd64 GitHub runner does not run npm under ARM64 QEMU during release builds.
FROM --platform=$BUILDPLATFORM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS webui
WORKDIR /webui
# Install deps against the lockfile first (better layer caching), then build.
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui/ ./
RUN npm run build

# ---------- stage 2: native build (pcsc-lite + python deps) ----------
# Everything that needs a C toolchain is compiled here and only the *artifacts* are copied into
# the runtime stage, so swig/gcc/meson/ninja/flex/perl/wget never reach the shipped image.
# NOTE: no --platform here on purpose — this stage compiles native binaries (pcsc-lite, pyscard)
# and must run on the TARGET platform. release.yml builds each arch on a native runner.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS deps
# pcsc-lite client library, built from source and pinned to the SAME version as the host pcscd
# and the engine image (PCSC_VERSION). pyscard loads libpcsclite.so.1 at runtime to detect the
# reader via the mounted host /run/pcscd socket — matching versions avoids protocol mismatch.
ARG PCSC_VERSION=2.3.3
ARG PCSC_SHA256=00b667aa71504ed1d39a48ad377de048c70dbe47229e8c48a3239ab62979c70f
RUN apt-get update && apt-get install -y --no-install-recommends \
        swig gcc python3-dev pkg-config \
        wget ca-certificates meson ninja-build flex perl \
    && cd /tmp \
    && wget -qO pcsc.tar.gz "https://github.com/LudovicRousseau/PCSC/archive/refs/tags/${PCSC_VERSION}.tar.gz" \
    && echo "${PCSC_SHA256}  pcsc.tar.gz" | sha256sum -c - \
    && tar xf pcsc.tar.gz && cd "PCSC-${PCSC_VERSION}" \
    && meson setup builddir --prefix=/usr -Dpolkit=false -Dlibsystemd=false -Dlibudev=false \
    && ninja -C builddir \
    && ninja -C builddir install && ldconfig \
    && DESTDIR=/pcsc-runtime ninja -C builddir install \
    && rm -rf /pcsc-runtime/usr/include /pcsc-runtime/usr/share /pcsc-runtime/usr/bin \
              /pcsc-runtime/usr/sbin /pcsc-runtime/etc /pcsc-runtime/var \
    && find /pcsc-runtime -name pkgconfig -type d -prune -exec rm -rf {} + \
    && find /pcsc-runtime -name '*.la' -delete \
    && find /pcsc-runtime ! -type d \
    && rm -rf /tmp/pcsc.tar.gz "/tmp/PCSC-${PCSC_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

# pyscard has no prebuilt wheel and is compiled here against the pcsc-lite installed above.
# --root keeps the console-script shebangs pointing at the FINAL /usr/local/bin/python.
COPY control/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/usr/local --root=/python-deps -r /tmp/requirements.txt

# ---------- stage 3: python runtime ----------
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
ARG MDD_VERSION=dev
# Self-built pcsc-lite runtime library (see stage 2) + the prebuilt python site-packages.
COPY --from=deps /pcsc-runtime/ /
COPY --from=deps /python-deps/usr/local/ /usr/local/
# modemmanager (mmcli) is the only runtime OS dependency; ldconfig registers libpcsclite.so.1.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates modemmanager \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/control

# App code + built WebUI, mirroring the repo layout under /app.
COPY control/ /app/control/
COPY VERSION /app/VERSION
COPY --from=webui /webui/dist /app/webui/dist

# Defaults; the installer overrides MDD_DATA/MDD_HOST_DATA/MDD_ADVERTISE_ADDR/port.
ENV MDD_WEBUI=/app/webui/dist \
    MDD_DATA=/data \
    MDD_HTTP_PORT=8443 \
    PYTHONUNBUFFERED=1

EXPOSE 8443
LABEL io.mdd-sim-gateway.managed="true" \
      org.opencontainers.image.version="${MDD_VERSION}" \
      org.opencontainers.image.title="MDD Sim Gateway control plane"
ENTRYPOINT ["python", "run.py"]
