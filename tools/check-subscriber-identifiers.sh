#!/bin/sh
# Fail if a real subscriber identifier looks like it has been committed.
#
# RELEASE_CHECKLIST.md has always required this scan, but it was a manual step that nothing
# enforced -- so a real MSISDN pair, six ICCIDs and three modem IMEIs sat in the test fixtures
# from the initial public release through v1.3.15 without anyone noticing. A rule that only
# lives in a document is a rule that gets skipped; this runs in CI instead.
#
# The check is deliberately shaped to fail LOUDLY on anything it cannot recognise as fictional,
# because the cost of a false positive (add it to the allow-list below, with a reason) is far
# lower than the cost of a miss (a subscriber identifier published irrevocably).
#
# Usage: tools/check-subscriber-identifiers.sh [path...]   (default: git-tracked source files)
#        tools/check-subscriber-identifiers.sh --commits <git rev-list arguments...>
#
# The --commits form scans every blob those commits introduce, which is what hooks/pre-push
# uses. Scanning the working tree is not enough before a push: a value that was committed and
# then removed is still in the history being published, and a push cannot be taken back.
set -eu

cd "$(dirname "$0")/.."

scanned_extensions() {
    case "$1" in
        *.py|*.js|*.jsx|*.sh|*.md|*.yml|*.yaml|*.json) return 0 ;;
        *) return 1 ;;
    esac
}

tmp=""
if [ "${1:-}" = "--commits" ]; then
    shift
    [ "$#" -gt 0 ] || { echo "--commits needs at least one revision" >&2; exit 2; }
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT INT TERM
    # rev-list --objects lists trees as well as blobs, and the same path can appear at several
    # blobs across the range -- each is a separate published version and each gets scanned.
    git rev-list --objects "$@" | while read -r object path; do
        [ -n "$path" ] || continue
        scanned_extensions "$path" || continue
        [ "$path" = "webui/package-lock.json" ] && continue
        [ "$(git cat-file -t "$object" 2>/dev/null)" = blob ] || continue
        destination="$tmp/$object/$path"
        mkdir -p "$(dirname "$destination")"
        git cat-file blob "$object" > "$destination"
        printf '%s\n' "$destination"
    done > "$tmp/.files"
    files=$(cat "$tmp/.files")
elif [ "$#" -gt 0 ]; then
    files=$(printf '%s\n' "$@")
else
    files=$(git ls-files '*.py' '*.js' '*.jsx' '*.sh' '*.md' '*.yml' '*.yaml' '*.json' \
        | grep -v '^webui/package-lock.json$')
fi

status=0

report() {
    # $1 = human-readable kind, $2 = matches (file:line:value)
    if [ -n "$2" ]; then
        printf '\n%s that are not recognisably fictional:\n%s\n' "$1" "$2"
        status=1
    fi
}

# A value is accepted as fictional when it matches one of these. Each entry needs a reason.
# Compared against the digits alone, with any leading '+' stripped.
#   0{6,}            zero-filled body -- the conventional "obviously made up" form
#   123456789        ascending-digit filler anywhere (MCC/MNC + 123456789 sample IMSIs)
#   ^123456          the same filler leading a value
#   ^00101           MCC 001 / MNC 01: the reserved test network (3GPP TS 23.122)
#   ^35000000        the TAC used by this repo's fictional IMEIs
#   ^490154203237518 the IMEI from the public worked example (3GPP/GSMA documentation)
#   ^44770090        Ofcom's 07700 900xxx drama range, reserved for fiction
#   ^1([0-9]{3})?555 NANP fictional 555, whether as the exchange or in place of the area code
#   ^447785016005    Vodafone UK's published SMSC -- carrier infrastructure, not a subscriber
fictional='0{6,}|123456789|^123456|^00101|^35000000|^490154203237518|^44770090|^1([0-9]{3})?555|^447785016005'

display() {
    # A blob extracted for --commits lives at $tmp/<object>/<path>; report it as the path it
    # has in the tree, with the object it came from, so the offending commit can be found.
    case "${tmp:-}" in
        "") printf '%s' "$1"; return ;;
    esac
    case "$1" in
        "$tmp"/*)
            rest=${1#"$tmp"/}
            printf '%s (blob %s)' "${rest#*/}" "$(printf '%s' "${rest%%/*}" | cut -c1-7)" ;;
        *) printf '%s' "$1" ;;
    esac
}

scan() {
    # $1 = regex for the identifier, $2 = label
    matches=""
    for f in $files; do
        [ -f "$f" ] || continue
        hits=$(grep -noE "$1" "$f" 2>/dev/null || true)
        [ -n "$hits" ] || continue
        for hit in $hits; do
            line=${hit%%:*}
            value=${hit#*:}
            # Compare on digits alone: the allow-list anchors with '^', which a leading
            # '+' would otherwise defeat.
            printf '%s' "$value" | tr -d '+' | grep -qE "$fictional" && continue
            matches="$matches  $(display "$f"):$line: $value
"
        done
    done
    report "$2" "$matches"
}

# ICCID: 89 (telecom industry) + 15-18 more digits.
scan '\b89[0-9]{15,18}\b' 'ICCIDs'

# IMEI and IMSI are both 15 digits; treat every bare 15-digit run as suspect.
scan '\b[0-9]{15}\b' 'IMEIs / IMSIs'

# E.164 numbers: UK mobile and NANP. Written with or without a leading '+'.
scan '\+?\b447[0-9]{9}\b' 'UK mobile numbers'
scan '\+1[0-9]{10}\b' 'NANP numbers'

if [ "$status" -ne 0 ]; then
    cat <<'EOF'

Each value above is either a real subscriber identifier -- which must not be committed, see
docs/RELEASE_CHECKLIST.md -- or a fictional one this check does not recognise. If it is
fictional, make that evident: zero-fill the body, or add the pattern to the allow-list in
tools/check-subscriber-identifiers.sh together with the reason it is safe.
EOF
    exit 1
fi

echo "No subscriber identifiers found outside the fictional ranges."
