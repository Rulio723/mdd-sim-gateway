#!/usr/bin/env bash
# Read-only USB passthrough diagnostics for a gateway running inside a Proxmox VM.
#
# The WebUI support bundle answers the card-path questions from inside the gateway, but a VM
# cannot see the layer above it: when passthrough breaks, the guest only observes "the modem is
# gone" while the reason lives in the host's USB/QEMU state. Run this on both sides and compare.
#
#   On the PVE host:   bash pve-usb-diagnose.sh host <VMID>
#   Inside that VM:    bash pve-usb-diagnose.sh guest
#
# With no arguments the mode is detected from the presence of `qm`. Nothing is modified.
#
# Environment:
#   MDD_USB_ID   modem USB id to look for            (default 2c7c:0125, the EC25 family)
#   MDD_REDACT   set to 0 to keep long digit strings (default 1: mask IMSI/ICCID/IMEI-shaped runs)

set -u

MODE="${1:-auto}"
VMID="${2:-}"
STAMP="$(date '+%Y%m%d-%H%M%S')"
USB_ID="${MDD_USB_ID:-2c7c:0125}"
REDACT="${MDD_REDACT:-1}"

if [[ "$MODE" == "auto" ]]; then
  if command -v qm >/dev/null 2>&1; then
    MODE="host"
  else
    MODE="guest"
  fi
fi

if [[ "$MODE" != "host" && "$MODE" != "guest" ]]; then
  echo "Usage: bash $0 [host <VMID>|guest]" >&2
  exit 2
fi

# No default VM id: querying the wrong VM produces a plausible report about unrelated hardware,
# which is worse than refusing to run.
if [[ "$MODE" == "host" && -z "$VMID" ]]; then
  echo "Usage: bash $0 host <VMID>   (the VM id running the gateway)" >&2
  exit 2
fi

REPORT="${PWD}/mdd-usb-diagnose-${MODE}-${STAMP}.txt"

section() {
  printf '\n===== %s =====\n' "$1"
}

run() {
  printf '\n$'
  printf ' %q' "$@"
  printf '\n'
  "$@" 2>&1 || printf '[command exited with status %s]\n' "$?"
}

run_shell() {
  local description="$1"
  shift
  printf '\n$ %s\n' "$description"
  bash -o pipefail -c "$*" 2>&1 || printf '[command exited with status %s]\n' "$?"
}

# Subscriber identifiers are 14-21 digit runs (IMSI 15, IMEI 15, ICCID 19-20). USB ids are hex
# and port numbers are far shorter, so both survive. This is a blunt filter over a report meant
# to be shared -- it is not the WebUI bundle's structural redaction.
redact() {
  if [[ "$REDACT" == "0" ]]; then
    cat
  else
    sed -E 's/[0-9]{14,21}/[redacted-id]/g'
  fi
}

monitor_snapshot() {
  section "QEMU monitor USB state"
  printf '%s\n' 'The following is a read-only monitor query: info usbhost; info usb.'
  if ! command -v timeout >/dev/null 2>&1; then
    printf '%s\n' 'timeout is unavailable; run `qm monitor VMID`, then `info usbhost`, `info usb`, `quit` manually.'
    return
  fi
  printf '\n$ qm monitor %q  # scripted read-only query\n' "$VMID"
  timeout 10s qm monitor "$VMID" <<'EOF' 2>&1 || \
    printf '[monitor query did not complete; run it manually if the output above is empty]\n'
info usbhost
info usb
quit
EOF
}

collect_host() {
  section "Report metadata"
  printf 'role=PVE-host\nvmid=%s\nusb_id=%s\ncreated_at=%s\n' \
    "$VMID" "$USB_ID" "$(date --iso-8601=seconds 2>/dev/null || date)"
  printf '%s\n' 'read_only=true'

  section "PVE and kernel"
  run uname -a
  if command -v pveversion >/dev/null 2>&1; then
    run pveversion -v
  fi

  section "VM state and selected configuration"
  run qm status "$VMID"
  run_shell "qm config $VMID | selected non-secret USB/args lines" \
    "qm config '$VMID' | sed -n -E '/^(name|machine|hostpci[0-9]+|usb[0-9]+|args):/p'"

  section "Physical modem visibility on PVE"
  if command -v lsusb >/dev/null 2>&1; then
    run lsusb -d "$USB_ID"
    run lsusb -t
  else
    printf '%s\n' 'lsusb is unavailable on the PVE host.'
  fi
  run_shell "USB device nodes for $USB_ID" \
    "for d in /sys/bus/usb/devices/*; do [ -f \"\$d/idVendor\" ] || continue; [ \"\$(cat \"\$d/idVendor\" 2>/dev/null):\$(cat \"\$d/idProduct\" 2>/dev/null)\" = '$USB_ID' ] || continue; printf 'sysfs=%s authorized=%s product=%s\\n' \"\$d\" \"\$(cat \"\$d/authorized\" 2>/dev/null)\" \"\$(cat \"\$d/product\" 2>/dev/null)\"; done"

  monitor_snapshot

  section "Recent PVE USB kernel events"
  run_shell "dmesg -T | USB/error filter | tail -250" \
    "dmesg -T | grep -Ei 'usb|xhci|${USB_ID%%:*}|disconnect|reset|device descriptor|error|fail' | tail -250"

  section "Relevant PVE processes"
  run_shell "QEMU USB arguments for VM $VMID" \
    "pid=\$(pgrep -f 'kvm.*-id[[:space:]]+$VMID([[:space:]]|$)' | head -1); if [ -n \"\$pid\" ]; then tr '\\0' ' ' < /proc/\$pid/cmdline | grep -oE -- '-device (qemu-xhci|usb-host)[^ ]*' || true; else echo 'QEMU process not found by VM id'; fi"
}

collect_guest() {
  section "Report metadata"
  printf 'role=VM-guest\nusb_id=%s\ncreated_at=%s\n' \
    "$USB_ID" "$(date --iso-8601=seconds 2>/dev/null || date)"
  printf '%s\n' 'read_only=true'

  section "Guest OS and virtualization"
  run uname -a
  if command -v systemd-detect-virt >/dev/null 2>&1; then
    run systemd-detect-virt
  fi
  run_shell "OS release" "sed -n '1,80p' /etc/os-release"

  section "Physical modem visibility inside guest"
  if command -v lsusb >/dev/null 2>&1; then
    run lsusb -d "$USB_ID"
    run lsusb -t
  else
    printf '%s\n' 'lsusb is unavailable inside the guest.'
  fi
  run_shell "modem character/network devices" \
    "find /dev -maxdepth 1 \( -name 'ttyUSB*' -o -name 'ttyACM*' -o -name 'cdc-wdm*' \) -ls 2>/dev/null | sort"
  run_shell "matching USB sysfs entries and bound drivers" \
    "for d in /sys/bus/usb/devices/*; do [ -f \"\$d/idVendor\" ] || continue; [ \"\$(cat \"\$d/idVendor\" 2>/dev/null):\$(cat \"\$d/idProduct\" 2>/dev/null)\" = '$USB_ID' ] || continue; printf 'sysfs=%s authorized=%s product=%s\\n' \"\$d\" \"\$(cat \"\$d/authorized\" 2>/dev/null)\" \"\$(cat \"\$d/product\" 2>/dev/null)\"; find \"\$d\" -maxdepth 3 -type l -name driver -printf 'driver-link=%p -> %l\\n' 2>/dev/null; done"

  section "MDD and ModemManager service state"
  run systemctl is-active mdd-sim-gateway-control.service mdd-sim-gateway-orchestrator.service ModemManager.service
  run systemctl is-enabled mdd-sim-gateway-control.service mdd-sim-gateway-orchestrator.service ModemManager.service
  run_shell "orchestrator ExecStart" \
    "systemctl show mdd-sim-gateway-orchestrator.service -p ExecStart --no-pager"

  section "Recent guest USB kernel events"
  run_shell "dmesg -T | USB/modem/error filter | tail -250" \
    "dmesg -T | grep -Ei 'usb|xhci|ttyUSB|ttyACM|cdc-wdm|option|qmi|${USB_ID%%:*}|disconnect|reset|device descriptor|error|fail' | tail -250"

  section "Recent orchestrator events"
  run journalctl -u mdd-sim-gateway-orchestrator.service --since=-45min --no-pager -n 300

  section "Live bridge and listener state"
  run_shell "bridge processes" "pgrep -af 'vpcd_modem_bridge|mdd_orchestrator' || true"
  run_shell "MDD VPCD listening ports" \
    "ss -lntp 2>/dev/null | grep -E ':(1536[0-9]|1561[0-9]|1587[0-9]|1612[0-9])([[:space:]]|$)' || true"
}

umask 077
{
  printf '%s\n' 'MDD Sim Gateway USB passthrough diagnostic report'
  printf '%s\n' 'This script performed read-only checks and made no system changes.'
  if [[ "$REDACT" == "0" ]]; then
    printf '%s\n' 'WARNING: MDD_REDACT=0 -- subscriber identifiers are NOT masked in this report.'
  fi
  if [[ "$MODE" == "host" ]]; then
    if ! command -v qm >/dev/null 2>&1; then
      printf '%s\n' 'ERROR: host mode requires the Proxmox qm command.'
      exit 1
    fi
    collect_host
  else
    collect_guest
  fi
} | redact | tee "$REPORT"

printf '\nReport saved to: %s\n' "$REPORT"
printf '%s\n' 'Review it before sharing, and attach the WebUI redacted support bundle alongside.'
