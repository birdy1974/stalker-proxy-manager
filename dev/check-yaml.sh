#!/usr/bin/env bash
# ============================================================================
# Parse-check the workflow YAML, and keep the template honest.
#
#   bash dev/check-yaml.sh                 # defaults: workflows + dev copies
#   bash dev/check-yaml.sh file.yml ...    # check your own files
#
# Why this script exists. Two smoke-job regressions were caused by the test's
# *harness*, not the app, and the second one by a copy-paste template:
# dev/docker-publish.yml.example is what you (or a bot without the
# `workflows` permission) install over .github/workflows/docker-publish.yml.
# It had silently drifted from the real workflow AND contained invalid YAML:
#
#     - name: Image metadata (tags: latest, sha, semver releases)   # INVALID
#     - name: "Image metadata (tags: latest, sha, semver releases)" # ok
#
# A YAML *plain scalar* may not contain ": " (colon + space). GitHub's parser
# then says "mapping values are not allowed here" and refuses the WHOLE
# workflow file - no job in it runs at all, so a broken one-line edit can
# disable build, push and smoke simultaneously. Rule: quote any scalar that
# contains a colon+space.
#
# So this script does two things: (1) parse every workflow/template it finds,
# (2) verify dev/docker-publish.yml.example is byte-identical to
# .github/workflows/docker-publish.yml (the template must be copyable as-is).
#
# Parser choice: PyYAML (python3 on a runner, or .venv / $SPM_PYTHON locally) ->
# ruby -> node/js-yaml. If no parser is available it says so and exits 0: a
# missing parser must not look like a broken workflow.
# ============================================================================
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
have_parser=""
# PyYAML from a venv in the repo counts too (and SPM_PYTHON overrides everything),
# so the check works locally as well as on a runner where it is preinstalled.
py=""
for cand in "${SPM_PYTHON:-}" python3 "$root/.venv/bin/python"; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import yaml' >/dev/null 2>&1; then py="$cand"; have_parser="PyYAML ($cand)"; break; fi
done
if [ -z "$py" ] && command -v ruby >/dev/null 2>&1; then
    have_parser="ruby"
elif [ -z "$py" ] && command -v node >/dev/null 2>&1 && node -e 'require("js-yaml")' 2>/dev/null; then
    have_parser="node/js-yaml"
fi

parse_one() {  # $1 = file; prints ONE readable line on failure, like GitHub does
    case "$have_parser" in
        PyYAML*) "$py" -c '
import sys, yaml
try:
    list(yaml.safe_load_all(open(sys.argv[1])))
except yaml.YAMLError as e:
    parts = [p.strip() for p in str(e).strip().splitlines() if p.strip()]
    print(" | ".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else "invalid YAML"))
    sys.exit(1)
' "$1" ;;
        ruby)    ruby -ryaml -e 'begin; YAML.load_file(ARGV[0]); rescue => e; puts e.message.lines.first.to_s.strip; exit 1; end' "$1" ;;
        node*)   node -e 'const fs=require("fs"),y=require("js-yaml");try{y.load(fs.readFileSync(process.argv[1],"utf8"))}catch(e){console.log(e.message.split("\n")[0]);process.exit(1)}' "$1" ;;
    esac
}

files=("$@")
if [ "${#files[@]}" -eq 0 ]; then
    shopt -s nullglob
    files=("$root"/.github/workflows/*.yml "$root"/.github/workflows/*.yaml \
           "$root"/docker-compose.yml "$root"/dev/*.example)
fi

echo "== yaml: parse check (${have_parser:-no parser found})"
for f in "${files[@]}"; do
    [ -e "$f" ] || continue
    if [ -z "$have_parser" ]; then
        echo "   SKIP ${f#"$root"/} (no YAML parser: apt install python3-yaml, or pip install pyyaml in .venv)"
        continue
    fi
    if err="$(parse_one "$f" 2>&1)"; then
        echo "   OK   ${f#"$root"/}"
    else
        echo "   FAIL ${f#"$root"/}"
        printf '%s\n' "$err" | sed 's/^/          /'
        echo "          hint: a name/value containing ': ' must be quoted" >&2
        fail=1
    fi
done

# --- template drift: the example must be a faithful copy of the workflow -----
wf="$root/.github/workflows/docker-publish.yml"
ex="$root/dev/docker-publish.yml.example"
if [ -f "$wf" ] && [ -f "$ex" ]; then
    echo "== yaml: template in sync"
    if cmp -s "$wf" "$ex"; then
        echo "   OK   dev/docker-publish.yml.example == .github/workflows/docker-publish.yml"
    else
        echo "   FAIL dev/docker-publish.yml.example differs from the real workflow;" >&2
        echo "        'cp dev/docker-publish.yml.example ...' would install a stale file." >&2
        diff -u "$wf" "$ex" | sed 's/^/          /' >&2 || true
        echo "        fix: cp .github/workflows/docker-publish.yml dev/docker-publish.yml.example" >&2
        fail=1
    fi
fi

if [ "$fail" -eq 0 ]; then
    echo
    echo "yaml OK"
fi
exit "$fail"
