"""
Smart contribution finder
=========================
Scans active GitHub repos with 10k-50k stars and finds small changes that a
maintainer is likely to accept:

  1. Open, unclaimed issues opened in the last month, labeled good first issue /
     docs / typo / help wanted. Good first issues are always listed first.
  2. Typos in the README (found with codespell) - ready-made small PRs

Every repo is scored by how friendly it is to outside contributors:
how many recent PRs from non-members got merged, and how fast.

Usage:
  pip install requests codespell
  export GH_TOKEN=github_pat_xxx
  python -u find_contributions.py
"""

import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests

# ======================= settings you can change =======================
MIN_STARS = 10_000
MAX_STARS = 70_000
MAX_REPOS = 600  # repos scanned per run (random sample -> different every day)
ACTIVE_DAYS = 30  # only repos with a push in the last N days
MAX_ISSUE_AGE_DAYS = 30  # only issues opened in the last N days
MAX_COMMENTS = 6  # issues with many comments are usually taken or complicated
MIN_MERGE_RATE = 0.4  # skip repos that merge fewer than 40% of outside PRs
TOP_ISSUES = 100  # how many issues to show in results
TOP_TYPOS = 60  # how many typo-fix repos to show in results
BATCH = 4  # repos per GraphQL request
# =======================================================================

# Issues with any of these labels are fetched (GitHub matches ANY of them).
LABEL_FILTER = [
    "documentation",
    "Documentation",
    "docs",
    "type: docs",
    "type: documentation",
    "kind/documentation",
    "area/docs",
    "typo",
    "good first issue",
    "Good First Issue",
    "good-first-issue",
    "Good first issue",
    "good first issue :+1:",
    "first-timers-only",
    "beginner",
    "starter",
    "easy",
    "help wanted",
    "Help Wanted",
    "status: help wanted",
]

TITLE_BONUS = re.compile(
    r"\b(typos?|spelling|readme|docs?|documentation|broken links?|dead links?|"
    r"examples?|grammar|wording|translation|outdated)\b",
    re.I,
)

# A comment (or the issue body) saying someone is taking or already took the issue.
CLAIM = re.compile(
    r"(i('d| would) (like|love|want) to (work|take|help|contribute|tackle|fix|try)|"
    r"i want to (work|take|fix|tackle|try)|happy to (work|submit|open|send|fix)|"
    r"(can|could|may) i (work|take|pick|try|tackle|fix|get|be assigned)|"
    r"assign (it|this|me)|please assign|"
    r"i('ll| will) (work|take|pick|submit|open|send|create|fix|tackle|try)|"
    r"i can (work|take|pick|fix|tackle)|let me (work|take|try|fix|handle)|"
    r"working on (it|this)|i('m| am) (on it|working|interested|taking)|"
    r"i('ve| have) (opened|submitted|created|raised|sent) a (pr|pull request)|"
    r"is (this|it|anyone|someone) (still )?(available|open|taken|working)|"
    r"^\s*/(assign|take|claim)\b)",
    re.I | re.M,
)

# Labels meaning someone is already on it.
TAKEN_LABEL = re.compile(
    r"in[ -]?progress|\bassigned|claimed|\bwip\b|has[ -]?pr|pr[ -]?(open|exists|submitted)|"
    r"\btaken\b|work[ -]in[ -]progress",
    re.I,
)

GOOD_FIRST = re.compile(
    r"good[ -]?first|first[ -]?timers?|beginner|starter|newcomer|\beasy\b", re.I
)

NO_TRIVIAL = re.compile(
    r"(typo|trivial|cosmetic|spelling|whitespace)[^.\n]{0,120}\b(not|won't|will not|don't|do not)\b"
    r"[^.\n]{0,40}(accept|merge|consider)|"
    r"\b(not|won't|will not|don't|do not)\b[^.\n]{0,40}(accept|merge)[^.\n]{0,120}"
    r"(typo|trivial|cosmetic)",
    re.I,
)

AI_POLICY = re.compile(
    r"\b(AI[- ]generated|LLMs?|ChatGPT|Copilot|generative AI)\b", re.I
)

EXTERNAL = {"CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"}

API = "https://api.github.com/graphql"
NOW = datetime.now(timezone.utc)

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("Set the GH_TOKEN environment variable first.")

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}"})


# ----------------------------------------------------------------------
# GitHub GraphQL helper with rate-limit handling
# ----------------------------------------------------------------------
def gql(query, variables=None):
    for attempt in range(6):
        try:
            r = session.post(
                API, json={"query": query, "variables": variables or {}}, timeout=90
            )
        except requests.RequestException as e:
            print(f"  network error ({e}), retrying in 15s")
            time.sleep(15)
            continue

        if r.status_code in (403, 429):
            wait = int(r.headers.get("Retry-After", 60))
            print(f"  rate limited, waiting {wait}s...")
            time.sleep(wait + 1)
            continue
        if r.status_code >= 500:
            print(f"  GitHub error {r.status_code}, retrying in 10s")
            time.sleep(10)
            continue
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}: {r.text[:200]}")
            return {}, [{"message": r.text}]

        body = r.json()
        errors = body.get("errors") or []
        if any(e.get("type") == "RATE_LIMITED" for e in errors):
            print("  rate limited, waiting 60s...")
            time.sleep(60)
            continue

        data = body.get("data") or {}
        rl = data.get("rateLimit")
        if rl and rl.get("remaining", 9999) < 100:
            reset = parse_time(rl["resetAt"])
            wait = max((reset - datetime.now(timezone.utc)).total_seconds(), 0) + 5
            print(f"  hourly limit almost used, waiting {int(wait)}s...")
            time.sleep(wait)

        time.sleep(1)  # be gentle, avoids secondary rate limits
        return data, errors
    return {}, [{"message": "gave up after retries"}]


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ----------------------------------------------------------------------
# Step 1: pick active repos in the star range
# ----------------------------------------------------------------------
REPO_SEARCH = """
query($q: String!) {
  rateLimit { remaining resetAt }
  search(query: $q, type: REPOSITORY, first: 100) {
    nodes { ... on Repository { nameWithOwner } }
  }
}
"""


def star_slices():
    # GitHub search returns max 1000 results per query, so split by stars.
    slices, s = [], MIN_STARS
    while s < MAX_STARS:
        width = 500 if s < 20_000 else 1000 if s < 30_000 else 2500
        e = min(s + width - 1, MAX_STARS)
        slices.append((s, e))
        s = e + 1
    return slices


def find_repos():
    since = (NOW - timedelta(days=ACTIVE_DAYS)).strftime("%Y-%m-%d")
    slices = star_slices()
    random.shuffle(slices)
    per_slice = MAX_REPOS // len(slices) + 1
    repos = []
    for lo, hi in slices:
        q = f"stars:{lo}..{hi} pushed:>{since} archived:false fork:false"
        data, _ = gql(REPO_SEARCH, {"q": q})
        nodes = [
            n["nameWithOwner"]
            for n in (data.get("search") or {}).get("nodes", [])
            if n and n.get("nameWithOwner")
        ]
        repos.extend(random.sample(nodes, min(per_slice, len(nodes))))
        if len(repos) >= MAX_REPOS:
            break
    return repos[:MAX_REPOS]


# ----------------------------------------------------------------------
# Step 2: fetch details for each repo (PR history, issues, README, CONTRIBUTING)
# ----------------------------------------------------------------------
FRAGMENT = """
fragment R on Repository {
  nameWithOwner
  stargazerCount
  url
  primaryLanguage { name }
  readme: object(expression: "HEAD:README.md") { ... on Blob { text } }
  contributing: object(expression: "HEAD:CONTRIBUTING.md") { ... on Blob { text } }
  contributing2: object(expression: "HEAD:.github/CONTRIBUTING.md") { ... on Blob { text } }
  pullRequests(first: 30, states: [MERGED, CLOSED],
               orderBy: {field: UPDATED_AT, direction: DESC}) {
    nodes { merged createdAt mergedAt authorAssociation }
  }
  issues(first: 25, states: OPEN, labels: $labels,
         orderBy: {field: CREATED_AT, direction: DESC}) {
    nodes {
      title
      url
      createdAt
      body
      assignees { totalCount }
      linkedBranches(first: 1) { totalCount }
      closedByPullRequestsReferences(first: 1) { totalCount }
      labels(first: 10) { nodes { name } }
      comments(last: 10) { totalCount nodes { body } }
      timelineItems(last: 10, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source { __typename ... on PullRequest { state } }
          }
        }
      }
    }
  }
}
"""


def detail_query(names):
    parts = []
    for i, full in enumerate(names):
        owner, name = full.split("/", 1)
        parts.append(
            f"  r{i}: repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ ...R }}"
        )
    return (
        "query($labels: [String!]) {\n  rateLimit { remaining resetAt }\n"
        + "\n".join(parts)
        + "\n}\n"
        + FRAGMENT
    )


def fetch_details(names):
    data, errors = gql(detail_query(names), {"labels": LABEL_FILTER})
    repos = [data.get(f"r{i}") for i in range(len(names))]
    if not any(repos) and len(names) > 1:
        # Batch failed (e.g. too heavy) -> try one by one
        repos = []
        for n in names:
            d, _ = gql(detail_query([n]), {"labels": LABEL_FILTER})
            repos.append(d.get("r0"))
    return [r for r in repos if r]


# ----------------------------------------------------------------------
# Step 3: analysis
# ----------------------------------------------------------------------
def friendliness(repo):
    """How likely an outside contributor's PR gets merged here."""
    prs = (repo.get("pullRequests") or {}).get("nodes") or []
    ext = [p for p in prs if p.get("authorAssociation") in EXTERNAL]
    if len(ext) < 3:
        return None
    merged = [p for p in ext if p.get("merged") and p.get("mergedAt")]
    rate = len(merged) / len(ext)
    days = [
        (parse_time(p["mergedAt"]) - parse_time(p["createdAt"])).total_seconds() / 86400
        for p in merged
    ]
    median_days = statistics.median(days) if days else None
    last_merge = max((parse_time(p["mergedAt"]) for p in merged), default=None)

    score = rate * 35
    if median_days is not None:
        score += (
            15
            if median_days <= 3
            else 10 if median_days <= 10 else 5 if median_days <= 30 else 0
        )
    if last_merge and (NOW - last_merge).days > 60:
        score -= 20  # nobody merged outside PRs recently
    return {
        "rate": rate,
        "median_days": median_days,
        "score": score,
        "recent": bool(last_merge and (NOW - last_merge).days <= 60),
    }


def label_score(names):
    best = 0
    for n in (x.lower() for x in names):
        if GOOD_FIRST.search(n):
            best = max(best, 40)
        elif "typo" in n:
            best = max(best, 25)
        elif "doc" in n:
            best = max(best, 20)
        elif "help wanted" in n or "help-wanted" in n:
            best = max(best, 12)
    return best


def is_taken(issue, labels):
    """True if anyone is assigned, linked a branch/PR, or said they're on it."""
    for field in ("assignees", "linkedBranches", "closedByPullRequestsReferences"):
        if (issue.get(field) or {}).get("totalCount", 0) > 0:
            return True
    if any(TAKEN_LABEL.search(l) for l in labels):
        return True
    for ev in (issue.get("timelineItems") or {}).get("nodes") or []:
        if not ev:
            continue
        if ev.get("__typename") == "ConnectedEvent":
            return True
        source = ev.get("source") or {}
        # A closed (unmerged) PR means the attempt was abandoned -> free again
        if source.get("__typename") == "PullRequest" and source.get("state") != "CLOSED":
            return True
    texts = [issue.get("body") or ""] + [
        c.get("body") or "" for c in (issue.get("comments") or {}).get("nodes") or [] if c
    ]
    return any(CLAIM.search(t) for t in texts)


def good_issues(repo, friend):
    out = []
    for issue in (repo.get("issues") or {}).get("nodes") or []:
        if not issue:
            continue
        age = (NOW - parse_time(issue["createdAt"])).days
        n_comments = (issue.get("comments") or {}).get("totalCount", 0)
        if age > MAX_ISSUE_AGE_DAYS or n_comments > MAX_COMMENTS:
            continue
        labels = [
            l["name"] for l in (issue.get("labels") or {}).get("nodes") or [] if l
        ]
        if is_taken(issue, labels):
            continue
        good_first = any(GOOD_FIRST.search(l) for l in labels)
        why = []
        score = label_score(labels) + friend["score"]
        if TITLE_BONUS.search(issue["title"]):
            score += 10
            why.append("docs/typo-type task")
        if age <= 7:
            score += 10
            why.append("this week")
        elif age <= 14:
            score += 5
        if n_comments == 0:
            score += 3
        why.insert(0, ", ".join(labels[:3]))
        out.append(
            {
                "score": round(score),
                "good_first_issue": good_first,
                "title": issue["title"],
                "url": issue["url"],
                "age_days": age,
                "comments": n_comments,
                "why": why,
            }
        )
    return out


def find_typos(text):
    if not text or len(text) > 300_000 or not shutil.which("codespell"):
        return []
    lines = text.splitlines()
    in_code, code_lines = False, set()
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_code = not in_code
            code_lines.add(i)
        elif in_code or line.startswith("    ") or line.startswith("\t"):
            code_lines.add(i)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        path = f.name
    try:
        out = subprocess.run(
            ["codespell", "--quiet-level", "3", path],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except Exception:
        return []
    finally:
        os.unlink(path)

    typos = []
    for m in re.finditer(r":(\d+): (\S+) ==> (.+)", out):
        ln, word, fix = int(m.group(1)), m.group(2), m.group(3).strip()
        if ln in code_lines or ln > len(lines):
            continue
        line = lines[ln - 1]
        if re.search(r"`[^`]*" + re.escape(word) + r"[^`]*`", line):
            continue  # inside inline code
        if re.search(r"\S*" + re.escape(word) + r"\S*\.(com|org|io|net|dev)", line):
            continue  # part of a URL
        typos.append({"line": ln, "word": word, "fix": fix})
    # Many hits usually means false positives (non-English README, jargon)
    return typos if len(typos) <= 8 else []


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def main():
    if not shutil.which("codespell"):
        print("Note: codespell not installed -> README typo scan is skipped.")

    print("Finding active repos...")
    names = find_repos()
    print(f"Scanning {len(names)} repos with {MIN_STARS:,}-{MAX_STARS:,} stars\n")

    issues, typo_fixes = [], []
    for start in range(0, len(names), BATCH):
        batch = names[start : start + BATCH]
        print(f"[{start + len(batch)}/{len(names)}] {', '.join(batch)}")
        for repo in fetch_details(batch):
            friend = friendliness(repo)
            if not friend or friend["rate"] < MIN_MERGE_RATE or not friend["recent"]:
                continue

            contrib = (
                (repo.get("contributing") or {}).get("text")
                or (repo.get("contributing2") or {}).get("text")
                or ""
            )
            warnings = []
            no_trivial = bool(NO_TRIVIAL.search(contrib))
            if no_trivial:
                warnings.append("no typo-only PRs")
            if AI_POLICY.search(contrib):
                warnings.append("has AI policy - read CONTRIBUTING")

            base = {
                "repo": repo["nameWithOwner"],
                "stars": repo["stargazerCount"],
                "language": (repo.get("primaryLanguage") or {}).get("name") or "",
                "merge_rate": round(friend["rate"] * 100),
                "median_merge_days": (
                    round(friend["median_days"], 1)
                    if friend["median_days"] is not None
                    else None
                ),
                "warnings": warnings,
            }

            for iss in good_issues(repo, friend):
                issues.append({**base, **iss})

            if not no_trivial:
                typos = find_typos((repo.get("readme") or {}).get("text"))
                if typos:
                    typo_fixes.append(
                        {**base, "score": round(friend["score"]), "typos": typos}
                    )

    issues.sort(key=lambda x: (x["good_first_issue"], x["score"]), reverse=True)
    typo_fixes.sort(key=lambda x: x["score"], reverse=True)
    issues, typo_fixes = issues[:TOP_ISSUES], typo_fixes[:TOP_TYPOS]

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(
            {"issues": issues, "readme_typos": typo_fixes},
            f,
            indent=2,
            ensure_ascii=False,
        )

    def merge_info(x):
        d = x["median_merge_days"]
        return f"{x['merge_rate']}% merged" + (f", ~{d}d" if d is not None else "")

    with open("results.md", "w", encoding="utf-8") as f:
        f.write("# Contributions likely to get merged\n\n")
        f.write(
            f"Updated {NOW:%Y-%m-%d %H:%M} UTC · scanned {len(names)} repos "
            f"({MIN_STARS:,}-{MAX_STARS:,} stars)\n\n"
        )
        f.write(
            "**Merge info** = % of recent PRs from outside contributors that got merged, "
            "and the typical days until merge.\n\n"
        )

        f.write(
            f"## Unclaimed issues from the last {MAX_ISSUE_AGE_DAYS} days ({len(issues)})\n\n"
        )
        f.write("⭐ = good first issue (always listed first)\n\n")
        f.write(
            "| Score | Repo | Stars | Lang | Merge info | Issue | Why | Warnings |\n"
        )
        f.write("|---|---|---|---|---|---|---|---|\n")
        for x in issues:
            f.write(
                f"| {x['score']} | {x['repo']} | {x['stars']:,} | {esc(x['language'])} | "
                f"{merge_info(x)} | {'⭐ ' if x['good_first_issue'] else ''}"
                f"[{esc(x['title'][:70])}]({x['url']}) | "
                f"{esc('; '.join(x['why']))} | {esc(', '.join(x['warnings']))} |\n"
            )

        f.write(f"\n## README typo fixes ({len(typo_fixes)})\n\n")
        f.write(
            "Before opening a PR, check the repo's open PRs to make sure nobody "
            "already fixed it.\n\n"
        )
        f.write("| Score | Repo | Stars | Merge info | Typos found | Warnings |\n")
        f.write("|---|---|---|---|---|---|\n")
        for x in typo_fixes:
            links = ", ".join(
                f"[{esc(t['word'])} → {esc(t['fix'])}]"
                f"(https://github.com/{x['repo']}/blob/HEAD/README.md?plain=1#L{t['line']})"
                for t in x["typos"]
            )
            f.write(
                f"| {x['score']} | {x['repo']} | {x['stars']:,} | {merge_info(x)} | "
                f"{links} | {esc(', '.join(x['warnings']))} |\n"
            )

    print(
        f"\nDone! {len(issues)} issues and {len(typo_fixes)} README typo fixes "
        f"saved to results.md"
    )


if __name__ == "__main__":
    main()
