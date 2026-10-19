#!/usr/bin/env python3
"""GitHub 조직(org)에서 '내' 커밋을 찾아 Jira 이슈로 자동 생성한다.

동작 방식
  1. 최근 LOOKBACK_HOURS 동안 push된 org 레포를 훑는다.
  2. 각 레포의 브랜치에서 GH_AUTHOR(기본: 토큰 소유자)의 커밋만 모은다.
  3. 이미 Jira에 만들어진 커밋(라벨 gh-commit-<sha>)은 건너뛴다.
  4. 나머지를 오래된 순서대로 Jira 이슈로 만든다.
  5. 아직 열린 자동 생성 이슈 중, 커밋이 머지된 것은 완료로 전환한다.

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


def request(method: str, url: str, headers: dict, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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

        브랜치를 마지막 커밋 날짜 내림차순으로 받아 since 이전 브랜치가 나오면 멈추므로,
        브랜치가 수백 개인 레포도 요청 몇 번이면 끝난다.
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
                    return
                for commit in target["history"]["nodes"]:
                    yield ref["name"], commit
            if not refs["pageInfo"]["hasNextPage"]:
                return
            cursor = refs["pageInfo"]["endCursor"]

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
                return f"PR #{pr['number']} 이(가) {pr['baseRefName']} 브랜치로 머지됨: {pr['url']}"
        default = repo["defaultBranchRef"]["name"]
        if base_branches and default not in base_branches:
            return None
        compare, _ = self.get(f"/repos/{owner}/{name}/compare/{urllib.parse.quote(default)}...{sha}")
        if compare["status"] in ("behind", "identical"):
            return f"커밋이 {default} 브랜치에 포함됨"
        return None


MERGE_QUERY = """
query($owner: String!, $name: String!, $oid: GitObjectID!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    object(oid: $oid) {
      ... on Commit {
        associatedPullRequests(first: 10) {
          nodes { number url merged baseRefName }
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

    def existing_labels(self, project: str, labels: list[str]) -> set[str]:
        """labels 중 이미 project 의 이슈에 붙어 있는 것만 돌려준다."""
        found: set[str] = set()
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
                    found.update(issue["fields"].get("labels") or [])
                token = result.get("nextPageToken")
                if not token:
                    break
                body["nextPageToken"] = token
        return found & set(labels)

    def create_issue(self, fields: dict) -> str:
        return self.call("POST", "/rest/api/3/issue", {"fields": fields})["key"]

    def open_commit_issues(self, project: str, labels: list[str]):
        """아직 완료되지 않은 자동 생성 이슈를 (key, owner, repo, sha) 로 돌려준다."""
        quoted = ", ".join(f'"{label}"' for label in labels)
        body = {
            "jql": f'project = "{project}" AND labels in ({quoted}) AND statusCategory != Done',
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

        '할 일 → 진행 중 → 완료' 처럼 바로 갈 수 없는 워크플로는 진행 중 상태를 거쳐 간다.
        dry_run 이면 실제로 옮기지 않고 경로만 계산한다(첫 단계까지만 확인 가능).
        """
        path: list[dict] = []
        for _ in range(3):
            transitions = self.transitions(key)
            done = pick_done_transition(transitions, preferred)
            if done:
                path.append(done)
                break
            visited = {t["to"]["name"] for t in path}
            step = next((t for t in transitions if t["to"]["statusCategory"]["key"] == "indeterminate"
                         and t["to"]["name"] not in visited), None)
            if not step:
                return None
            path.append(step)
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


def adf_description(c: Commit) -> dict:
    def field(name: str, text: str, href: str | None = None) -> dict:
        value = {"type": "text", "text": text}
        if href:
            value["marks"] = [{"type": "link", "attrs": {"href": href}}]
        return {
            "type": "paragraph",
            "content": [{"type": "text", "text": f"{name}: ", "marks": [{"type": "strong"}]}, value],
        }

    return {
        "type": "doc",
        "version": 1,
        "content": [
            field("Commit", c.sha[:12], c.url),
            field("Repository", c.repo, f"https://github.com/{c.repo}"),
            field("Branch", c.branch),
            field("Author", c.author_name),
            field("Date", c.date),
            {"type": "codeBlock", "content": [{"type": "text", "text": c.message or "(no message)"}]},
        ],
    }


def issue_fields(c: Commit, project: str, issue_type: str, extra_labels: list[str], assignee: str | None) -> dict:
    repo_name = c.repo.split("/", 1)[1]
    fields = {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
        "summary": f"[{repo_name}] {c.title}"[:255],
        "description": adf_description(c),
        "labels": [LABEL_PREFIX.rstrip("-"), c.label, *extra_labels],
    }
    if assignee:
        fields["assignee"] = {"accountId": assignee}
    return fields


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

    existing = jira.existing_labels(project, [c.label for c in commits])
    new = [c for c in commits if c.label not in existing]
    print(f"이미 등록됨 {len(commits) - len(new)}개, 새로 생성할 커밋 {len(new)}개")
    if not new:
        return 0

    issue_type = env("JIRA_ISSUE_TYPE", "Task")
    available = jira.issue_type_names(project)
    if issue_type not in available:
        sys.exit(f"프로젝트 {project} 에 이슈 유형 '{issue_type}' 이 없습니다. JIRA_ISSUE_TYPE 을 다음 중 하나로 지정하세요: {available}")
    assignee = env("JIRA_ASSIGNEE_ACCOUNT_ID", "me")
    if assignee == "me":
        assignee = jira.my_account_id()
    elif assignee == "none":
        assignee = None

    failed = 0
    for c in new:
        fields = issue_fields(c, project, issue_type, labels, assignee)
        if dry_run:
            print(f"  [dry-run] {c.repo}@{c.sha[:7]} → {fields['summary']}")
            continue
        try:
            key = jira.create_issue(fields)
            print(f"  생성 {key}: {c.repo}@{c.sha[:7]} {c.title}")
        except HttpError as e:
            failed += 1
            print(f"  실패 {c.repo}@{c.sha[:7]}: {e}", file=sys.stderr)
    return failed


def complete_merged(gh: GitHub, jira: Jira, project: str, labels: list[str], dry_run: bool) -> int:
    base_branches = {b.strip() for b in env("MERGE_BASE_BRANCHES").split(",") if b.strip()}
    preferred = env("JIRA_DONE_TRANSITION")

    issues = list(jira.open_commit_issues(project, [LABEL_PREFIX.rstrip("-"), *labels]))
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
