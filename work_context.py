"""Fetch and summarize work context from GitHub and Jira."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote_plus

from env_utils import load_env_file

load_env_file()

try:
    import requests
except ModuleNotFoundError:
    requests = None


GITHUB_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class WorkContextConfig:
    github_repos: Sequence[str] = ()
    jira_base_url: str = ""
    jira_project_key: str = ""
    jira_jql: str = ""
    github_token: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    persona: str = ""
    call_type: str = ""
    max_prs_per_repo: int = 5
    max_comments_per_item: int = 3
    max_jira_issues: int = 10

    @property
    def has_github(self) -> bool:
        return bool(self.github_repos)

    @property
    def has_jira(self) -> bool:
        return bool(self.jira_base_url and (self.jira_jql or self.jira_project_key))


def build_work_context_brief(config: WorkContextConfig, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"meera_context_brief_{timestamp}.md"

    lines: list[str] = [
        "# Meera Work Context Brief",
        "",
        f"Generated: {dt.datetime.now().isoformat(sep=' ', timespec='seconds')}",
        "",
    ]

    jira_section, linked_pr_refs = build_jira_section(config)
    github_section = build_github_section(config, linked_pr_refs)
    prep_section = build_prep_section(github_section, jira_section, config.persona, config.call_type)

    if config.has_github or linked_pr_refs:
        lines.extend(github_section)
        lines.extend(["", "---", ""])
    lines.extend(jira_section)
    lines.extend(["", "---", ""])
    lines.extend(prep_section)
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def build_github_section(
    config: WorkContextConfig, linked_pr_refs: Sequence[str] = ()
) -> list[str]:
    lines = ["## GitHub Context"]
    if not config.has_github:
        if linked_pr_refs:
            lines.append("_GitHub repositories were not configured, but Jira comments linked to PRs._")
        else:
            lines.append("_GitHub context was not provided._")
        return lines
    if requests is None:
        lines.append("_requests is not installed, so GitHub context could not be fetched._")
        return lines

    session = requests.Session()
    if config.github_token:
        session.headers.update(
            {
                "Authorization": f"Bearer {config.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            }
        )

    for repo in config.github_repos:
        repo = repo.strip()
        if not repo:
            continue
        try:
            owner, name = _parse_repo(repo)
        except ValueError as exc:
            lines.append(f"- `{repo}`: {exc}")
            continue

        lines.append(f"### {owner}/{name}")
        prs = _fetch_github_pull_requests(session, owner, name, config.max_prs_per_repo)
        if not prs:
            lines.append("- No open pull requests found.")
            continue

        for pr in prs:
            lines.extend(_format_github_pr(session, owner, name, pr, config.max_comments_per_item))

    linked_prs = _fetch_linked_github_prs(session, linked_pr_refs, config.max_comments_per_item)
    if linked_prs:
        lines.append("### Linked PRs from Jira")
        for owner, repo, pr in linked_prs:
            lines.extend(_format_github_pr(session, owner, repo, pr, config.max_comments_per_item))
    return lines


def _format_github_pr(
    session: requests.Session,
    owner: str,
    repo: str,
    pr: dict,
    max_comments: int,
) -> list[str]:
    number = pr.get("number")
    title = pr.get("title", "")
    url = pr.get("html_url", "")
    user = (pr.get("user") or {}).get("login", "unknown")
    updated_at = pr.get("updated_at", "")
    body = (pr.get("body") or "").strip()

    lines = [
        f"- PR #{number}: {title}",
        f"  - Author: {user}",
        f"  - Updated: {updated_at}",
        f"  - URL: {url}",
    ]
    if body:
        lines.append(f"  - Summary: {_trim_text(body, 280)}")

    issue_comments = _fetch_github_issue_comments(session, owner, repo, number, max_comments)
    review_comments = _fetch_github_review_comments(session, owner, repo, number, max_comments)
    files = _fetch_github_pr_files(session, owner, repo, number, 5)

    if files:
        lines.append(f"  - Files: {', '.join(files)}")

    if issue_comments:
        lines.append("  - Issue comments:")
        for comment in issue_comments:
            lines.append(f"    - {comment}")

    if review_comments:
        lines.append("  - Review comments:")
        for comment in review_comments:
            lines.append(f"    - {comment}")

    return lines


def build_jira_section(config: WorkContextConfig) -> tuple[list[str], list[str]]:
    lines = ["## Jira Context"]
    linked_pr_refs: list[str] = []
    if not config.has_jira:
        lines.append("_No Jira configuration found._")
        return lines, linked_pr_refs
    if requests is None:
        lines.append("_requests is not installed, so Jira context could not be fetched._")
        return lines, linked_pr_refs

    session = requests.Session()
    if config.jira_email and config.jira_api_token:
        session.auth = (config.jira_email, config.jira_api_token)

    base_url = config.jira_base_url.rstrip("/")
    jira_jql = _build_jira_jql(config.jira_project_key, config.jira_jql)
    issues = _fetch_jira_issues(session, base_url, jira_jql, config.max_jira_issues)

    if config.jira_project_key:
        lines.append(f"- Project key: {config.jira_project_key}")
    if jira_jql:
        lines.append(f"- Query: {jira_jql}")

    if not issues:
        lines.append("_No Jira issues matched the query._")
        return lines, linked_pr_refs

    for issue in issues:
        fields = issue.get("fields", {})
        key = issue.get("key", "")
        summary = fields.get("summary", "")
        status = (fields.get("status") or {}).get("name", "")
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
        updated = fields.get("updated", "")
        lines.append(f"- {key}: {summary}")
        lines.append(f"  - Type: {issue_type}")
        lines.append(f"  - Status: {status}")
        lines.append(f"  - Assignee: {assignee}")
        lines.append(f"  - Updated: {updated}")

        comments = (((fields.get("comment") or {}).get("comments")) or [])[:max(0, config.max_comments_per_item)]
        if comments:
            lines.append("  - Comments:")
            for comment in comments:
                author = (comment.get("author") or {}).get("displayName", "Unknown")
                body = _jira_body_to_text(comment.get("body", ""), 220)
                lines.append(f"    - {author}: {body}")
                linked_pr_refs.extend(_extract_github_pr_refs(body))

    return lines, _dedupe(linked_pr_refs)


def _fetch_jira_issues(
    session: requests.Session,
    base_url: str,
    jira_jql: str,
    max_results: int,
) -> list[dict]:
    url = f"{base_url}/rest/api/3/search/jql"
    payload = {
        "jql": jira_jql,
        "maxResults": max_results,
        "fields": ["summary", "status", "comment", "updated", "issuetype", "assignee"],
    }
    response = session.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json().get("issues", [])


def _build_jira_jql(project_key: str, jira_jql: str) -> str:
    project_key = project_key.strip()
    jira_jql = jira_jql.strip()

    if not project_key and jira_jql:
        return jira_jql
    if project_key and not jira_jql:
        return f"project = {project_key} ORDER BY updated DESC"
    if not project_key and not jira_jql:
        return ""

    if re.search(r"\bproject\s*=", jira_jql, flags=re.IGNORECASE):
        return jira_jql

    order_match = re.search(r"\bORDER\s+BY\b", jira_jql, flags=re.IGNORECASE)
    if not order_match:
        return f"project = {project_key} AND ({jira_jql})"

    query_part = jira_jql[: order_match.start()].strip()
    order_part = jira_jql[order_match.end() :].strip()
    if query_part:
        return f"project = {project_key} AND ({query_part}) ORDER BY {order_part}"
    return f"project = {project_key} ORDER BY {order_part}"


def build_prep_section(
    github_section: list[str],
    jira_section: list[str],
    persona: str = "",
    call_type: str = "",
) -> list[str]:
    lines = ["## Call Prep / Transcript Draft"]
    highlights = _collect_highlights(github_section + jira_section)

    persona = persona.strip()
    call_type = call_type.strip()
    if persona or call_type:
        lines.append("### Persona / Call Type")
        if persona:
            lines.append(f"- Persona: {persona}")
        if call_type:
            lines.append(f"- Call type: {call_type}")

    if highlights:
        lines.append("### What Meera Should Say")
        persona_key = persona.lower()
        call_key = call_type.upper()
        if persona_key.startswith("software developer") and call_key in {"BUG CALL", "BUG FIX CALL"}:
            lines.append(
                "- I’m joining as a bug fixer, so I’ll focus on the issue, the comments, the blockers, and whether this should be closed or kept open."
            )
        elif persona_key.startswith("architect") and call_key == "ARCHITECTURE CALL":
            lines.append(
                "- I’m joining as an architect, so I’ll focus on design choices, tradeoffs, dependencies, and any risk in the current shape of the solution."
            )
        elif persona_key.startswith("business analyst") and call_key == "REQUIREMENTS CALL":
            lines.append(
                "- I’m joining as a business analyst, so I’ll focus on requirements, scope, edge cases, acceptance criteria, and anything that still needs clarification."
            )
        elif persona_key.startswith("sales") and call_key == "SALES CALL":
            lines.append(
                "- I’m joining for a sales call, so I’ll focus on pain points, business goals, objections, buying signals, and the next best step."
            )
        elif persona_key.startswith("marketing") and call_key == "MARKETING CALL":
            lines.append(
                "- I’m joining for a marketing call, so I’ll focus on the audience, positioning, campaign goals, channels, and success metrics."
            )
        elif persona:
            lines.append(
                "- This persona is not wired for context loading yet, so Meera will keep the call general."
            )
        if _section_has_content(github_section):
            lines.append(
                "- I reviewed the recent GitHub and Jira context and pulled out the most relevant updates."
            )
        else:
            lines.append(
                "- I reviewed the recent Jira context and pulled out the most relevant updates."
            )
        for item in highlights[:5]:
            lines.append(f"- {item}")
    else:
        lines.append("_No Jira or GitHub context was available to summarize._")

    lines.extend(
        [
            "",
            "### Suggested Flow",
            "1. Open with a short status update.",
            "2. Mention the main PRs and Jira items.",
            "3. Call out blockers, review comments, and unanswered questions.",
            "4. Close with the next action and owner.",
        ]
    )
    return lines


def _collect_highlights(lines: Sequence[str]) -> list[str]:
    highlights: list[str] = []
    for line in lines:
        text = line.strip()
        if text.startswith("- PR #") or text.startswith("- JIRA") or re.match(r"^- [A-Z][A-Z0-9]+-", text):
            highlights.append(text.lstrip("- ").strip())
        elif text.startswith("- ") and ("blocked" in text.lower() or "todo" in text.lower() or "fix" in text.lower()):
            highlights.append(text.lstrip("- ").strip())
    return highlights


def _section_has_content(lines: Sequence[str]) -> bool:
    return any(
        line.strip()
        and not line.startswith("## ")
        and line.strip() not in {
            "_GitHub context was not provided._",
            "_No GitHub repositories configured._",
            "_No repository or Jira context was available to summarize._",
        }
        for line in lines
    )


def _extract_github_pr_refs(text: object) -> list[str]:
    raw = str(text)
    pattern = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
    refs: list[str] = []
    for match in pattern.finditer(raw):
        owner, repo, number = match.group(1), match.group(2), match.group(3)
        refs.append(f"https://github.com/{owner}/{repo}/pull/{number}")
    return refs


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _parse_repo(repo: str) -> tuple[str, str]:
    repo = repo.strip()
    if repo.startswith("https://github.com/"):
        repo = repo.removeprefix("https://github.com/")
    parts = repo.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError("expected owner/repo or https://github.com/owner/repo")
    return parts[0], parts[1]


def _parse_github_pr_url(url: str) -> tuple[str, str, int] | None:
    pattern = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
    match = pattern.search(url.strip())
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def _fetch_github_pull_requests(
    session: requests.Session, owner: str, repo: str, per_repo_limit: int
) -> list[dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    response = session.get(
        url,
        params={"state": "open", "per_page": per_repo_limit, "sort": "updated", "direction": "desc"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _fetch_github_issue_comments(
    session: requests.Session, owner: str, repo: str, number: int, limit: int
) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments"
    response = session.get(url, params={"per_page": limit}, timeout=30)
    response.raise_for_status()
    return [_trim_text(comment.get("body", ""), 180) for comment in response.json()]


def _fetch_github_review_comments(
    session: requests.Session, owner: str, repo: str, number: int, limit: int
) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/comments"
    response = session.get(url, params={"per_page": limit}, timeout=30)
    response.raise_for_status()
    return [_trim_text(comment.get("body", ""), 180) for comment in response.json()]


def _fetch_github_pr_files(
    session: requests.Session, owner: str, repo: str, number: int, limit: int
) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"
    response = session.get(url, params={"per_page": limit}, timeout=30)
    response.raise_for_status()
    return [item.get("filename", "") for item in response.json() if item.get("filename")]


def _fetch_linked_github_prs(
    session: requests.Session,
    linked_pr_refs: Sequence[str],
    max_comments: int,
) -> list[tuple[str, str, dict]]:
    results: list[tuple[str, str, dict]] = []
    seen: set[str] = set()

    for ref in linked_pr_refs:
        parsed = _parse_github_pr_url(ref)
        if not parsed:
            continue
        owner, repo, number = parsed
        key = f"{owner}/{repo}#{number}"
        if key in seen:
            continue
        seen.add(key)

        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
        response = session.get(url, timeout=30)
        response.raise_for_status()
        results.append((owner, repo, response.json()))

    return results


def _trim_text(value: object, max_len: int) -> str:
    text = str(value).replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _jira_body_to_text(value: object, max_len: int = 220) -> str:
    text = _adf_to_text(value).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _adf_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(part for part in (_adf_to_text(item) for item in value) if part)
    if isinstance(value, dict):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        if "content" in value:
            return _adf_to_text(value["content"])
        if "value" in value and isinstance(value["value"], str):
            return value["value"]
        return " ".join(
            part for part in (_adf_to_text(item) for item in value.values()) if part
        )
    return str(value)
