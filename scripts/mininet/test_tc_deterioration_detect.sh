#!/usr/bin/env bash
# Fixture tests for tc_deterioration_steps.sh HTB/netem detection.
# Proves detection works for arbitrary Mininet handles (1:, 5:, ...), not only 1:1.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TC_SCRIPT="$ROOT/tc_deterioration_steps.sh"
MOCK_DIR="$(mktemp -d)"
PROFILE="$(mktemp)"

cleanup() {
  rm -rf "$MOCK_DIR" "$PROFILE"
}
trap cleanup EXIT

cat >"$PROFILE" <<'EOF'
IFACE=eth0
0 20ms 0%
EOF

cat >"$MOCK_DIR/tc" <<'MOCK'
#!/usr/bin/env bash
case "$*" in
  *"qdisc show dev"*)
    case "${TC_FIXTURE:-}" in
      htb5)
        cat <<'EOF'
qdisc htb 5: root refcnt 5 r2q 10 default 0x1 direct_packets_stat 0 direct_qlen 1000
 Sent 180 bytes 2 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
qdisc netem 10: parent 5:1 limit 1000 delay 20ms seed 1
 Sent 180 bytes 2 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
EOF
        ;;
      htb1)
        cat <<'EOF'
qdisc htb 1: root refcnt 5 r2q 10 default 0x1 direct_packets_stat 0 direct_qlen 1000
 Sent 180 bytes 2 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
qdisc netem 10: parent 1:1 limit 1000 delay 20ms seed 1
 Sent 180 bytes 2 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
EOF
        ;;
      *)
        echo "unknown fixture ${TC_FIXTURE:-}" >&2
        exit 1
        ;;
    esac
    ;;
  *"class show dev"*)
    case "${TC_FIXTURE:-}" in
      htb5)
        echo "class htb 5:1 root leaf 10: prio 0 rate 30Mbit ceil 30Mbit burst 15Kb cburst 1600b"
        ;;
      htb1)
        echo "class htb 1:1 root leaf 10: prio 0 rate 30Mbit ceil 30Mbit burst 15Kb cburst 1600b"
        ;;
    esac
    ;;
  *"filter show dev"*)
    ;;
  *"qdisc replace dev eth0 parent "*)
    ;;
  *"qdisc add dev eth0 parent "*)
    ;;
  *)
    echo "unsupported tc args: $*" >&2
    exit 1
    ;;
esac
MOCK
chmod +x "$MOCK_DIR/tc"

run_detect() {
  local fixture="$1"
  local want_root="$2"
  local want_parent="$3"
  local want_handle="$4"
  local out rc

  out="$(
    PATH="$MOCK_DIR:$PATH" \
      TC_FIXTURE="$fixture" \
      TC_DETERIORATION_DETECT_ONLY=1 \
      bash "$TC_SCRIPT" "$PROFILE" 2>&1
  )" || rc=$?
  rc="${rc:-0}"

  if [[ "$rc" -ne 0 ]]; then
    echo "FAIL fixture=$fixture: detect exited $rc"
    echo "$out"
    return 1
  fi
  if ! grep -q "detected_root_htb=${want_root}" <<<"$out"; then
    echo "FAIL fixture=$fixture: missing detected_root_htb=${want_root}"
    echo "$out"
    return 1
  fi
  if ! grep -q "detected_htb_parent=${want_parent}" <<<"$out"; then
    echo "FAIL fixture=$fixture: missing detected_htb_parent=${want_parent}"
    echo "$out"
    return 1
  fi
  if ! grep -q "detected_netem_handle=${want_handle}" <<<"$out"; then
    echo "FAIL fixture=$fixture: missing detected_netem_handle=${want_handle}"
    echo "$out"
    return 1
  fi
  if grep -q 'no HTB class 1:x' <<<"$out"; then
    echo "FAIL fixture=$fixture: still depends on literal 1:x"
    echo "$out"
    return 1
  fi
  echo "OK fixture=$fixture root=${want_root} parent=${want_parent} handle=${want_handle}"
}

run_apply() {
  local fixture="$1"
  local out rc

  out="$(
    PATH="$MOCK_DIR:$PATH" \
      TC_FIXTURE="$fixture" \
      bash "$TC_SCRIPT" "$PROFILE" 2>&1
  )" || rc=$?
  rc="${rc:-0}"

  if [[ "$rc" -ne 0 ]]; then
    echo "FAIL fixture=$fixture: apply exited $rc"
    echo "$out"
    return 1
  fi
  if ! grep -q "step 1/1 at=0s delay=20ms loss=0%" <<<"$out"; then
    echo "FAIL fixture=$fixture: missing step 1/1 apply log"
    echo "$out"
    return 1
  fi
  if ! grep -q "finished all steps" <<<"$out"; then
    echo "FAIL fixture=$fixture: missing finished all steps"
    echo "$out"
    return 1
  fi
  if grep -q "root became netem" <<<"$out"; then
    echo "FAIL fixture=$fixture: root HTB preservation check failed"
    echo "$out"
    return 1
  fi
  echo "OK fixture=$fixture apply finished"
}

fail=0
run_detect htb1 "1:" "1:1" "10:" || fail=1
run_detect htb5 "5:" "5:1" "10:" || fail=1
run_apply htb5 || fail=1

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi

echo "all tc deterioration detection fixture tests passed"
