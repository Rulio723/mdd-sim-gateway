#!/usr/bin/env bash
# Safely bind exactly two Quectel/DJI EC25 modems to one PVE VM.
#
# Preview only:
#   bash pve-bind-ec25-modems.sh 104
# Apply (gracefully restarts the VM only when it was already running):
#   bash pve-bind-ec25-modems.sh --apply 104

set -Eeuo pipefail

VID="2c7c"
PID="0125"
APPLY=0
VMID=""
SYS_USB_ROOT="${MDD_PVE_SYS_USB_ROOT:-/sys/bus/usb/devices}"
PVE_CONFIG_ROOT="${MDD_PVE_CONFIG_ROOT:-/etc/pve/qemu-server}"
USB_DEV_ROOT="${MDD_PVE_USB_DEV_ROOT:-/dev/bus/usb}"
PROC_ROOT="${MDD_PVE_PROC_ROOT:-/proc}"

usage() {
  cat <<'EOF'
Usage: pve-bind-ec25-modems.sh [--apply] VMID

Without --apply, prints the detected physical USB paths and proposed qm args.
With --apply, requires root, gracefully shuts down a running VM, replaces the
known MDD two-modem args, and starts the VM again if it was running before.

Safety rules:
  * exactly two 2c7c:0125 devices must be present;
  * existing PVE usbN entries cause a refusal (avoids duplicate passthrough);
  * another VM configured for or holding either physical port causes a refusal;
  * unrelated custom qm args are never overwritten.
EOF
}

while (($#)); do
  case "$1" in
    --apply) APPLY=1 ;;
    -h|--help) usage; exit 0 ;;
    --*) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -n "$VMID" ]]; then
        printf 'Only one VMID may be supplied.\n' >&2
        exit 2
      fi
      VMID="$1"
      ;;
  esac
  shift
done

if [[ ! "$VMID" =~ ^[1-9][0-9]*$ ]]; then
  usage >&2
  exit 2
fi
if ! command -v qm >/dev/null 2>&1; then
  printf 'ERROR: qm was not found; run this script on the PVE host.\n' >&2
  exit 1
fi
if ((APPLY)) && [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  printf 'ERROR: --apply must run as root on the PVE host.\n' >&2
  exit 1
fi
qm status "$VMID" >/dev/null

rows=()
for device in "$SYS_USB_ROOT"/*; do
  [[ -f "$device/idVendor" && -f "$device/idProduct" ]] || continue
  [[ "$(<"$device/idVendor")" == "$VID" ]] || continue
  [[ "$(<"$device/idProduct")" == "$PID" ]] || continue
  [[ -f "$device/busnum" && -f "$device/devpath" && -f "$device/devnum" ]] || continue
  bus="$(<"$device/busnum")"
  port="$(<"$device/devpath")"
  devnum="$(<"$device/devnum")"
  [[ "$bus" =~ ^[0-9]+$ && "$port" =~ ^[0-9]+([.][0-9]+)*$ ]] || {
    printf 'ERROR: invalid USB topology at %s (bus=%q port=%q).\n' \
      "$device" "$bus" "$port" >&2
    exit 1
  }
  rows+=("$bus|$port|$devnum|${device##*/}")
done

if ((${#rows[@]} != 2)); then
  printf 'ERROR: expected exactly two %s:%s modems, found %d. No change made.\n' \
    "$VID" "$PID" "${#rows[@]}" >&2
  command -v lsusb >/dev/null 2>&1 && lsusb -d "$VID:$PID" >&2 || true
  exit 1
fi

sorted_rows=()
while IFS= read -r row; do
  sorted_rows+=("$row")
done < <(printf '%s\n' "${rows[@]}" | sort -t'|' -k1,1n -k2,2V)
rows=("${sorted_rows[@]}")
IFS='|' read -r bus1 port1 devnum1 sysfs1 <<<"${rows[0]}"
IFS='|' read -r bus2 port2 devnum2 sysfs2 <<<"${rows[1]}"

printf 'Detected modem 1: bus=%s port=%s device=%s sysfs=%s\n' \
  "$bus1" "$port1" "$devnum1" "$sysfs1"
printf 'Detected modem 2: bus=%s port=%s device=%s sysfs=%s\n' \
  "$bus2" "$port2" "$devnum2" "$sysfs2"

target_topologies=("$bus1-$port1" "$bus2-$port2")
config_conflicts=()
if [[ -d "$PVE_CONFIG_ROOT" ]]; then
  shopt -s nullglob
  for other_config in "$PVE_CONFIG_ROOT"/*.conf; do
    other_vmid="${other_config##*/}"
    other_vmid="${other_vmid%.conf}"
    [[ "$other_vmid" == "$VMID" ]] && continue

    while IFS= read -r line; do
      [[ "$line" =~ ^usb[0-9]+: ]] || continue
      if [[ "$line" == *"host=$VID:$PID"* ]]; then
        config_conflicts+=("VM $other_vmid: $line")
        continue
      fi
      for topology in "${target_topologies[@]}"; do
        [[ "$line" == *"host=$topology"* ]] || continue
        config_conflicts+=("VM $other_vmid: $line")
      done
    done <"$other_config"

    other_args="$(sed -n 's/^args: //p' "$other_config")"
    [[ -n "$other_args" ]] || continue
    for row in "${rows[@]}"; do
      IFS='|' read -r other_bus other_port _ _ <<<"$row"
      if [[ "$other_args" =~ hostbus=$other_bus,hostport=$other_port([,[:space:]]|$) ]]; then
        config_conflicts+=("VM $other_vmid args: $other_args")
      fi
    done
  done
  shopt -u nullglob
fi

if ((${#config_conflicts[@]})); then
  printf 'ERROR: another VM is configured for the detected EC25 USB device(s):\n' >&2
  printf '  %s\n' "${config_conflicts[@]}" >&2
  printf 'Remove the duplicate passthrough before binding VM %s. No change made.\n' \
    "$VMID" >&2
  exit 1
fi

if ! command -v fuser >/dev/null 2>&1; then
  printf 'ERROR: fuser was not found; cannot verify exclusive USB ownership.\n' >&2
  exit 1
fi

holder_conflicts=()
for row in "${rows[@]}"; do
  IFS='|' read -r holder_bus _ holder_devnum _ <<<"$row"
  device_node="$(printf '%s/%03d/%03d' "$USB_DEV_ROOT" "$holder_bus" "$holder_devnum")"
  while IFS= read -r holder_pid; do
    cmdline_path="$PROC_ROOT/$holder_pid/cmdline"
    if [[ ! -r "$cmdline_path" ]]; then
      holder_conflicts+=("PID $holder_pid holds $device_node (command unavailable)")
      continue
    fi
    holder_cmd="$(tr '\0' ' ' <"$cmdline_path")"
    holder_vmid="$(sed -n -E 's/.*(^|[[:space:]])-id[[:space:]]+([0-9]+)([[:space:]]|$).*/\2/p' <<<"$holder_cmd")"
    if [[ "$holder_vmid" != "$VMID" ]]; then
      holder_conflicts+=("PID $holder_pid VM ${holder_vmid:-unknown} holds $device_node")
    fi
  done < <(fuser "$device_node" 2>/dev/null | tr ' ' '\n' | sed -n '/^[0-9][0-9]*$/p')
done

if ((${#holder_conflicts[@]})); then
  printf 'ERROR: another process or VM already holds the detected EC25 USB device(s):\n' >&2
  printf '  %s\n' "${holder_conflicts[@]}" >&2
  printf 'Stop the conflicting VM/process before binding VM %s. No change made.\n' \
    "$VMID" >&2
  exit 1
fi

new_args="-device qemu-xhci,id=x1 -device qemu-xhci,id=x2"
new_args+=" -device usb-host,hostbus=$bus1,hostport=$port1,bus=x1.0"
new_args+=" -device usb-host,hostbus=$bus2,hostport=$port2,bus=x2.0"

config="$(qm config "$VMID")"
current_args="$(sed -n 's/^args: //p' <<<"$config")"
usb_lines="$(sed -n -E '/^usb[0-9]+:/p' <<<"$config")"

if [[ -n "$usb_lines" ]]; then
  printf 'ERROR: VM %s has PVE usbN passthrough entries:\n%s\n' "$VMID" "$usb_lines" >&2
  printf 'Remove or review them manually first; combining both methods duplicates devices.\n' >&2
  exit 1
fi

# Only replace the exact two-controller/two-device layout managed by this script. Anything
# else may carry unrelated QEMU settings and must be reviewed by a human.
mdd_args_re='^-device qemu-xhci,id=x1 -device qemu-xhci,id=x2 -device usb-host,hostbus=[0-9]+,hostport=[0-9]+([.][0-9]+)*,bus=x1[.]0 -device usb-host,hostbus=[0-9]+,hostport=[0-9]+([.][0-9]+)*,bus=x2[.]0$'
if [[ -n "$current_args" && ! "$current_args" =~ $mdd_args_re ]]; then
  printf 'ERROR: VM %s has unrelated custom args; refusing to overwrite them:\n%s\n' \
    "$VMID" "$current_args" >&2
  exit 1
fi

printf 'Current args:  %s\n' "${current_args:-(none)}"
printf 'Proposed args: %s\n' "$new_args"

if [[ "$current_args" == "$new_args" ]]; then
  printf 'VM %s is already bound to the current physical USB ports.\n' "$VMID"
  exit 0
fi
if ((!APPLY)); then
  printf 'Preview only; run again with --apply to update VM %s.\n' "$VMID"
  exit 0
fi

stamp="$(date '+%Y%m%d-%H%M%S')"
backup="/root/mdd-pve-usb-bind-${VMID}-${stamp}.txt"
umask 077
{
  printf 'vmid=%s\ncreated_at=%s\n' "$VMID" "$stamp"
  printf 'previous_args=%s\n' "$current_args"
  printf 'new_args=%s\n' "$new_args"
} >"$backup"
printf 'Binding record: %s\n' "$backup"

before="$(qm status "$VMID" | awk '{print $2}')"
if [[ "$before" == "running" ]]; then
  printf 'Gracefully shutting down VM %s...\n' "$VMID"
  qm shutdown "$VMID" --timeout 60
fi
after="$(qm status "$VMID" | awk '{print $2}')"
if [[ "$after" != "stopped" ]]; then
  printf 'ERROR: VM %s did not stop; args were not changed.\n' "$VMID" >&2
  exit 1
fi

qm set "$VMID" --args "$new_args"
printf 'Applied:\n'
qm config "$VMID" | sed -n -E '/^(args|usb[0-9]+):/p'

if [[ "$before" == "running" ]]; then
  qm start "$VMID"
  printf 'VM %s restarted; status: ' "$VMID"
  qm status "$VMID" | awk '{print $2}'
else
  printf 'VM %s was already stopped and remains stopped.\n' "$VMID"
fi
