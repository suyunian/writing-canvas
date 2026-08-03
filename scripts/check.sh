#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
quick_validate="$repo_root/../.system/skill-creator/scripts/quick_validate.py"

cd "$repo_root"

python3 "$repo_root/scripts/canvas.py" self-check
python3 "$quick_validate" "$repo_root"
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
curl -fsS http://127.0.0.1:39173/api/health >/dev/null
printf '%s\n' 'writing-canvas checks: OK'
