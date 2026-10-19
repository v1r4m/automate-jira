#!/bin/bash
# .env 를 읽어 commit_to_jira.py 를 한 번 실행한다. launchd 가 이 스크립트를 주기적으로 호출한다.
set -euo pipefail
cd "$(dirname "$0")"

# .env 를 읽되, 명령줄에서 이미 준 값(예: DRY_RUN=true ./run.sh)이 우선하게 한다.
overrides="$(export -p)"
set -a
source ./.env
set +a
eval "$overrides"

# GH_TOKEN 이 비어 있으면 gh CLI 로그인 토큰을 쓴다.
if [ -z "${GH_TOKEN:-}" ] && command -v gh >/dev/null; then
  GH_TOKEN="$(gh auth token)"
  export GH_TOKEN
fi

exec python3 commit_to_jira.py "$@"
