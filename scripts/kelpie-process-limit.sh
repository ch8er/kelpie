#!/bin/sh
# Writes how many processes a user runs and how many it may run, as node_exporter
# textfile-collector metrics, for Kelpie's rule 7 (incident 19: a farm Mac hit
# kern.maxprocperuid twice, and every new process of the agents failed):
#
#   kelpie_user_processes{user="agent"} 1830
#   kelpie_user_processes_max{user="agent"} 2666
#
# Usage: kelpie-process-limit.sh OUTPUT_DIR [USER]
#
# OUTPUT_DIR is node_exporter's --collector.textfile.directory; USER defaults to the user
# running the script. The limit is the lower of kern.maxprocperuid (macOS: node_exporter
# has no processes collector there) and the process ulimit of the user running the script,
# so run it as USER. It counts processes, as macOS's limit does; on Linux, where the limit
# counts threads, node_exporter's --collector.processes is the source to use. The file is
# written to a temporary name and renamed, so node_exporter never reads half of it. Run it
# every minute (deploy/textfile/com.kelpie.process-limit.plist).
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 OUTPUT_DIR [USER]" >&2
  exit 2
fi
out_dir=$1
user=${2:-$(id -un)}
[ -d "$out_dir" ] || { echo "$0: $out_dir is not a directory" >&2; exit 2; }
case "$user" in
  '' | -* | *[!A-Za-z0-9._-]*) echo "$0: $user is not a user name" >&2; exit 2 ;;
esac
id -u -- "$user" >/dev/null 2>&1 || { echo "$0: no user $user" >&2; exit 2; }

# Every process of the user, with a terminal or not (POSIX ps, by numeric uid: no
# truncated names).
uid=$(id -u -- "$user")
count=$(ps -A -o uid= | awk -v u="$uid" '$1 == u' | wc -l | tr -d ' ')
# The lower of kern.maxprocperuid (macOS) and the process ulimit (-u in bash and zsh, -p
# in dash) of the user running this, when they are numbers.
per_uid=$(sysctl -n kern.maxprocperuid 2>/dev/null) || per_uid=
ulim=$( (ulimit -u) 2>/dev/null) || ulim=$( (ulimit -p) 2>/dev/null) || ulim=
max=
for n in "$per_uid" "$ulim"; do
  case "$n" in
    '' | *[!0-9]*) ;;
    *) if [ -z "$max" ] || [ "$n" -lt "$max" ]; then max=$n; fi ;;
  esac
done

tmp=$(mktemp "$out_dir/.kelpie_processes.XXXXXX")
trap 'rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM  # through the EXIT trap: no temporary file left behind
{
  echo "# HELP kelpie_user_processes Processes of a user (scripts/kelpie-process-limit.sh)."
  echo "# TYPE kelpie_user_processes gauge"
  printf 'kelpie_user_processes{user="%s"} %s\n' "$user" "$count"
  case "$max" in
    '') echo "$0: no numeric process limit for $user" >&2 ;;
    *)
      echo "# HELP kelpie_user_processes_max Processes a user may run (the lower of kern.maxprocperuid and the process ulimit)."
      echo "# TYPE kelpie_user_processes_max gauge"
      printf 'kelpie_user_processes_max{user="%s"} %s\n' "$user" "$max"
      ;;
  esac
} >"$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$out_dir/kelpie_processes.prom"
trap - EXIT
