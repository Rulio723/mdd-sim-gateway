# VoWiFi engine image: pure-Python SWu IKEv2/IPsec tunnel + sysmocom Asterisk + USIM bridge.
# Based on phcoder/asterisk-docker (osmocom "VoWiFi with Asterisk"), with additions:
#   - swu_ike.py    : pure-Python SWu (ePDG) IKEv2/IPsec client (fasferraz/SWu-IKEv2, patched)
#                     — the ONLY tunnel path; verifies the SIM PIN in EAP-AKA, userspace ESP
#   - pin_keeper.py : holds SIM PIN (CHV1) verified so IKE EAP-AKA + SIP IMS-AKA work on PIN-enabled SIMs
#   - ami_usim.py   : reference USIM<->AMI bridge, patched to VERIFY PIN + report status
#   - render.py     : renders /config/instance.json -> Asterisk configs at container start
#
# Reproducibility: source inputs and the multi-architecture base manifest are pinned. Fedora
# packages still resolve from the supported Fedora 44 repositories at build time, so every
# release workflow performs a clean image build instead of claiming byte-for-byte reproducibility.
FROM fedora:44@sha256:6c75d5bf57cb0fa5aa4b92c6a83c86c791644496d9ac230de7711f5b8ec3b898 AS build

# pcsc-lite is pinned to a fixed upstream version and built from source (see below), so the
# engine's PC/SC client library matches the host pcscd EXACTLY across any distro — this is the
# same PCSC_VERSION the installer pins on the host and the control image uses. Avoids the
# client/server protocol mismatch that distro-default pcsc-lite packages cause.
ARG PCSC_VERSION=2.3.3
ARG PCSC_SHA256=00b667aa71504ed1d39a48ad377de048c70dbe47229e8c48a3239ab62979c70f
ARG PJPROJECT_REPOSITORY=https://github.com/MddIdd/pjproject-sysmocom-mirror.git
ARG ASTERISK_REPOSITORY=https://github.com/MddIdd/asterisk-sysmocom-mirror.git

WORKDIR /home/asterisk-build

RUN dnf -y --refresh --setopt=fastestmirror=True --setopt=max_parallel_downloads=20 \
    --setopt=timeout=30 --setopt=minrate=50k --setopt=retries=3 upgrade && \
    dnf -y --setopt=fastestmirror=True --setopt=max_parallel_downloads=20 \
    --setopt=timeout=30 --setopt=minrate=50k --setopt=retries=3 \
    --setopt=install_weak_deps=False install \
    git gcc g++ openssl-devel patch libedit-devel opus-devel libuuid-devel autoconf \
    automake libtool gettext perl python3 flex bison pkg-config m4 gettext-devel gperf gmp-devel \
    jansson-devel libxml2-devel sqlite-devel opencore-amr-devel vo-amrwbenc-devel \
    libmnl-devel speex-devel speexdsp-devel libvorbis-devel alsa-lib-devel portaudio-devel \
    libcurl-devel xmlstarlet postgresql-devel unixODBC-devel neon-devel gmime-devel lua-devel \
    uriparser-devel libxslt-devel mysql-devel bluez-libs-devel radcli-devel freetds-devel \
    jack-audio-connection-kit-devel libcap-devel net-snmp-devel corosynclib-devel newt-devel \
    popt-devel libical-devel spandsp-devel openldap-devel codec2-devel fftw-devel libsndfile-devel \
    unbound-devel wget curl subversion libresample-devel binutils-devel libsrtp-devel gsm-devel iproute \
    procps-ng net-tools python3-pysocks python3-requests python3-pyscard python3-jinja2 \
    python3-devel python3-pip \
    meson ninja-build perl-podlators systemd-devel \
    tcpdump bind-utils iputils

# Build pcsc-lite ${PCSC_VERSION} from source (client library + headers). Installs over any
# distro-provided libpcsclite pulled in by python3-pyscard so pyscard (loads libpcsclite.so.1 at
# runtime) uses the pinned version and talks the same protocol as the host pcscd.
RUN cd /tmp && \
    wget -qO pcsc.tar.gz "https://github.com/LudovicRousseau/PCSC/archive/refs/tags/${PCSC_VERSION}.tar.gz" && \
    echo "${PCSC_SHA256}  pcsc.tar.gz" | sha256sum -c - && \
    tar xf pcsc.tar.gz && cd "PCSC-${PCSC_VERSION}" && \
    meson setup builddir --prefix=/usr -Dpolkit=false && \
    ninja -C builddir && ninja -C builddir install && \
    ldconfig && \
    rm -rf /tmp/pcsc.tar.gz "/tmp/PCSC-${PCSC_VERSION}"

# sysmocom pjproject + Asterisk (VoLTE/VoWiFi/IMS-AKA/SMS-over-IMS fork), identical to reference build
COPY patches/asterisk/ /home/asterisk-build/patches/asterisk/
RUN set -eu; \
    checkout_with_retry() { \
        url="$1"; destination="$2"; revision="$3"; attempt=1; \
        while [ "$attempt" -le 3 ]; do \
            rm -rf "$destination"; \
            git init -q "$destination"; \
            git -C "$destination" remote add origin "$url"; \
            if git -C "$destination" -c http.version=HTTP/1.1 fetch --depth=1 origin "$revision" && \
                git -C "$destination" checkout --detach FETCH_HEAD; then \
                return 0; \
            fi; \
            echo "checkout attempt ${attempt}/3 failed for ${revision}; retrying" >&2; \
            attempt=$((attempt + 1)); \
            sleep 5; \
        done; \
        return 1; \
    }; \
    fetch_with_retry() { \
        repository="$1"; revision="$2"; depth="$3"; attempt=1; \
        while [ "$attempt" -le 3 ]; do \
            if git -C "$repository" -c http.version=HTTP/1.1 fetch --depth="$depth" origin "$revision"; then \
                return 0; \
            fi; \
            echo "fetch attempt ${attempt}/3 failed for ${revision}; retrying" >&2; \
            attempt=$((attempt + 1)); \
            sleep 5; \
        done; \
        return 1; \
    }; \
    checkout_with_retry "$PJPROJECT_REPOSITORY" pjproject 20537ab1e6beb2baf9ec15599e0217a601cf7ce5 && \
    checkout_with_retry "$ASTERISK_REPOSITORY" asterisk d231cb2c658545773fcd5eb9de787219b9ef6566 && \
    cd asterisk && \
    git config user.email "build@vowifi" && git config user.name "vowifi build" && \
    fetch_with_retry . f1b60dcd9568c4045512fd0d8b619b9fb91a7f35 2 && \
    git cherry-pick f1b60dcd9568c4045512fd0d8b619b9fb91a7f35 && \
    for p in /home/asterisk-build/patches/asterisk/*.py; do echo "applying $p"; python3 "$p" || exit 1; done && \
    ln -s ../../../pjproject third-party/pjproject/source && \
    sh bootstrap.sh && \
    ./configure --enable-binary-modules && \
    make menuselect/menuselect menuselect-tree menuselect.makeopts && \
    ./menuselect/menuselect --enable codec_opus --disable BUILD_NATIVE && \
    make -j$(nproc) && \
    make install DOWNLOAD='curl --http1.1 -fL -O -C - --retry 5 --retry-all-errors --connect-timeout 15 --max-time 900 --speed-time 30 --speed-limit 1024'

RUN python3 -m pip install --no-cache-dir panoramisk==1.4

# SWu (python IKEv2/IPsec) tunnel dependencies. swu_ike.py is the ePDG tunnel: it needs
# pycryptodome/cryptography (IKE + ESP crypto), pyserial (imported by the upstream emulator), and
# mitshell's card helpers. AKA secrets stay inside the physical SIM/eSIM; the image does not
# include a software Milenage implementation or accept Ki/OP/OPc.
RUN python3 -m pip install --no-cache-dir pyserial==3.5 pycryptodome==3.23.0 cryptography==50.0.0 \
    "git+https://github.com/mitshell/card.git@790a8fb0b8fbc0b06f568feba51aeef28352145e"

# Derive the Fedora runtime package closure from the binary we just built and every Asterisk
# module we ship. This keeps all codecs and optional modules loadable without carrying compilers,
# headers or source trees into the final image. Packages needed by the Python tunnel and its shell
# supervisor are explicit because they are not visible to ldd.
RUN set -eu; \
    { \
      printf '%s\n' \
        bash coreutils grep hostname iproute net-tools procps-ng \
        tcpdump bind-utils iputils ca-certificates \
        python3 python3-jinja2 python3-pyscard python3-pysocks python3-requests; \
      { printf '%s\n' /usr/sbin/asterisk; find /usr/lib/asterisk/modules -type f -name '*.so'; } | \
        while IFS= read -r binary; do \
          ldd "$binary" 2>/dev/null || true; \
        done | \
        sed -n \
          -e 's/^[[:space:]]*[^[:space:]]* => \([^[:space:]]*\).*/\1/p' \
          -e 's/^[[:space:]]*\(\/[^[:space:]]*\) (.*/\1/p' | \
        sort -u | \
        while IFS= read -r library; do \
          if package=$(rpm -qf --qf '%{NAME}' "$library" 2>/dev/null); then \
            printf '%s\n' "$package"; \
          fi; \
        done; \
    } | sort -u > /runtime-packages.txt

FROM fedora:44@sha256:6c75d5bf57cb0fa5aa4b92c6a83c86c791644496d9ac230de7711f5b8ec3b898 AS runtime

COPY --from=build /runtime-packages.txt /tmp/runtime-packages.txt
RUN dnf -y --refresh --setopt=fastestmirror=True --setopt=max_parallel_downloads=20 \
    --setopt=timeout=30 --setopt=minrate=50k --setopt=retries=3 upgrade && \
    dnf -y --setopt=fastestmirror=True --setopt=max_parallel_downloads=20 \
    --setopt=timeout=30 --setopt=minrate=50k --setopt=retries=3 \
    --setopt=install_weak_deps=False install $(cat /tmp/runtime-packages.txt) && \
    dnf clean all && rm -rf /var/cache/dnf /tmp/runtime-packages.txt

# Asterisk and its bundled pjproject are built outside Fedora's RPM database. Copy only their
# installed runtime trees. The pinned pcsc-lite client overwrites Fedora's client library so it
# continues to match the host pcscd protocol exactly.
COPY --from=build /usr/sbin/asterisk /usr/sbin/asterisk
COPY --from=build /usr/lib/asterisk/ /usr/lib/asterisk/
COPY --from=build /usr/lib/libasteriskpj.so /usr/lib/libasteriskpj.so
COPY --from=build /usr/lib/libasteriskpj.so.2 /usr/lib/libasteriskpj.so.2
COPY --from=build /usr/lib/libasteriskssl.so /usr/lib/libasteriskssl.so
COPY --from=build /usr/lib/libasteriskssl.so.1 /usr/lib/libasteriskssl.so.1
COPY --from=build /var/lib/asterisk/ /var/lib/asterisk/
COPY --from=build /usr/lib64/libpcsclite.so /usr/lib64/libpcsclite.so
COPY --from=build /usr/lib64/libpcsclite.so.1 /usr/lib64/libpcsclite.so.1
COPY --from=build /usr/lib64/libpcsclite_real.so /usr/lib64/libpcsclite_real.so
COPY --from=build /usr/lib64/libpcsclite_real.so.1 /usr/lib64/libpcsclite_real.so.1
COPY --from=build /usr/local/ /usr/local/
RUN mkdir -p /var/cache/asterisk /var/log/asterisk /var/spool/asterisk && ldconfig && \
    missing=0; \
    for binary in /usr/sbin/asterisk /usr/lib/asterisk/modules/*.so; do \
      if ldd "$binary" 2>/dev/null | grep -q 'not found'; then \
        echo "missing runtime dependency: $binary" >&2; \
        ldd "$binary" >&2; \
        missing=1; \
      fi; \
    done; \
    [ "$missing" = 0 ] && \
    python3 -c 'import Crypto, cryptography, jinja2, panoramisk, requests, serial, smartcard, socks, card' && \
    asterisk -V

# our scripts + templates
COPY pin_keeper.py   /usr/local/bin/pin_keeper.py
COPY ami_usim.py     /usr/local/bin/ami_usim.py
COPY swu_ike.py      /usr/local/bin/swu_ike.py
COPY log_capture.py  /usr/local/bin/log_capture.py
COPY render.py       /usr/local/bin/render.py
COPY notify.py       /usr/local/bin/notify.py
COPY templates/      /opt/mdd-sim-gateway/templates/
COPY entrypoint.sh   /entrypoint.sh
RUN chmod +x /usr/local/bin/*.py /entrypoint.sh

# Fingerprints of what went into this image. A later reload compares them against the checkout
# to decide between reusing the image, refreshing the scripts through Dockerfile.overlay, and
# rebuilding from source (see ensure_engine_image in install.sh).
ARG RUNTIME_FP=""
ARG BASE_FP=""
ARG MDD_VERSION=""
LABEL io.mdd-sim-gateway.managed="true" \
      io.mdd-sim-gateway.runtime-fp="${RUNTIME_FP}" \
      io.mdd-sim-gateway.base-fp="${BASE_FP}" \
      org.opencontainers.image.version="${MDD_VERSION}" \
      org.opencontainers.image.source="https://github.com/MddIdd/mdd-sim-gateway" \
      org.opencontainers.image.title="MDD Sim Gateway VoWiFi engine"
ENTRYPOINT ["/entrypoint.sh"]
