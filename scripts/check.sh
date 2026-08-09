#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

cd "$repo_root"

python3 "$repo_root/scripts/canvas.py" self-check
python3 - <<'PY'
try:
    import markdown_it
except ModuleNotFoundError as error:
    raise SystemExit("markdown-it-py is required; install requirements.txt") from error

print("dependency check: markdown-it-py OK")

try:
    import reportlab  # noqa: F401
except ModuleNotFoundError:
    print("optional dependency check: reportlab not installed (PDF export unavailable)")
else:
    print("optional dependency check: reportlab OK")
PY
python3 -m py_compile "$repo_root/scripts/canvas.py"

node - "$repo_root/assets/index.html" <<'NODE'
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)];
if (!scripts.length) throw new Error('no inline script found');
for (const match of scripts) new vm.Script(match[1]);
console.log('javascript syntax: OK');
NODE

git diff --check
git diff --cached --check
printf '%s\n' 'writing-canvas checks: OK'
