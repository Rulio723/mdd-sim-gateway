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
set -eu

cd "$(dirname "$0")/.."

if [ "$#" -gt 0 ]; then
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
            matches="$matches  $f:$line: $value
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
