#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

if [ "$#" -ne 1 ]; then
  printf '%s\n' '用法：./scripts/release.sh patch|minor|major' >&2
  exit 2
fi

bump=$1
case "$bump" in
  patch|minor|major) ;;
  *)
    printf '%s\n' '版本增量只能是 patch、minor 或 major。' >&2
    exit 2
    ;;
esac

if [ "$(git branch --show-current)" != "main" ]; then
  printf '%s\n' '发布必须在 main 分支执行。' >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  printf '%s\n' '发布前要求工作区干净，请先提交或处理现有改动。' >&2
  exit 1
fi

remote_counts=$(git rev-list --left-right --count origin/main...HEAD)
behind=$(printf '%s' "$remote_counts" | awk '{ print $1 }')
if [ "$behind" -ne 0 ]; then
  printf '%s\n' '本地 main 落后于 origin/main，请先同步远端。' >&2
  exit 1
fi

latest_tag=$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-version:refname | sed -n '1p')
latest_tag=${latest_tag:-v0.0.0}
version=${latest_tag#v}
old_ifs=$IFS
IFS=.
set -- $version
IFS=$old_ifs
major=$1
minor=$2
patch=$3

case "$major:$minor:$patch" in
  *[!0-9:]*|'' )
    printf '%s\n' "无法解析当前版本标签：$latest_tag" >&2
    exit 1
    ;;
esac

case "$bump" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
esac

next_tag="v${major}.${minor}.${patch}"
if git rev-parse --verify --quiet "refs/tags/$next_tag" >/dev/null; then
  printf '%s\n' "标签已存在：$next_tag" >&2
  exit 1
fi
if git ls-remote --exit-code --refs origin "refs/tags/$next_tag" >/dev/null 2>&1; then
  printf '%s\n' "远端标签已存在：$next_tag" >&2
  exit 1
fi

./scripts/check.sh
git tag -a "$next_tag" -m "Release $next_tag"
git push origin main "$next_tag"
printf '%s\n' "release: $next_tag pushed"
