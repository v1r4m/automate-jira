#!/bin/bash
# macOS launchd 에 등록해서 run.sh 를 주기적으로(기본 15분) 실행한다.
#   ./scripts/install-launchd.sh          설치/재설치
#   ./scripts/install-launchd.sh 600      10분 간격으로 설치
#
# launchd 작업은 ~/Documents 등 보호된 폴더를 읽지 못하므로(macOS TCC),
# 실행에 필요한 파일을 APP_DIR 로 복사해서 거기서 돌린다.
# 코드나 .env 를 고친 뒤에는 이 스크립트를 다시 실행해야 반영된다.
set -euo pipefail

LABEL="com.automate-jira.sync"
INTERVAL="${1:-900}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$HOME/Library/Application Support/automate-jira"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/automate-jira.log"
# launchd 는 PATH 가 최소한이라 python3 / gh 위치를 지금 셸 기준으로 넣어 준다.
PATH_VALUE="$(dirname "$(command -v python3)"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

[ -f "$ROOT/.env" ] || { echo ".env 가 없습니다. .env.example 을 복사해서 채우세요." >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" "$APP_DIR"
cp "$ROOT/commit_to_jira.py" "$ROOT/run.sh" "$ROOT/.env" "$APP_DIR/"
chmod 600 "$APP_DIR/.env"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$APP_DIR/run.sh</string></array>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH_VALUE</string></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "설치됨: $PLIST (${INTERVAL}초 간격)"
echo "실행 위치: $APP_DIR"
echo "로그:   tail -f $LOG"
