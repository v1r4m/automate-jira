# automate-jira

GitHub organization 레포에 **내가 한 커밋**을 Jira 이슈로 자동 생성하고, 커밋이 **머지되면 이슈를 완료**로 바꿉니다.
macOS launchd로 내 컴퓨터에서 15분마다 실행됩니다.

- org 관리자 권한이나 webhook이 필요 없습니다. 내 토큰으로 읽기만 합니다.
- 커밋 1개당 Jira 이슈 1개를 만들고, 라벨 `gh-commit-<sha>`로 중복 생성을 막습니다 (state 파일 없음).
- 모든 브랜치를 봅니다. 같은 커밋이 여러 브랜치에 있으면 1개만 만들고, merge 커밋은 건너뜁니다.

## 머지 → 완료 규칙

아직 완료되지 않은 자동 생성 이슈마다, 본문의 커밋 링크로 다음을 확인합니다.

1. 커밋을 포함한 **PR이 머지됨** (squash·rebase 머지 포함)
2. 또는 커밋이 레포의 **기본 브랜치**에 들어가 있음 (PR 없이 직접 push한 경우)

둘 중 하나라도 해당하면 머지 근거를 코멘트로 남기고, 상태 분류가 '완료'인 전환으로 옮깁니다.

> 기본 브랜치는 레포마다 다릅니다. 예를 들어 `sms`는 `prd`라서, master에 push한 커밋은 master가 prd로 머지될 때 완료됩니다.
> 기본 브랜치에 바로 커밋하면 이슈가 만들어진 다음 실행에서 곧바로 완료됩니다.

## 설치

```sh
cp .env.example .env              # 값 채우기 (아래 표)
DRY_RUN=true ./run.sh             # 무엇을 할지 미리 보기
./scripts/install-launchd.sh      # 15분마다 자동 실행 등록 (간격 변경: ./scripts/install-launchd.sh 600)
tail -f ~/Library/Logs/automate-jira.log
```

macOS는 launchd 작업이 `~/Documents` 같은 보호된 폴더를 읽지 못하게 막습니다.
그래서 설치 스크립트는 `commit_to_jira.py`, `run.sh`, `.env`를 `~/Library/Application Support/automate-jira/`로 복사해서 거기서 실행합니다.
**코드나 `.env`를 고친 뒤에는 `./scripts/install-launchd.sh`를 다시 실행**해야 반영됩니다.

| 작업 | 명령 |
|---|---|
| 지금 한 번 실행 | `./run.sh` (미리 보기: `DRY_RUN=true ./run.sh`) |
| 자동 실행 즉시 트리거 | `launchctl kickstart gui/$(id -u)/com.automate-jira.sync` |
| 상태 확인 | `launchctl print gui/$(id -u)/com.automate-jira.sync \| grep -E "state\|last exit"` |
| 끄기 | `./scripts/uninstall-launchd.sh` |

명령줄에서 준 환경변수가 `.env`보다 우선합니다. 예: `LOOKBACK_HOURS=336 DRY_RUN=true ./run.sh`

## 설정 (`.env`)

| 이름 | 필수 | 설명 / 기본값 |
|---|---|---|
| `GH_TOKEN` | | GitHub PAT (classic: `repo`, `read:org`). 비우면 `gh auth token` 사용 |
| `GH_ORG` | ✅ | `fnf-es` |
| `GH_AUTHOR` | | 비우면 토큰 소유자 |
| `GH_REPOS` | | 비우면 org 전체. 일부만: `repo-a,repo-b` |
| `BRANCH_SCOPE` | | `all`(기본) / `default` |
| `LOOKBACK_HOURS` | | 몇 시간 전 커밋까지 볼지. 컴퓨터가 꺼져 있던 시간까지 커버하도록 `72` 권장 |
| `SKIP_MERGE_COMMITS` | | `true` |
| `JIRA_BASE_URL` | ✅ | `https://fnf.atlassian.net` |
| `JIRA_EMAIL` | ✅ | Atlassian 계정 이메일 |
| `JIRA_API_TOKEN` | ✅ | https://id.atlassian.com/manage-profile/security/api-tokens |
| `JIRA_PROJECT_KEY` | ✅ | `ERPTEST` |
| `JIRA_ISSUE_TYPE` | | ERPTEST는 `작업` (기본값 `Task`. 없는 유형이면 사용 가능한 목록을 출력하고 멈춤) |
| `JIRA_LABELS` | | `auto-commit` (쉼표로 여러 개) |
| `JIRA_ASSIGNEE_ACCOUNT_ID` | | `me`(기본) / `none` / accountId |
| `AUTO_DONE_ON_MERGE` | | `true`. 머지되면 완료 처리 |
| `MERGE_BASE_BRANCHES` | | 비우면 어느 브랜치로든 PR이 머지되면 완료. 예: `prd,main`이면 그 브랜치로 머지된 경우만 |
| `JIRA_DONE_TRANSITION` | | 쓸 전환 이름 (예: `완료`). 비우면 상태 분류가 '완료'인 첫 전환 |
| `DRY_RUN` | | `true`면 Jira를 바꾸지 않고 출력만 함 |

> org가 SSO를 쓰면 PAT를 만든 뒤 **Configure SSO → Authorize**를 해야 org 레포가 보입니다.
> 커밋 작성자는 GitHub 계정에 등록된 이메일 기준으로 매칭합니다. 회사 이메일로 커밋한다면 GitHub Settings → Emails에 그 주소가 등록돼 있어야 합니다.
