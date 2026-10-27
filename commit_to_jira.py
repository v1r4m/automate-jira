#!/usr/bin/env python3
"""GitHub 조직(org)에서 '내' 커밋을 찾아 Jira 이슈로 자동 생성한다.

동작 방식
  1. 최근 LOOKBACK_HOURS 동안 push된 org 레포를 훑는다.
  2. 각 레포의 브랜치에서 GH_AUTHOR(기본: 토큰 소유자)의 커밋만 모은다.
  3. 이미 Jira에 만들어진 커밋(라벨 gh-commit-<sha>)은 건너뛴다.
  4. 나머지를 오래된 순서대로 Jira 이슈로 만든다. REPO_COMPONENTS 의 레포면 컴포넌트를 붙이고,
     REPO_EPIC_PROJECTS 의 레포면 그 프로젝트에 에픽을 만들어 이슈와 링크한다.
  5. 아직 열린 자동 생성 이슈(에픽 포함) 중, 커밋이 머지된 것은 완료로 전환한다.

중복 방지 상태를 Jira 라벨에 저장하므로 별도 DB/state 파일이 필요 없고,
같은 기간을 여러 번 돌려도 이슈가 중복 생성되지 않는다.

표준 라이브러리만 사용한다 (pip install 불필요).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GITHUB_API = "https://api.github.com"
LABEL_PREFIX = "gh-commit-"


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, "").strip() or default
    if required and not value:
        sys.exit(f"환경변수 {name} 가 필요합니다.")
    return value or ""


def env_map(name: str) -> dict[str, str]:
    """'sms=SMSDEV;wms=WMSDEV' 형식을 {레포: 값} 으로 읽는다. 컴포넌트 이름에 쉼표·콜론이 있어 ; 로 구분한다."""
    pairs = (item.split("=", 1) for item in env(name).split(";") if "=" in item)
    return {repo.strip(): value.strip() for repo, value in pairs if repo.strip() and value.strip()}


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


# --------------------------------------------------------------------------- HTTP


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} {url}\n{body[:500]}")
        self.status = status


def request(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            payload = json.loads(raw) if raw else None
            return payload, resp.headers
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, url, e.read().decode(errors="replace")) from None


def next_link(headers) -> str | None:
    link = headers.get("Link") or ""
    match = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return match.group(1) if match else None


# ------------------------------------------------------------------------- GitHub


class GitHub:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "automate-jira",
        }

    def get(self, path: str, params: dict | None = None):
        url = path if path.startswith("http") else GITHUB_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return request("GET", url, self.headers)

    def paginate(self, path: str, params: dict | None = None):
        payload, headers = self.get(path, {"per_page": 100, **(params or {})})
        yield from payload
        url = next_link(headers)
        while url:
            payload, headers = self.get(url)
            yield from payload
            url = next_link(headers)

    def login(self) -> str:
        user, _ = self.get("/user")
        return user["login"]

    def recently_pushed_repos(self, org: str, since: datetime):
        # sort=pushed 내림차순이므로 since 이전 레포가 나오면 중단한다.
        for repo in self.paginate(f"/orgs/{org}/repos", {"type": "all", "sort": "pushed", "direction": "desc"}):
            pushed_at = repo.get("pushed_at")
            if not pushed_at or parse_ts(pushed_at) < since:
                break
            if repo.get("archived"):
                continue
            yield repo

    def graphql(self, query: str, variables: dict) -> dict:
        payload, _ = request("POST", GITHUB_API + "/graphql", self.headers, {"query": query, "variables": variables})
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL 오류: {payload['errors']}")
        return payload["data"]

    def user_node_id(self, login: str) -> str:
        return self.graphql("query($login: String!) { user(login: $login) { id } }", {"login": login})["user"]["id"]

    def branch_commits(self, owner: str, name: str, author_id: str, since: datetime):
        """최근 갱신된 브랜치들에서 author 의 커밋을 (branch, commit) 으로 돌려준다.

        TAG_COMMIT_DATE 정렬은 브랜치에서 날짜순을 보장하지 않으므로(예: sms 에서 prd 가 더 최근
        master 보다 먼저 온다) 중간에 멈추지 않고 모든 브랜치를 보며 since 이전 브랜치만 건너뛴다.
        """
        cursor = None
        while True:
            data = self.graphql(BRANCH_COMMITS_QUERY, {
                "owner": owner, "name": name, "cursor": cursor, "since": iso(since), "author": author_id,
            })
            refs = data["repository"]["refs"]
            for ref in refs["nodes"]:
                target = ref["target"]
                if not target.get("committedDate"):  # 태그 등 커밋이 아닌 대상
                    continue
                if parse_ts(target["committedDate"]) < since:
                    continue
                for commit in target["history"]["nodes"]:
                    yield ref["name"], commit
            if not refs["pageInfo"]["hasNextPage"]:
                return
            cursor = refs["pageInfo"]["endCursor"]

    def commit_detail(self, full_name: str, sha: str) -> dict:
        data, _ = self.get(f"/repos/{full_name}/commits/{sha}")
        return {
            "additions": data.get("stats", {}).get("additions", 0),
            "deletions": data.get("stats", {}).get("deletions", 0),
            "files": [
                {
                    "name": f.get("filename", "?"),
                    "status": f.get("status", "?"),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch": f.get("patch", ""),
                }
                for f in data.get("files") or []
            ],
        }

    def merge_reason(self, owner: str, name: str, sha: str, base_branches: set[str]) -> str | None:
        """커밋이 머지됐으면 그 근거(사람이 읽을 문장)를, 아니면 None 을 돌려준다.

        - 커밋을 포함한 PR 이 머지됨 (squash/rebase 머지도 PR 로 잡힌다)
        - 또는 커밋이 기본 브랜치에 이미 들어가 있음 (PR 없이 직접 push 한 경우)
        base_branches 가 비어 있지 않으면 그 브랜치로 머지된 경우만 인정한다.
        """
        data = self.graphql(MERGE_QUERY, {"owner": owner, "name": name, "oid": sha})
        repo = data["repository"]
        commit = repo and repo["object"]
        if not commit:  # 레포가 없거나 force-push 등으로 커밋이 사라짐
            return None
        for pr in commit["associatedPullRequests"]["nodes"]:
            if pr["merged"] and (not base_branches or pr["baseRefName"] in base_branches):
                return (f"PR #{pr['number']} \"{pr['title']}\" 이(가) {pr['baseRefName']} 브랜치에 머지되어 "
                        f"작업을 완료 처리합니다. {pr['url']}")
        default = repo["defaultBranchRef"]["name"]
        if base_branches and default not in base_branches:
            return None
        compare, _ = self.get(f"/repos/{owner}/{name}/compare/{urllib.parse.quote(default)}...{sha}")
        if compare["status"] in ("behind", "identical"):
            return f"커밋이 {default} 브랜치에 반영되어 작업을 완료 처리합니다."
        return None


MERGE_QUERY = """
query($owner: String!, $name: String!, $oid: GitObjectID!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    object(oid: $oid) {
      ... on Commit {
        associatedPullRequests(first: 10) {
          nodes { number title url merged baseRefName }
        }
      }
    }
  }
}
"""


BRANCH_COMMITS_QUERY = """
query($owner: String!, $name: String!, $cursor: String, $since: GitTimestamp!, $author: ID!) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 25, after: $cursor,
         orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target {
          ... on Commit {
            committedDate
            history(first: 100, since: $since, author: {id: $author}) {
              nodes {
                oid
                url
                message
                parents { totalCount }
                author { name date }
              }
            }
          }
        }
      }
    }
  }
}
"""


# --------------------------------------------------------------------------- Jira


class Jira:
    def __init__(self, base_url: str, email: str, token: str):
        self.base_url = base_url.rstrip("/")
        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "User-Agent": "automate-jira",
        }

    def call(self, method: str, path: str, body: dict | None = None):
        payload, _ = request(method, self.base_url + path, self.headers, body)
        return payload

    def my_account_id(self) -> str:
        return self.call("GET", "/rest/api/3/myself")["accountId"]

    def issue_type_names(self, project: str) -> list[str]:
        result = self.call("GET", f"/rest/api/3/issue/createmeta/{urllib.parse.quote(project)}/issuetypes")
        return [t["name"] for t in result.get("issueTypes", result.get("values", []))]

    def keys_by_label(self, project: str, labels: list[str]) -> dict[str, str]:
        """labels 중 이미 project 의 이슈에 붙어 있는 것을 {라벨: 이슈 키} 로 돌려준다."""
        found: dict[str, str] = {}
        for i in range(0, len(labels), 50):
            chunk = labels[i : i + 50]
            quoted = ", ".join(f'"{label}"' for label in chunk)
            body = {
                "jql": f'project = "{project}" AND labels in ({quoted})',
                "fields": ["labels"],
                "maxResults": 100,
            }
            while True:
                result = self.call("POST", "/rest/api/3/search/jql", body)
                for issue in result.get("issues", []):
                    for label in issue["fields"].get("labels") or []:
                        found.setdefault(label, issue["key"])
                token = result.get("nextPageToken")
                if not token:
                    break
                body["nextPageToken"] = token
        return {label: key for label, key in found.items() if label in labels}

    def create_issue(self, fields: dict) -> str:
        return self.call("POST", "/rest/api/3/issue", {"fields": fields})["key"]

    def link(self, a: str, b: str, link_type: str) -> None:
        self.call("POST", "/rest/api/3/issueLink", {
            "type": {"name": link_type}, "outwardIssue": {"key": a}, "inwardIssue": {"key": b},
        })

    def open_commit_issues(self, projects: list[str], labels: list[str]):
        """아직 완료되지 않은 자동 생성 이슈를 (key, owner, repo, sha) 로 돌려준다."""
        quoted = ", ".join(f'"{label}"' for label in labels)
        in_projects = ", ".join(f'"{p}"' for p in projects)
        body = {
            "jql": f"project in ({in_projects}) AND labels in ({quoted}) AND statusCategory != Done",
            "fields": ["description"],
            "maxResults": 100,
        }
        while True:
            result = self.call("POST", "/rest/api/3/search/jql", body)
            for issue in result.get("issues", []):
                # 이슈 본문에 넣어 둔 커밋 링크에서 레포와 sha 를 읽는다.
                match = COMMIT_URL_RE.search(json.dumps(issue["fields"].get("description") or {}))
                if match:
                    yield issue["key"], *match.groups()
            token = result.get("nextPageToken")
            if not token:
                return
            body["nextPageToken"] = token

    def transitions(self, key: str) -> list[dict]:
        return self.call("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]

    def complete(self, key: str, preferred: str, comment: str, dry_run: bool) -> str | None:
        """이슈를 완료 상태로 옮기고 도착한 상태 이름을 돌려준다. 갈 방법이 없으면 None.

        '할 일 → 진행 중 → 완료' 처럼 바로 갈 수 없는 워크플로는 아직 안 거친 상태를 따라 최대 6단계까지 간다.
        dry_run 이면 실제로 옮기지 않고 경로만 계산한다(첫 단계까지만 확인 가능).
        """
        path: list[dict] = []
        visited = {self.call("GET", f"/rest/api/3/issue/{key}?fields=status")["fields"]["status"]["name"]}
        for _ in range(6):
            transitions = self.transitions(key)
            done = pick_done_transition(transitions, preferred)
            if done:
                path.append(done)
                break
            # 진행 중 상태를 우선 거치고, SMSDEV 처럼 '백로그 → 미해결' 을 먼저 지나야 하면 할 일 상태도 거친다.
            candidates = [t for t in transitions if t["to"]["name"] not in visited]
            step = next((t for category in ("indeterminate", "new") for t in candidates
                         if t["to"]["statusCategory"]["key"] == category), None)
            if not step:
                return None
            path.append(step)
            visited.add(step["to"]["name"])
            if dry_run:  # 실제로 옮기지 않으면 그다음 전환은 알 수 없다
                return " → ".join(t["to"]["name"] for t in path) + " → …"
            self.call("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": step["id"]}})
        else:
            return None

        if not dry_run:
            self.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
            }})
            self.call("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": path[-1]["id"]}})
        return " → ".join(t["to"]["name"] for t in path)


# 상태 분류는 '완료'지만 작업이 끝난 게 아닌 전환 (예: ITSM 의 '취소')
NOT_DONE_WORDS = ("취소", "거절", "반려", "철회", "cancel", "reject", "decline", "won't", "invalid", "duplicate")


def pick_done_transition(transitions: list[dict], preferred: str) -> dict | None:
    if preferred:
        return next((t for t in transitions if preferred in (t["name"], t["to"]["name"])), None)
    for t in transitions:
        name = f"{t['name']} {t['to']['name']}".lower()
        if t["to"]["statusCategory"]["key"] == "done" and not any(w in name for w in NOT_DONE_WORDS):
            return t
    return None


COMMIT_URL_RE = re.compile(r"https://github\.com/([^/\"]+)/([^/\"]+)/commit/([0-9a-f]{40})")


# ------------------------------------------------------------------------ commits


@dataclass
class Commit:
    sha: str
    repo: str
    branch: str
    message: str
    url: str
    author_name: str
    date: str

    @property
    def label(self) -> str:
        return LABEL_PREFIX + self.sha

    @property
    def title(self) -> str:
        return self.message.splitlines()[0].strip() if self.message.strip() else "(no message)"


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_commits(gh: GitHub, org: str, author: str, since: datetime, all_branches: bool, skip_merges: bool,
                    repo_filter: set[str]) -> list[Commit]:
    author_id = gh.user_node_id(author)
    commits: dict[str, Commit] = {}
    for repo in gh.recently_pushed_repos(org, since):
        if repo_filter and repo["name"] not in repo_filter:
            continue
        default = repo["default_branch"]
        for branch, c in gh.branch_commits(repo["owner"]["login"], repo["name"], author_id, since):
            if not all_branches and branch != default:
                continue
            if skip_merges and c["parents"]["totalCount"] > 1:
                continue
            # 같은 커밋이 여러 브랜치에 있으면 기본 브랜치를 우선 기록한다.
            if c["oid"] in commits and branch != default:
                continue
            commits[c["oid"]] = Commit(
                sha=c["oid"],
                repo=repo["full_name"],
                branch=branch,
                message=c["message"],
                url=c["url"],
                author_name=c["author"]["name"],
                date=c["author"]["date"],
            )
    return sorted(commits.values(), key=lambda c: c.date)


# ---------------------------------------------------------------------------- LLM

# 사내 AX Link 게이트웨이(OpenAI Chat Completions 형식). 모델명 프리픽스로 provider 가 정해진다.
AX_LINK_API = "https://ax-link-api.fnf.co.kr/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
MAX_FILES_IN_PROMPT = 40
MAX_DIFF_CHARS = 6000

TICKET_SYSTEM_PROMPT = """\
당신은 F&F IT 조직의 개발자입니다. 방금 GitHub에 올린 커밋을 근거로, 사내 IT 업무 요청 채널(Jira ITSM)에 \
남길 작업 티켓을 직접 작성합니다. 이 티켓은 현업 담당자와 IT 동료가 "무엇이, 왜, 어떻게 바뀌었는지"를 \
코드를 보지 않고도 이해할 수 있어야 합니다.

아래 JSON 하나만 출력하세요. 코드펜스나 다른 설명은 붙이지 마세요.
{
  "summary": "티켓 제목. 60자 이내. 어떤 화면/기능에서 무엇을 개선·수정했는지 업무 언어로. 레포 이름, 'feat:'/'fix:' 같은 접두어는 빼세요.",
  "background": "왜 이 작업이 필요했는지 2~3문장. 어떤 문제나 요구가 있었는지.",
  "changes": ["무엇을 어떻게 바꿨는지 3~6개 항목. 각 항목은 한 문장, '~하도록 수정' '~를 추가' 같은 개조식."],
  "impact": "이 변경으로 사용자·업무에 무엇이 나아지는지 1~3문장.",
  "check_points": ["배포 후 현업이나 IT가 확인해 보면 좋을 점 0~3개. 근거가 없으면 빈 배열."],
  "tasks": [{"summary": "개발 작업 제목. 40자 이내.", "description": "이 작업에서 한 일 1~3문장."}]
}

작성 규칙:
- 근거는 제공된 커밋 메시지, 변경 파일, 줄 수(있으면 diff)에 실제로 드러난 사실로 한정합니다. \
없는 기능·수치·영향을 지어내지 마세요. 확실하지 않은 내용은 쓰지 마세요.
- 전문 용어는 필요한 만큼만 쓰고, 파일명·함수명 나열보다 화면·기능·업무 흐름 중심으로 설명하세요.
- 정중한 존댓말 서술체. 과장된 홍보 문구, 이모지, 마크다운 문법은 쓰지 마세요.
- 커밋 해시나 링크는 넣지 마세요(따로 붙습니다).
- tasks는 이 커밋의 작업을 개발팀 백로그(에픽 아래 작업)용으로 나눈 것입니다. 화면·API·DB·배치·설정처럼 \
성격이 다른 변경은 따로, 같은 성격의 자잘한 변경은 하나로 묶어 1~6개로 나누세요. 작은 커밋이면 1개로 충분합니다."""

# Co-Authored-By 같은 트레일러가 남아 있으면 모델이 그걸 내용으로 착각하므로 지운다.
TRAILER_RE = re.compile(
    r"^\s*(co-authored-by:|signed-off-by:|co-committed-by:|reviewed-by:|🤖|generated with\b)", re.IGNORECASE
)


def clean_message(message: str) -> str:
    return "\n".join(line for line in message.splitlines() if not TRAILER_RE.match(line)).strip()


def llm_complete(system: str, user: str, api_key: str, model: str) -> str:
    payload, _ = request("POST", AX_LINK_API, {"Authorization": f"Bearer {api_key}", "User-Agent": "automate-jira"}, {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": 4096,
    }, timeout=120)
    choices = payload.get("choices") or []
    text = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
    if not text:
        raise RuntimeError(f"AX Link 응답이 비어 있음: {str(payload)[:200]}")
    return text


def ticket_prompt(c: Commit, detail: dict, send_diff: bool) -> str:
    message = clean_message(c.message)
    subject, _, body = message.partition("\n")
    lines = [
        f"저장소: {c.repo} (브랜치 {c.branch})",
        f"커밋 제목: {subject}",
    ]
    if body.strip():
        lines.append(f"커밋 본문:\n{body.strip()}")
    lines.append(f"변경 규모: 파일 {len(detail['files'])}개, +{detail['additions']} / -{detail['deletions']} 줄")
    lines.append("변경 파일:")
    for f in detail["files"][:MAX_FILES_IN_PROMPT]:
        lines.append(f"  - {f['status']} {f['name']} (+{f['additions']}/-{f['deletions']})")
    if len(detail["files"]) > MAX_FILES_IN_PROMPT:
        lines.append(f"  - …외 {len(detail['files']) - MAX_FILES_IN_PROMPT}개 파일")
    if send_diff:
        budget = MAX_DIFF_CHARS
        for f in detail["files"]:
            if not f["patch"] or budget <= 0:
                continue
            chunk = f["patch"][:budget]
            budget -= len(chunk)
            lines.append(f"diff {f['name']}:\n{chunk}")
    lines.append("\n위 커밋을 바탕으로 작업 티켓 JSON을 작성해 주세요.")
    return "\n".join(lines)


def parse_ticket(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)  # 모델이 코드펜스를 붙여도 본문 JSON 만 꺼낸다
    if not match:
        raise RuntimeError(f"JSON 을 찾지 못함: {text[:200]}")
    ticket = json.loads(match.group(0))
    if not str(ticket.get("summary", "")).strip():
        raise RuntimeError("summary 가 비어 있음")
    return {
        "summary": str(ticket["summary"]).strip(),
        "background": str(ticket.get("background") or "").strip(),
        "changes": [str(x).strip() for x in ticket.get("changes") or [] if str(x).strip()],
        "impact": str(ticket.get("impact") or "").strip(),
        "check_points": [str(x).strip() for x in ticket.get("check_points") or [] if str(x).strip()],
        "tasks": [
            {"summary": str(t["summary"]).strip(), "description": str(t.get("description") or "").strip()}
            for t in ticket.get("tasks") or [] if isinstance(t, dict) and str(t.get("summary") or "").strip()
        ],
    }


def write_ticket(c: Commit, detail: dict) -> dict | None:
    """LLM 으로 티켓 문구를 만든다. 꺼져 있거나 실패하면 None (커밋 메시지 그대로 사용)."""
    api_key = env("AX_LINK_API_KEY")
    if not api_key or not env_bool("USE_LLM", True):
        return None
    try:
        text = llm_complete(TICKET_SYSTEM_PROMPT, ticket_prompt(c, detail, env_bool("SEND_DIFF", False)),
                            api_key, env("AX_LINK_MODEL", DEFAULT_MODEL))
        return parse_ticket(text)
    except (HttpError, RuntimeError, ValueError, OSError) as e:
        print(f"  ! LLM 문구 생성 실패, 커밋 메시지로 대체 ({c.repo}@{c.sha[:7]}): {e}", file=sys.stderr)
        return None


# ------------------------------------------------------------------------ issues


def adf_text(text: str, href: str | None = None, strong: bool = False) -> dict:
    node = {"type": "text", "text": text}
    marks = []
    if href:
        marks.append({"type": "link", "attrs": {"href": href}})
    if strong:
        marks.append({"type": "strong"})
    if marks:
        node["marks"] = marks
    return node


def adf_heading(text: str) -> dict:
    return {"type": "heading", "attrs": {"level": 3}, "content": [adf_text(text)]}


def adf_paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [adf_text(text)]}


def adf_bullets(items: list[str]) -> dict:
    return {"type": "bulletList", "content": [
        {"type": "listItem", "content": [adf_paragraph(item)]} for item in items
    ]}


def adf_commit_info(c: Commit, detail: dict) -> list[dict]:
    def field(name: str, text: str, href: str | None = None) -> dict:
        return {"type": "paragraph", "content": [adf_text(f"{name}: ", strong=True), adf_text(text, href)]}

    return [
        field("Commit", c.sha[:12], c.url),
        field("Repository", c.repo, f"https://github.com/{c.repo}"),
        field("Branch", c.branch),
        field("Author", c.author_name),
        field("Date", c.date),
        field("변경 규모", f"파일 {len(detail['files'])}개, +{detail['additions']} / -{detail['deletions']} 줄"),
        {"type": "codeBlock", "content": [adf_text(clean_message(c.message) or "(no message)")]},
    ]


def adf_description(c: Commit, detail: dict, ticket: dict | None) -> dict:
    content = []
    if ticket:
        if ticket["background"]:
            content += [adf_heading("배경"), adf_paragraph(ticket["background"])]
        if ticket["changes"]:
            content += [adf_heading("작업 내용"), adf_bullets(ticket["changes"])]
        if ticket["impact"]:
            content += [adf_heading("기대 효과"), adf_paragraph(ticket["impact"])]
        if ticket["check_points"]:
            content += [adf_heading("확인 포인트"), adf_bullets(ticket["check_points"])]
        content += [{"type": "rule"}, adf_heading("커밋 정보")]
    return {"type": "doc", "version": 1, "content": content + adf_commit_info(c, detail)}


def issue_fields(c: Commit, detail: dict, ticket: dict | None, project: str, issue_type: str,
                 extra_labels: list[str], assignee: str | None, component: str | None = None) -> dict:
    repo_name = c.repo.split("/", 1)[1]
    title = ticket["summary"] if ticket else c.title
    fields = {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
        "summary": f"[{repo_name}] {title}"[:255],
        "description": adf_description(c, detail, ticket),
        "labels": [LABEL_PREFIX.rstrip("-"), c.label, *extra_labels],
    }
    if assignee:
        fields["assignee"] = {"accountId": assignee}
    if component:
        fields["components"] = [{"name": component}]
    return fields


def epic_tasks(c: Commit, ticket: dict | None) -> list[dict]:
    """에픽 아래 둘 작업 목록. LLM 이 나눈 tasks → 없으면 작업 내용 항목 → 그것도 없으면 커밋 제목 1개."""
    if ticket and ticket["tasks"]:
        return ticket["tasks"]
    if ticket and ticket["changes"]:
        return [{"summary": change, "description": ""} for change in ticket["changes"]]
    return [{"summary": ticket["summary"] if ticket else c.title, "description": ""}]


def task_fields(c: Commit, detail: dict, task: dict, project: str, issue_type: str, epic_key: str,
                extra_labels: list[str], assignee: str | None) -> dict:
    # 커밋 링크와 라벨을 에픽과 똑같이 달아 두면, 머지될 때 같은 규칙으로 함께 완료된다.
    content = [adf_paragraph(task["description"])] if task["description"] else []
    content += [{"type": "rule"}, adf_heading("커밋 정보"), *adf_commit_info(c, detail)]
    fields = {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
        "parent": {"key": epic_key},
        "summary": f"[{c.repo.split('/', 1)[1]}] {task['summary']}"[:255],
        "description": {"type": "doc", "version": 1, "content": content},
        "labels": [LABEL_PREFIX.rstrip("-"), c.label, *extra_labels],
    }
    if assignee:
        fields["assignee"] = {"accountId": assignee}
    return fields


def require_issue_type(jira: Jira, project: str, issue_type: str, env_name: str) -> None:
    available = jira.issue_type_names(project)
    if issue_type not in available:
        sys.exit(f"프로젝트 {project} 에 이슈 유형 '{issue_type}' 이 없습니다. {env_name} 을 다음 중 하나로 지정하세요: {available}")


# --------------------------------------------------------------------------- main


def create_issues(gh: GitHub, jira: Jira, org: str, project: str, labels: list[str], dry_run: bool) -> int:
    author = env("GH_AUTHOR") or gh.login()
    since = datetime.now(timezone.utc) - timedelta(hours=float(env("LOOKBACK_HOURS", "24")))
    repo_filter = {r.strip() for r in env("GH_REPOS").split(",") if r.strip()}

    print(f"[생성] org={org} author={author} since={iso(since)}")
    commits = collect_commits(
        gh, org, author, since,
        all_branches=env("BRANCH_SCOPE", "all") == "all",
        skip_merges=env_bool("SKIP_MERGE_COMMITS", True),
        repo_filter=repo_filter,
    )
    print(f"내 커밋 {len(commits)}개 발견")
    if not commits:
        return 0

    components = env_map("REPO_COMPONENTS")
    epic_projects = env_map("REPO_EPIC_PROJECTS")

    def repo_name(c: Commit) -> str:
        return c.repo.split("/", 1)[1]

    existing = jira.keys_by_label(project, [c.label for c in commits])
    # 에픽은 ITSM 이슈와 따로 확인한다. 에픽 생성만 실패했던 커밋은 다음 실행에서 에픽만 다시 만든다.
    epic_existing: dict[str, set[str]] = {}
    for epic_project in set(epic_projects.values()):
        labels_in_project = [c.label for c in commits if epic_projects.get(repo_name(c)) == epic_project]
        epic_existing[epic_project] = set(jira.keys_by_label(epic_project, labels_in_project))

    def needs_epic(c: Commit) -> bool:
        epic_project = epic_projects.get(repo_name(c))
        return bool(epic_project) and c.label not in epic_existing[epic_project]

    todo = [c for c in commits if c.label not in existing or needs_epic(c)]
    new_count = sum(c.label not in existing for c in todo)
    print(f"이미 등록됨 {len(commits) - new_count}개, 새로 생성할 커밋 {new_count}개, "
          f"에픽만 만들 커밋 {len(todo) - new_count}개")
    if not todo:
        return 0

    issue_type = env("JIRA_ISSUE_TYPE", "Task")
    require_issue_type(jira, project, issue_type, "JIRA_ISSUE_TYPE")
    epic_type = env("EPIC_ISSUE_TYPE", "에픽")
    task_type = env("EPIC_TASK_TYPE", "작업")
    for epic_project in {epic_projects[repo_name(c)] for c in todo if needs_epic(c)}:
        require_issue_type(jira, epic_project, epic_type, "EPIC_ISSUE_TYPE")
        require_issue_type(jira, epic_project, task_type, "EPIC_TASK_TYPE")
    link_type = env("EPIC_LINK_TYPE", "Relates")
    assignee = env("JIRA_ASSIGNEE_ACCOUNT_ID", "me")
    if assignee == "me":
        assignee = jira.my_account_id()
    elif assignee == "none":
        assignee = None

    failed = 0
    for c in todo:
        try:
            detail = gh.commit_detail(c.repo, c.sha)
            ticket = write_ticket(c, detail)
            fields = issue_fields(c, detail, ticket, project, issue_type, labels, assignee,
                                  components.get(repo_name(c)))
            epic_project = epic_projects.get(repo_name(c)) if needs_epic(c) else None
            epic_fields = epic_project and issue_fields(c, detail, ticket, epic_project, epic_type, labels, assignee)
            if dry_run:
                key = existing.get(c.label)
                print(f"  [dry-run] {c.repo}@{c.sha[:7]} → {key or '(새 이슈)'} {fields['summary']}"
                      + (f" / 컴포넌트 {fields['components'][0]['name']}" if "components" in fields and not key else "")
                      + (f" / {epic_project} 에픽 생성 후 {link_type} 링크" if epic_project else ""))
                if ticket and c.label not in existing:
                    print("    " + json.dumps(ticket, ensure_ascii=False, indent=2).replace("\n", "\n    "))
                if epic_project:
                    for task in epic_tasks(c, ticket):
                        print(f"    └ {task_type}: {task['summary']}")
                continue
            key = existing.get(c.label)
            if not key:
                key = jira.create_issue(fields)
                print(f"  생성 {key}: {c.repo}@{c.sha[:7]} {fields['summary']}")
            if epic_fields:
                epic_key = jira.create_issue(epic_fields)
                jira.link(key, epic_key, link_type)
                print(f"  에픽 {epic_key} ↔ {key}: {epic_fields['summary']}")
                for task in epic_tasks(c, ticket):
                    task_key = jira.create_issue(task_fields(c, detail, task, epic_project, task_type, epic_key,
                                                             labels, assignee))
                    print(f"    └ {task_type} {task_key}: {task['summary']}")
        except HttpError as e:
            failed += 1
            print(f"  실패 {c.repo}@{c.sha[:7]}: {e}", file=sys.stderr)
    return failed


def complete_merged(gh: GitHub, jira: Jira, project: str, labels: list[str], dry_run: bool) -> int:
    base_branches = {b.strip() for b in env("MERGE_BASE_BRANCHES").split(",") if b.strip()}
    preferred = env("JIRA_DONE_TRANSITION")

    # 에픽도 본문에 같은 커밋 링크가 있으므로 ITSM 이슈와 같은 규칙으로 완료된다.
    projects = [project, *sorted(set(env_map("REPO_EPIC_PROJECTS").values()) - {project})]
    issues = list(jira.open_commit_issues(projects, [LABEL_PREFIX.rstrip("-"), *labels]))
    print(f"[완료] 아직 열린 커밋 이슈 {len(issues)}개 확인")

    failed = 0
    for key, owner, repo, sha in issues:
        try:
            reason = gh.merge_reason(owner, repo, sha, base_branches)
            if not reason:
                continue
            path = jira.complete(key, preferred, f"[automate-jira] {reason}", dry_run)
            if not path:
                failed += 1
                print(f"  {key}: 완료로 보낼 전환(transition)을 찾지 못함", file=sys.stderr)
                continue
            print(f"  {'[dry-run] ' if dry_run else ''}완료 {key} → {path} ({reason})")
        except (HttpError, RuntimeError) as e:
            failed += 1
            print(f"  실패 {key}: {e}", file=sys.stderr)
    return failed


def main() -> int:
    dry_run = env_bool("DRY_RUN", False)
    gh = GitHub(env("GH_TOKEN", required=True))
    jira = Jira(env("JIRA_BASE_URL", required=True), env("JIRA_EMAIL", required=True),
                env("JIRA_API_TOKEN", required=True))
    org = env("GH_ORG", required=True)
    project = env("JIRA_PROJECT_KEY", required=True)
    labels = [label.strip() for label in env("JIRA_LABELS", "auto-commit").split(",") if label.strip()]
    print(f"{datetime.now().isoformat(timespec='seconds')} dry_run={dry_run}")

    failed = create_issues(gh, jira, org, project, labels, dry_run)
    if env_bool("AUTO_DONE_ON_MERGE", True):
        failed += complete_merged(gh, jira, project, labels, dry_run)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
