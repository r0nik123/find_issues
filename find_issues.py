"""
Find open, unclaimed "good first issue" issues in GitHub repos with 10k+ stars.

"Unclaimed" means:
  - no assignee
  - no linked pull request
  - no recent comment like "I'd like to work on this" / "can I take this"

Usage:
  pip install requests
  export GITHUB_TOKEN=ghp_xxx      # free personal token, no scopes needed
  python find_issues.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

# ---------- settings you can change ----------
MIN_STARS = 10000
MAX_REPOS = 100  # how many repos to scan (search API max is 1000)
LANGUAGE = ""  # e.g. "python" or "" for any
LABELS = [
    '"good first issue"',
    '"good-first-issue"',
    '"Good First Issue"',
    '"beginner"',
    '"easy"',
]
MAX_COMMENTS = 5  # skip issues with many comments (usually already claimed)
MAX_ISSUE_AGE_DAYS = 365  # skip very old issues
ISSUES_PER_REPO = 10
# ---------------------------------------------

CLAIM_PATTERNS = re.compile(
    r"(i('d| would) like to (work|take)|can i (work|take|pick)|"
    r"assign (it|this) to me|i('ll| will) (work|take|pick)|working on (it|this)|"
    r"i('m| am) on it|may i (work|take)|please assign)",
    re.IGNORECASE,
)

TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("Set the GITHUB_TOKEN environment variable first.")

session = requests.Session()
session.headers.update(
    {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
)


def get(url, params=None, is_search=False):
    """GET with automatic waiting when rate-limited."""
    while True:
        r = session.get(url, params=params, timeout=30)
        if r.status_code in (403, 429) and "rate limit" in r.text.lower():
            reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset - time.time(), 5) + 1
            print(f"  rate limited, waiting {int(wait)}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        if is_search:
            time.sleep(2.1)  # search API allows ~30 requests/minute
        return r.json()


def find_repos():
    q = f"stars:>{MIN_STARS} good-first-issues:>0 archived:false"
    if LANGUAGE:
        q += f" language:{LANGUAGE}"
    repos, page = [], 1
    while len(repos) < MAX_REPOS:
        data = get(
            "https://api.github.com/search/repositories",
            {"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page},
            is_search=True,
        )
        items = data.get("items", [])
        if not items:
            break
        repos.extend(items)
        page += 1
    return repos[:MAX_REPOS]


def looks_claimed(issue):
    if issue["comments"] == 0:
        return False
    comments = get(issue["comments_url"], {"per_page": 30})
    return any(CLAIM_PATTERNS.search(c.get("body") or "") for c in comments)


def find_issues(repo_full_name):
    q = (
        f"repo:{repo_full_name} is:issue is:open no:assignee -linked:pr "
        f"label:{','.join(LABELS)} comments:<={MAX_COMMENTS}"
    )
    data = get(
        "https://api.github.com/search/issues",
        {"q": q, "sort": "created", "order": "desc", "per_page": ISSUES_PER_REPO},
        is_search=True,
    )
    now = datetime.now(timezone.utc)
    results = []
    for issue in data.get("items", []):
        created = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
        if (now - created).days > MAX_ISSUE_AGE_DAYS:
            continue
        if looks_claimed(issue):
            continue
        results.append(
            {
                "repo": repo_full_name,
                "title": issue["title"],
                "url": issue["html_url"],
                "labels": [l["name"] for l in issue["labels"]],
                "comments": issue["comments"],
                "created_at": issue["created_at"],
            }
        )
    return results


def main():
    print("Searching repos...")
    repos = find_repos()
    print(f"Found {len(repos)} repos. Checking issues...\n")

    all_issues = []
    for i, repo in enumerate(repos, 1):
        name = repo["full_name"]
        print(f"[{i}/{len(repos)}] {name} ({repo['stargazers_count']:,} stars)")
        try:
            issues = find_issues(name)
        except requests.HTTPError as e:
            print(f"  skipped: {e}")
            continue
        for iss in issues:
            iss["stars"] = repo["stargazers_count"]
            iss["language"] = repo.get("language")
        all_issues.extend(issues)
        print(f"  {len(issues)} unclaimed issue(s)")

    all_issues.sort(key=lambda x: x["created_at"], reverse=True)

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(all_issues, f, indent=2, ensure_ascii=False)

    with open("results.md", "w", encoding="utf-8") as f:
        f.write(f"# Unclaimed good first issues ({len(all_issues)})\n\n")
        f.write(f"Updated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n\n")
        f.write("| Repo | Stars | Lang | Issue | Comments | Created |\n")
        f.write("|---|---|---|---|---|---|\n")
        for x in all_issues:
            title = x["title"].replace("|", "\\|")[:80]
            f.write(
                f"| {x['repo']} | {x['stars']:,} | {x['language'] or ''} | "
                f"[{title}]({x['url']}) | {x['comments']} | {x['created_at'][:10]} |\n"
            )

    print(f"\nDone! {len(all_issues)} issues saved to results.md and results.json")


if __name__ == "__main__":
    main()
