#!/bin/sh
# Writes the version of each toolchain the agents build with as a node_exporter
# textfile-collector metric, so Kelpie can name a version change as the likely cause of an
# outage that starts soon after it (incident 20: an Xcode update broke every iOS build):
#
#   kelpie_toolchain_info{tool="xcode",version="16.1 (16B40)"} 1
#
# Usage: kelpie-toolchain.sh OUTPUT_DIR [TOOL=COMMAND ...]
#
# OUTPUT_DIR is node_exporter's --collector.textfile.directory. Each TOOL=COMMAND runs
# COMMAND with sh -c and takes the first line it prints as TOOL's version (TOOL: lower-case
# letters, digits, ".", "_" and "-"), e.g. 'node=node --version'; at most 100 characters of
# it (printable ASCII) are kept. Without any, on macOS it reads the selected Xcode
# ("16.1 (16B40)", from its Info.plist and version.plist, not xcodebuild, which can fail
# for a moment) and CoreSimulator (its framework's CFBundleVersion); elsewhere it writes
# no versions.
# Report one version per tool: two at once read as changes back and forth. A command that
# fails or prints nothing is skipped with a message on stderr. Each gets at most
# KELPIE_TOOL_TIMEOUT seconds (20), through timeout(1), gtimeout or else perl's alarm (macOS
# ships perl, not timeout). The file is written to a temporary name and renamed, so
# node_exporter never reads half of it. Run it every few minutes (deploy/textfile has a
# launchd agent): a change is timed to the run that sees it. Kelpie reads only the metric
# name and the two labels, all config data ([resources.prometheus] toolchain_metric,
# toolchain_tool_label, toolchain_version_label).
set -eu

if [ "$#" -lt 1 ]; then
  echo "usage: $0 OUTPUT_DIR [TOOL=COMMAND ...]" >&2
  exit 2
fi
out_dir=$1
shift
[ -d "$out_dir" ] || { echo "$0: $out_dir is not a directory" >&2; exit 2; }

seconds=${KELPIE_TOOL_TIMEOUT:-20}
case "$seconds" in
  '' | *[!0-9]*) echo "$0: KELPIE_TOOL_TIMEOUT must be whole seconds" >&2; exit 2 ;;
esac

plistbuddy=${KELPIE_PLISTBUDDY:-/usr/libexec/PlistBuddy}  # macOS's; a test may swap it

# Runs "$@" for at most $seconds.
bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$seconds" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$seconds" "$@"
  elif command -v perl >/dev/null 2>&1; then
    perl -e 'alarm shift; exec @ARGV or exit 127' "$seconds" "$@"
  else
    "$@"
  fi
}

# The selected Xcode's version and build ("16.1 (16B40)") from its own plists: no
# xcodebuild, which can be slow or fail for a moment (a false change each time).
xcode_version() {
  dir=$(xcode-select -p 2>/dev/null || true)
  [ -n "$dir" ] || return 0
  v=$("$plistbuddy" -c 'Print :CFBundleShortVersionString' "$dir/../Info.plist" \
    2>/dev/null || true)
  b=$("$plistbuddy" -c 'Print :ProductBuildVersion' "$dir/../version.plist" \
    2>/dev/null || true)
  [ -n "$v" ] || return 0
  if [ -n "$b" ]; then printf '%s (%s)\n' "$v" "$b"; else printf '%s\n' "$v"; fi
}

coresimulator_version() {
  "$plistbuddy" -c 'Print :CFBundleVersion' \
    /Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/Resources/Info.plist \
    2>/dev/null || true
}

# One metric line for tool $1 with the first line of $2 as its version, or a message.
emit() {
  version=$(printf '%s\n' "$2" | head -n 1 | LC_ALL=C tr -cd '\040-\176' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | cut -c1-100)
  if [ -z "$version" ]; then
    echo "$0: skipped $1: no version" >&2
    return 0
  fi
  # Label values escape backslash and double quote (Prometheus text format).
  label=$(printf '%s' "$version" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
  printf 'kelpie_toolchain_info{tool="%s",version="%s"} 1\n' "$1" "$label"
}

tmp=
trap '[ -z "$tmp" ] || rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM  # through the EXIT trap: no temporary file left behind
tmp=$(mktemp "$out_dir/.kelpie_toolchain.XXXXXX")
{
  echo "# HELP kelpie_toolchain_info Active toolchain versions (scripts/kelpie-toolchain.sh)."
  echo "# TYPE kelpie_toolchain_info gauge"
  if [ "$#" -eq 0 ] && [ "$(uname -s)" = Darwin ]; then
    emit xcode "$(xcode_version)"
    emit coresimulator "$(coresimulator_version)"
  fi
  for spec in "$@"; do
    tool=${spec%%=*}
    cmd=${spec#*=}
    if [ "$tool" = "$spec" ] || [ -z "$cmd" ]; then
      echo "$0: skipped '$spec': not TOOL=COMMAND" >&2
      continue
    fi
    case "$tool" in
      '' | *[!a-z0-9._-]*) echo "$0: skipped tool '$tool': use a-z, 0-9, '.', '_', '-'" >&2; continue ;;
    esac
    emit "$tool" "$(bounded sh -c "$cmd" 2>/dev/null || true)"
  done
} >"$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$out_dir/kelpie_toolchain.prom"
