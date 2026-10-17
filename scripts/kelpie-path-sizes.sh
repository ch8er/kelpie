#!/bin/sh
# Writes the size of each given directory as a node_exporter textfile-collector metric, so
# Kelpie's rule 7 can name what is filling a disk (incident 19):
#
#   kelpie_path_bytes{path="/Users/agent/Library/Developer/CoreSimulator/Devices",mountpoint="/System/Volumes/Data"} 36507222016
#
# Usage: kelpie-path-sizes.sh OUTPUT_DIR DIRECTORY [DIRECTORY ...]
#
# OUTPUT_DIR is node_exporter's --collector.textfile.directory. The file is written to a
# temporary name and renamed, so node_exporter never reads half of it. Run it every 15
# minutes (deploy/textfile has a launchd agent and a systemd timer): du walks every file,
# so pick the few directories that grow (simulators, caches, worktrees), not the whole disk.
# Each size stays on the directory's own filesystem (du -x), and ``mountpoint`` names that
# filesystem as node_exporter does, so Kelpie lists a directory under the disk it fills.
# Directories must be absolute paths without a newline; one that does not exist, or that du
# gives no size for, is skipped with a message on stderr (one du reads only in part is
# reported with what it could read), and one given twice is measured once.
# Each du gets at most KELPIE_DU_TIMEOUT seconds (120) when timeout(1) or gtimeout
# (Homebrew coreutils) is on the PATH, so one hung network mount costs only its own line.
set -eu

if [ "$#" -lt 2 ]; then
  echo "usage: $0 OUTPUT_DIR DIRECTORY [DIRECTORY ...]" >&2
  exit 2
fi
out_dir=$1
shift
[ -d "$out_dir" ] || { echo "$0: $out_dir is not a directory" >&2; exit 2; }

limit=
for t in timeout gtimeout; do
  if command -v "$t" >/dev/null 2>&1; then
    limit="$t ${KELPIE_DU_TIMEOUT:-120}"
    break
  fi
done

tmp=
trap '[ -z "$tmp" ] || rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM  # through the EXIT trap: no temporary file left behind
tmp=$(mktemp "$out_dir/.kelpie_paths.XXXXXX")
{
  echo "# HELP kelpie_path_bytes Size of a directory Kelpie watches (scripts/kelpie-path-sizes.sh)."
  echo "# TYPE kelpie_path_bytes gauge"
  seen='
'
  for dir in "$@"; do
    case "$dir" in
      /*) ;;
      *) echo "$0: skipped $dir: not an absolute path" >&2; continue ;;
    esac
    case "$dir" in
      *'
'*) echo "$0: skipped a path with a newline" >&2; continue ;;
    esac
    while [ "$dir" != / ] && [ "${dir%/}" != "$dir" ]; do dir=${dir%/}; done  # /a/ is /a
    case "$seen" in
      *"
$dir
"*) continue ;;
    esac
    seen="$seen$dir
"
    [ -d "$dir" ] || { echo "$0: skipped $dir: not a directory" >&2; continue; }
    # -H: a directory given as a symlink (/var/lib/docker on a data disk) is measured.
    kb=$($limit du -skxH -- "$dir" 2>/dev/null | head -n 1 | cut -f1 || true)
    case "$kb" in
      '' | *[!0-9]*) echo "$0: skipped $dir: du gave no size" >&2; continue ;;
    esac
    # The mountpoint follows the capacity column ("45%"); a device name may hold spaces.
    # macOS pads the columns with several spaces.
    mount=$(df -P -- "$dir" 2>/dev/null | sed -n '2s/^.* [0-9][0-9]*%  *//p')
    # Label values escape backslash and double quote (Prometheus text format).
    label=$(printf '%s' "$dir" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    mlabel=$(printf '%s' "$mount" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    printf 'kelpie_path_bytes{path="%s",mountpoint="%s"} %s\n' "$label" "$mlabel" "$((kb * 1024))"
  done
} >"$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$out_dir/kelpie_paths.prom"
trap - EXIT
