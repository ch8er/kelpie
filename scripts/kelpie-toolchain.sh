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
# letters, digits, ".", "_" and "-"), e.g. 'node=node --version'. Without any, on macOS it
# reads the active Xcode (xcodebuild -version: "16.1 (16B40)") and CoreSimulator (its
# framework's CFBundleVersion); elsewhere it writes no versions. A command that fails or
# prints nothing is skipped with a message on stderr. Each gets at most KELPIE_TOOL_TIMEOUT
# seconds (20) when timeout(1) or gtimeout is on the PATH. The file is written to a
# temporary name and renamed, so node_exporter never reads half of it. Run it every few
# minutes (deploy/textfile has a launchd agent): a change is timed to the run that sees it.
# Kelpie reads only the metric name and the two labels, all config data
# ([resources.prometheus] toolchain_metric, toolchain_tool_label, toolchain_version_label).
set -eu

if [ "$#" -lt 1 ]; then
  echo "usage: $0 OUTPUT_DIR [TOOL=COMMAND ...]" >&2
  exit 2
fi
out_dir=$1
shift
[ -d "$out_dir" ] || { echo "$0: $out_dir is not a directory" >&2; exit 2; }

limit=
for t in timeout gtimeout; do
  if command -v "$t" >/dev/null 2>&1; then
    limit="$t ${KELPIE_TOOL_TIMEOUT:-20}"
    break
  fi
done

if [ "$#" -eq 0 ] && [ "$(uname -s)" = Darwin ]; then
  plist=/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/Resources/Info.plist
  set -- \
    'xcode=xcodebuild -version | awk '\''NR==1{v=$2} /^Build version/{b=$3} END{if (v) print v (b ? " (" b ")" : "")}'\''' \
    "coresimulator=/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' $plist"
fi

tmp=
trap '[ -z "$tmp" ] || rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM  # through the EXIT trap: no temporary file left behind
tmp=$(mktemp "$out_dir/.kelpie_toolchain.XXXXXX")
{
  echo "# HELP kelpie_toolchain_info Active toolchain versions (scripts/kelpie-toolchain.sh)."
  echo "# TYPE kelpie_toolchain_info gauge"
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
    version=$($limit sh -c "$cmd" 2>/dev/null | head -n 1 || true)
    # trim, and drop control characters: a label value is one line
    version=$(printf '%s' "$version" | tr -d '\000-\037\177' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    if [ -z "$version" ]; then
      echo "$0: skipped $tool: its command printed no version" >&2
      continue
    fi
    # Label values escape backslash and double quote (Prometheus text format).
    label=$(printf '%s' "$version" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    printf 'kelpie_toolchain_info{tool="%s",version="%s"} 1\n' "$tool" "$label"
  done
} >"$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$out_dir/kelpie_toolchain.prom"
