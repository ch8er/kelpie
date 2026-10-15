#!/usr/bin/env bash
# Create GitHub issues (and their labels) from docs/roadmap.md.
#
# Usage:
#   scripts/create_issues.sh [--dry-run] [--repo OWNER/REPO] [--roadmap PATH]
#
# Roadmap format (see docs/roadmap.md):
#   ### <title>
#   labels: P0, week-1, area:core
#   <body markdown ...>
# "## ..." headings are section markers and are ignored. Issues whose title already
# exists in the repository (open or closed) are skipped. --dry-run never calls gh.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
roadmap="${script_dir}/../docs/roadmap.md"
repo=""
dry_run=0

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --repo)
      [[ $# -ge 2 ]] || { echo "error: --repo needs OWNER/REPO" >&2; exit 2; }
      repo="$2"; shift 2 ;;
    --repo=*) repo="${1#--repo=}"; shift ;;
    --roadmap)
      [[ $# -ge 2 ]] || { echo "error: --roadmap needs a path" >&2; exit 2; }
      roadmap="$2"; shift 2 ;;
    --roadmap=*) roadmap="${1#--roadmap=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$repo" && ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "error: --repo must look like OWNER/REPO, got: $repo" >&2
  exit 2
fi
[[ -f "$roadmap" ]] || { echo "error: roadmap not found: $roadmap" >&2; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }

# Parse the roadmap into JSON lines: {"title": ..., "labels": [...], "body": ...}.
parse_roadmap() {
  python3 -I - "$1" <<'PY'
import json
import re
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()

issues = []
current = None
in_fence = False
fence = ""
errors = []


def finish():
    global current
    if current is None:
        return
    body = "\n".join(current["body"]).strip("\n")
    if not body.strip():
        errors.append(f"line {current['line']}: issue {current['title']!r} has an empty body")
    issues.append({"title": current["title"], "labels": current["labels"], "body": body + "\n"})
    current = None


for no, raw in enumerate(lines, start=1):
    line = raw.rstrip()
    stripped = line.lstrip()
    m = re.match(r"^(`{3,}|~{3,})", stripped)
    if m:
        if not in_fence:
            in_fence, fence = True, m.group(1)
        elif re.fullmatch(re.escape(fence[0]) + "{%d,}\\s*" % len(fence), stripped):
            in_fence = False
    if not in_fence and not m:
        if re.match(r"^##\s+\S", line):
            finish()
            continue
        h3 = re.match(r"^###\s+(.+?)\s*#*\s*$", line)
        if h3:
            finish()
            current = {"title": h3.group(1).strip(), "labels": None, "body": [], "line": no}
            continue
    if current is None:
        continue
    if current["labels"] is None:
        if not line.strip():
            continue
        lm = re.match(r"^labels:\s*(.*)$", line.strip(), re.IGNORECASE)
        if not lm:
            errors.append(f"line {no}: expected 'labels:' after '### {current['title']}'")
            current["labels"] = []
            continue
        labels = [x.strip() for x in lm.group(1).split(",") if x.strip()]
        if not labels:
            errors.append(f"line {no}: issue {current['title']!r} has no labels")
        current["labels"] = labels
        continue
    current["body"].append(line)

finish()

seen = set()
for issue in issues:
    if issue["title"] in seen:
        errors.append(f"duplicate title: {issue['title']!r}")
    seen.add(issue["title"])

if in_fence:
    errors.append("unterminated code fence")
if not issues:
    errors.append("no issues found")
if errors:
    for e in errors:
        print(f"roadmap error: {e}", file=sys.stderr)
    sys.exit(1)

for issue in issues:
    print(json.dumps(issue, ensure_ascii=False))
PY
}

# Extract one field from a JSON line: field <json> title|labels|body
field() {
  python3 -I -c '
import json, sys
d = json.loads(sys.argv[1])
v = d[sys.argv[2]]
sys.stdout.write("\n".join(v) if isinstance(v, list) else v)
' "$1" "$2"
}

label_color() {
  case "$1" in
    P0) echo "b60205" ;;
    P1) echo "fbca04" ;;
    P2) echo "c5def5" ;;
    adapter) echo "5319e7" ;;
    week-*) echo "1d76db" ;;
    area:*) echo "0e8a16" ;;
    *) echo "ededed" ;;
  esac
}

label_description() {
  case "$1" in
    P0) echo "Now, this week" ;;
    P1) echo "Next step" ;;
    P2) echo "Later, on signal" ;;
    adapter) echo "Platform adapter" ;;
    week-*) echo "Planned for ${1/week-/week }" ;;
    area:*) echo "Area: ${1#area:}" ;;
    *) echo "" ;;
  esac
}

issues_file="$(mktemp)"
existing_file="$(mktemp)"
body_file="$(mktemp)"
trap 'rm -f "$issues_file" "$existing_file" "$body_file"' EXIT

parse_roadmap "$roadmap" >"$issues_file"
total="$(wc -l <"$issues_file" | tr -d ' ')"

# Unique labels across all issues, in first-seen order.
mapfile -t all_labels < <(
  while IFS= read -r json; do field "$json" labels; echo; done <"$issues_file" \
    | awk 'NF && !seen[$0]++'
)

if [[ "$dry_run" -eq 0 ]]; then
  command -v gh >/dev/null || { echo "error: gh (GitHub CLI) is required" >&2; exit 1; }
  gh auth status >/dev/null 2>&1 || { echo "error: gh is not authenticated (run: gh auth login)" >&2; exit 1; }
  if [[ -z "$repo" ]]; then
    repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
  fi
fi
target="${repo:-<current repo>}"

echo "Roadmap: $roadmap"
echo "Target:  $target$([[ "$dry_run" -eq 1 ]] && echo ' (dry run, gh not called)')"
echo "Issues:  $total, labels: ${#all_labels[@]}"
echo

for label in "${all_labels[@]}"; do
  color="$(label_color "$label")"
  desc="$(label_description "$label")"
  if [[ "$dry_run" -eq 1 ]]; then
    echo "[dry-run] label  $label (#$color)"
  else
    gh label create "$label" --repo "$repo" --color "$color" --description "$desc" --force >/dev/null
    echo "label    $label"
  fi
done
echo

if [[ "$dry_run" -eq 0 ]]; then
  gh issue list --repo "$repo" --state all --limit 5000 --json title --jq '.[].title' >"$existing_file"
fi

created=0
skipped=0
while IFS= read -r json; do
  title="$(field "$json" title)"
  labels_csv="$(field "$json" labels | paste -sd, -)"
  if grep -Fxq -- "$title" "$existing_file"; then
    echo "skip     $title (already exists)"
    skipped=$((skipped + 1))
    continue
  fi
  if [[ "$dry_run" -eq 1 ]]; then
    body_lines="$(field "$json" body | wc -l | tr -d ' ')"
    echo "[dry-run] issue  $title  [$labels_csv] ($body_lines body lines)"
  else
    field "$json" body >"$body_file"
    url="$(gh issue create --repo "$repo" --title "$title" --label "$labels_csv" --body-file "$body_file")"
    echo "created  $title -> $url"
    printf '%s\n' "$title" >>"$existing_file"
  fi
  created=$((created + 1))
done <"$issues_file"

echo
if [[ "$dry_run" -eq 1 ]]; then
  echo "Dry run: would create $created issue(s), skip $skipped."
else
  echo "Created $created issue(s), skipped $skipped."
fi
