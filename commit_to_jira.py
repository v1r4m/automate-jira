#!/usr/bin/env python3
"""GitHub 조직(org)에서 '내' 커밋을 찾아 Jira 이슈로 자동 생성한다.

동작 방식
  1. 최근 LOOKBACK_HOURS 동안 push된 org 레포를 훑는다.
  2. 각 레포의 브랜치에서 GH_AUTHOR(기본: 토큰 소유자)의 커밋만 모은다.
  3. 이미 Jira에 만들어진 커밋(라벨 gh-commit-<sha>)은 건너뛴다.
  4. 나머지를 오래된 순서대로 Jira 이슈로 만든다.

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
        "labels": [c.label, *extra_labels],
    }
    if assignee:
        fields["assignee"] = {"accountId": assignee}
    return fields


# --------------------------------------------------------------------------- main


def main() -> int:
    dry_run = env_bool("DRY_RUN", False)

    gh = GitHub(env("GH_TOKEN", required=True))
    org = env("GH_ORG", required=True)
    author = env("GH_AUTHOR") or gh.login()
    since = datetime.now(timezone.utc) - timedelta(hours=float(env("LOOKBACK_HOURS", "24")))
    repo_filter = {r.strip() for r in env("GH_REPOS").split(",") if r.strip()}

    print(f"org={org} author={author} since={iso(since)} dry_run={dry_run}")
    commits = collect_commits(
        gh, org, author, since,
        all_branches=env("BRANCH_SCOPE", "all") == "all",
        skip_merges=env_bool("SKIP_MERGE_COMMITS", True),
        repo_filter=repo_filter,
    )
    print(f"내 커밋 {len(commits)}개 발견")
    if not commits:
        return 0

    jira = Jira(env("JIRA_BASE_URL", required=True), env("JIRA_EMAIL", required=True),
                env("JIRA_API_TOKEN", required=True))
    project = env("JIRA_PROJECT_KEY", required=True)
    issue_type = env("JIRA_ISSUE_TYPE", "Task")
    available = jira.issue_type_names(project)
    if issue_type not in available:
        sys.exit(f"프로젝트 {project} 에 이슈 유형 '{issue_type}' 이 없습니다. JIRA_ISSUE_TYPE 을 다음 중 하나로 지정하세요: {available}")
    extra_labels = [label.strip() for label in env("JIRA_LABELS", "auto-commit").split(",") if label.strip()]
    assignee = env("JIRA_ASSIGNEE_ACCOUNT_ID", "me")
    if assignee == "me":
        assignee = jira.my_account_id()
    elif assignee == "none":
        assignee = None

    existing = jira.existing_labels(project, [c.label for c in commits])
    new = [c for c in commits if c.label not in existing]
    print(f"이미 등록됨 {len(commits) - len(new)}개, 새로 생성할 커밋 {len(new)}개")

    failed = 0
    for c in new:
        fields = issue_fields(c, project, issue_type, extra_labels, assignee)
        if dry_run:
            print(f"  [dry-run] {c.repo}@{c.sha[:7]} → {fields['summary']}")
            continue
        try:
            key = jira.create_issue(fields)
            print(f"  생성 {key}: {c.repo}@{c.sha[:7]} {c.title}")
        except HttpError as e:
            failed += 1
            print(f"  실패 {c.repo}@{c.sha[:7]}: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
