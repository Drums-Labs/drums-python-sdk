#!/usr/bin/env bash
# Dependency audit (A-40): install the package into a fresh venv the way CI does, freeze what got installed, and run
# pip-audit on exactly that set. Fails on any known vulnerability that is not in the allowlist (.pip-audit-ignore).
#
#   .github/scripts/pip-audit.sh PIP_INSTALL_ARGS...      e.g. .github/scripts/pip-audit.sh '.[test]'
#
# Allowlist format, one advisory per line (blank lines and lines starting with # are skipped):
#   <advisory id>  <review-by YYYY-MM-DD>  <reason>
# An entry past its review date fails the audit, so an accepted advisory is looked at again rather than forgotten.
# Env: PIP_AUDIT_IGNORE_FILE (default .pip-audit-ignore), PIP_AUDIT_VERSION (default 2.10.1).
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: $0 PIP_INSTALL_ARGS..." >&2; exit 2; }
ignore_file="${PIP_AUDIT_IGNORE_FILE:-.pip-audit-ignore}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
py="${PYTHON:-python3}"

# The allowlist: ids, with the review date enforced.
ignore_args=()
if [ -f "$ignore_file" ]; then
  today="$(date -u +%F)"; expired=0
  while read -r id review reason; do
    case "$id" in ''|'#'*) continue;; esac
    [[ "$review" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "$ignore_file: $id has no review-by date (YYYY-MM-DD)" >&2; exit 2; }
    [ -n "$reason" ] || { echo "$ignore_file: $id has no reason" >&2; exit 2; }
    if [[ "$review" < "$today" ]]; then echo "::error::$ignore_file: $id was due for review on $review ($reason)"; expired=1; fi
    echo "allowlisted: $id (review by $review): $reason"
    ignore_args+=(--ignore-vuln "$id")
  done < "$ignore_file"
  [ "$expired" = 0 ] || exit 1
fi

# The audited set: what `pip install ARGS` puts into a clean venv (pip itself included), minus this project.
"$py" -m venv "$tmp/target"
"$tmp/target/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$tmp/target/bin/python" -m pip install --quiet --disable-pip-version-check "$@"
"$tmp/target/bin/python" -m pip freeze --all --exclude-editable | grep -v ' @ file://' > "$tmp/installed.txt" || true
echo "audited set ($(wc -l < "$tmp/installed.txt" | tr -d " ") packages, from: pip install $*):"
sed 's/^/  /' "$tmp/installed.txt"

"$py" -m venv "$tmp/tool"
"$tmp/tool/bin/python" -m pip install --quiet --disable-pip-version-check "pip-audit==${PIP_AUDIT_VERSION:-2.10.1}"
"$tmp/tool/bin/pip-audit" --requirement "$tmp/installed.txt" --no-deps --disable-pip --progress-spinner off \
  --desc on ${ignore_args[@]+"${ignore_args[@]}"}
