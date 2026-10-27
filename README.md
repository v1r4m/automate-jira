# automate-jira

GitHub organization 레포에 **내가 한 커밋**을 Jira 이슈로 자동 생성하고, 커밋이 **머지되면 이슈를 완료**로 바꿉니다.
macOS launchd로 내 컴퓨터에서 15분마다 실행됩니다.

- org 관리자 권한이나 webhook이 필요 없습니다. 내 토큰으로 읽기만 합니다.
- 커밋 1개당 Jira 이슈 1개를 만들고, 라벨 `gh-commit-<sha>`로 중복 생성을 막습니다 (state 파일 없음).
- 모든 브랜치를 봅니다. 같은 커밋이 여러 브랜치에 있으면 1개만 만들고, merge 커밋은 건너뜁니다.

## LLM 티켓 문구

`AX_LINK_API_KEY`가 있으면 사내 AX Link 게이트웨이(기본 `anthropic/claude-haiku-5.5`)로 커밋 내용을 정리해 티켓을 씁니다.

- **LLM에 보내는 것:** 커밋 메시지, 변경 파일 목록, 줄 수. `SEND_DIFF=true`면 diff 일부(최대 6000자)도 보냅니다.
- **제목:** `[레포] 업무 언어로 쓴 요약` 형식입니다.
- **본문:** 배경 / 작업 내용 / 기대 효과 / 확인 포인트 아래에 커밋 정보(링크, 브랜치, 변경 규모, 원본 메시지)가 붙습니다.
- LLM 호출이 실패하거나 `USE_LLM=false`면 커밋 메시지를 그대로 씁니다. 이슈 생성 자체는 막지 않습니다.
- 문구를 미리 보려면: `DRY_RUN=true LOOKBACK_HOURS=336 GH_REPOS=sms ./run.sh`

## 레포별 컴포넌트와 에픽

특정 레포의 커밋에는 ITSM 컴포넌트를 붙이고, 개발 프로젝트에 에픽을 따로 만들 수 있습니다.

```sh
REPO_COMPONENTS="sms=SMS (매장관리시스템)"   # 따옴표 필수 (괄호·공백 때문)
REPO_EPIC_PROJECTS=sms=SMSDEV
```

- 형식은 `레포=값`이고, 여러 개는 `;`로 구분합니다. 컴포넌트 이름에 쉼표·콜론이 있어서 `;`를 씁니다.
- **에픽:** ITSM 이슈와 같은 제목·본문·라벨로 SMSDEV에 에픽을 만들고, ITSM 이슈와 `Relates`로 링크합니다.
  ITSM 이슈에는 `상위 항목` 필드가 없어서, 에픽을 상위로 지정하지 않고 링크로 연결합니다.
- **작업:** 에픽 아래에 `작업`을 만듭니다. LLM이 커밋을 화면·API·DB 등 성격별로 1~6개로 나눕니다.
  LLM이 없거나 실패하면 커밋 제목 하나로 작업 1개를 만듭니다.
- 에픽과 작업도 같은 커밋 링크와 라벨을 가지므로, 머지되면 ITSM 이슈와 함께 완료됩니다.
- 에픽 생성만 실패했다면 다음 실행에서 에픽만 다시 만듭니다 (`LOOKBACK_HOURS` 안의 커밋만 해당).
- 컴포넌트는 새로 만드는 ITSM 이슈에만 붙습니다. 이미 있는 이슈는 바꾸지 않습니다.

## 머지 → 완료 규칙

아직 완료되지 않은 자동 생성 이슈마다, 본문의 커밋 링크로 다음을 확인합니다.

1. 커밋을 포함한 **PR이 머지됨** (squash·rebase 머지 포함)
2. 또는 커밋이 레포의 **기본 브랜치**에 들어가 있음 (PR 없이 직접 push한 경우)

둘 중 하나라도 해당하면 머지 근거를 코멘트로 남기고 완료 상태로 옮깁니다.

- 상태 분류가 '완료'인 전환을 쓰되, **취소·거절·반려** 같은 전환은 제외합니다. ITSM에는 '할 일 → 취소'가 완료 분류로 있습니다.
- ITSM처럼 '할 일'에서 '완료'로 바로 갈 수 없으면 **'진행 중'을 거쳐서** 갑니다 (할 일 → 진행 중 → 완료).
- 특정 전환을 쓰고 싶으면 `JIRA_DONE_TRANSITION`에 이름을 지정하세요.
- 방금 만든 이슈는 Jira 검색 인덱스에 늦게 반영될 수 있습니다. 그런 경우 다음 실행에서 완료 처리됩니다.

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
| `JIRA_PROJECT_KEY` | ✅ | `ITSM` |
| `JIRA_ISSUE_TYPE` | | ITSM·ERPTEST 모두 `작업` (기본값 `Task`. 없는 유형이면 사용 가능한 목록을 출력하고 멈춤) |
| `JIRA_LABELS` | | `auto-commit` (쉼표로 여러 개) |
| `JIRA_ASSIGNEE_ACCOUNT_ID` | | `me`(기본) / `none` / accountId |
| `REPO_COMPONENTS` | | 레포별 ITSM 컴포넌트. 예: `"sms=SMS (매장관리시스템)"` |
| `REPO_EPIC_PROJECTS` | | 레포별로 에픽을 만들 프로젝트. 예: `sms=SMSDEV` |
| `EPIC_ISSUE_TYPE` | | `에픽` |
| `EPIC_TASK_TYPE` | | 에픽 아래 만들 이슈 유형. `작업` |
| `EPIC_LINK_TYPE` | | ITSM 이슈와 에픽을 잇는 링크 유형. `Relates` |
| `AUTO_DONE_ON_MERGE` | | `true`. 머지되면 완료 처리 |
| `MERGE_BASE_BRANCHES` | | 비우면 어느 브랜치로든 PR이 머지되면 완료. 예: `prd,main`이면 그 브랜치로 머지된 경우만 |
| `JIRA_DONE_TRANSITION` | | 쓸 전환 이름 (예: `완료`). 비우면 취소·거절 등을 뺀 '완료' 분류의 첫 전환 |
| `AX_LINK_API_KEY` | | 사내 AX Link 키(`svc_...`). 비우면 LLM 없이 커밋 메시지 사용 |
| `AX_LINK_MODEL` | | `anthropic/claude-haiku-5.5` |
| `USE_LLM` | | `true` |
| `SEND_DIFF` | | `false`. `true`면 diff 일부도 LLM에 보냄 |
| `DRY_RUN` | | `true`면 Jira를 바꾸지 않고 출력만 함 |

> org가 SSO를 쓰면 PAT를 만든 뒤 **Configure SSO → Authorize**를 해야 org 레포가 보입니다.
> 커밋 작성자는 GitHub 계정에 등록된 이메일 기준으로 매칭합니다. 회사 이메일로 커밋한다면 GitHub Settings → Emails에 그 주소가 등록돼 있어야 합니다.
