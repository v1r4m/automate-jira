# automate-jira

GitHub organization 레포에 **내가 한 커밋**을 찾아서 Jira 이슈로 자동 생성합니다.

- org 관리자 권한이나 org webhook이 필요 없습니다. 내 PAT로 읽기만 합니다.
- 이 레포의 GitHub Actions가 15분마다 돌면서 최근 24시간 동안의 내 커밋을 확인합니다.
- 커밋마다 Jira 이슈 1개, 라벨 `gh-commit-<sha>`를 붙여 중복 생성을 막습니다 (state 파일 없음).
- 모든 브랜치를 봅니다. 같은 커밋이 여러 브랜치에 있으면 기본 브랜치 기준으로 1개만 만듭니다. merge 커밋은 건너뜁니다.

## 설정

### 1. 토큰 발급

| 이름 | 발급 위치 | 권한 |
|---|---|---|
| GitHub PAT | Settings → Developer settings → Personal access tokens | classic: `repo`, `read:org` / fine-grained: 대상 org, Contents·Metadata read |
| Jira API token | https://id.atlassian.com/manage-profile/security/api-tokens | 이슈를 만들 프로젝트에 대한 생성 권한 |

> org가 SSO를 쓰면 PAT를 만든 뒤 **Configure SSO → Authorize**를 해야 org 레포가 보입니다.
> fine-grained PAT는 org 관리자 승인이 필요할 수 있습니다.

### 2. 레포 Secrets / Variables 등록

Settings → Secrets and variables → Actions

**Secrets**

| 이름 | 값 |
|---|---|
| `GH_PAT` | GitHub PAT |
| `JIRA_EMAIL` | Atlassian 계정 이메일 |
| `JIRA_API_TOKEN` | Jira API token |

**Variables**

| 이름 | 필수 | 예시 / 기본값 |
|---|---|---|
| `GH_ORG` | ✅ | `fnf-es` |
| `JIRA_BASE_URL` | ✅ | `https://fnf.atlassian.net` |
| `JIRA_PROJECT_KEY` | ✅ | `ERPTEST` |
| `JIRA_ISSUE_TYPE` | | ERPTEST는 `작업` (기본값 `Task`, 없는 유형이면 실행 시 사용 가능한 목록을 출력) |
| `JIRA_LABELS` | | `auto-commit` (쉼표로 여러 개) |
| `JIRA_ASSIGNEE_ACCOUNT_ID` | | `me`(기본, 나에게 할당) / `none` / accountId |
| `GH_AUTHOR` | | 비우면 PAT 소유자 |
| `GH_REPOS` | | 비우면 org 전체. 일부만: `repo-a,repo-b` |
| `BRANCH_SCOPE` | | `all`(기본) / `default` |
| `SYNC_ENABLED` | | `true`일 때만 15분 주기 실행 |

`gh` CLI로 등록:

```sh
gh secret set GH_PAT
gh secret set JIRA_EMAIL
gh secret set JIRA_API_TOKEN
gh variable set GH_ORG --body fnf-es
gh variable set JIRA_BASE_URL --body https://fnf.atlassian.net
gh variable set JIRA_PROJECT_KEY --body ERPTEST
gh variable set JIRA_ISSUE_TYPE --body 작업
```

### 3. 테스트 후 켜기

Actions → **Commit → Jira** → Run workflow (기본값 `dry_run: true`)로 어떤 이슈가 만들어질지 로그로 확인합니다.
문제 없으면 변수 `SYNC_ENABLED`를 `true`로 설정해 15분 주기 실행을 켭니다 (`gh variable set SYNC_ENABLED --body true`).
끄려면 이 변수를 지우거나 `false`로 바꾸면 됩니다.

> 처음 켤 때는 최근 24시간치 커밋이 한꺼번에 등록됩니다. 더 과거 것까지 원하면 수동 실행 시 `lookback_hours`를 늘리세요.

## 로컬 실행

```sh
cp .env.example .env   # 값 채우기
set -a; source .env; set +a
GH_TOKEN=$(gh auth token) DRY_RUN=true python3 commit_to_jira.py
```

## 참고

- GitHub 스케줄 실행은 부하에 따라 수 분~수십 분 늦어질 수 있습니다. 24시간 lookback + 중복 방지 덕분에 늦어져도 빠지는 커밋은 없습니다.
- 레포에 60일간 활동이 없으면 GitHub이 스케줄 워크플로를 자동으로 끕니다. 꺼지면 Actions 탭에서 다시 Enable 하면 됩니다.
- 커밋 작성자 매칭은 GitHub 계정에 등록된 이메일 기준입니다. 회사 이메일로 커밋한다면 GitHub Settings → Emails에 그 주소가 등록돼 있어야 합니다.
