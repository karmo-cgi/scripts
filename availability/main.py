#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path

def load_dotenv() -> None:
    env_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".env",
    )

    if not os.path.exists(env_file):
        return

    with open(env_file, encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")

            os.environ.setdefault(key, value)

load_dotenv()

SINCE_DAYS = 30
PR_PAGE_SIZE = 100
ORG_PAGE_SIZE = 100
LATEST_REVIEWS_PAGE_SIZE = 100

USER_WHITELIST_REGEX = os.getenv("USER_WHITELIST_REGEX", "")
USER_PATTERN = re.compile(rf"{USER_WHITELIST_REGEX}", re.IGNORECASE)


def run(cmd: list[str]) -> str:
    """Run a command and return stdout."""
    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}):\n"
            f"  {' '.join(cmd)}\n\n"
            f"{result.stderr.strip()}"
        )

    return result.stdout.strip()


def gh_graphql(query: str) -> dict[str, Any]:
    """Execute a GitHub GraphQL query through gh, retrying transient failures because gh can be flaky."""
    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            output = run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"query={query}",
                ]
            )

            if not output:
                return {}

            data = json.loads(output)

            if "errors" in data:
                raise RuntimeError(
                    "GitHub GraphQL query failed:\n"
                    + json.dumps(data["errors"], indent=2)
                )

            return data["data"]

        except RuntimeError:
            if attempt == max_attempts - 1:
                raise


def get_repo() -> tuple[str, str]:
    """Get owner and repository name from git origin."""
    remote = run(["git", "remote", "get-url", "origin"])
    remote = remote.rstrip("/")

    match = re.match(
        r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$",
        remote,
    )

    if not match:
        match = re.match(
            r"^(?:ssh://git@github\.com/|https?://github\.com/)"
            r"([^/]+)/(.+?)(?:\.git)?$",
            remote,
        )

    if not match:
        print(
            f"Error: origin does not appear to be a GitHub repository:\n"
            f"  {remote}",
            file=sys.stderr,
        )
        sys.exit(1)

    return match.group(1), match.group(2)


def get_recent_prs(
    owner: str,
    repo: str,
) -> list[dict[str, Any]]:
    """Fetch recently updated PRs using GraphQL pagination."""
    since = (
        datetime.now(timezone.utc)
        - timedelta(days=SINCE_DAYS)
    )
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    prs: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        after = "null" if cursor is None else json.dumps(cursor)
        query = f"""
        query {{
          repository(
            owner: {json.dumps(owner)}
            name: {json.dumps(repo)}
          ) {{
            pullRequests(
              first: {PR_PAGE_SIZE}
              after: {after}
              states: [OPEN]
              orderBy: {{
                field: UPDATED_AT
                direction: DESC
              }}
            ) {{
              nodes {{
                number
                state
                updatedAt

                author {{
                  login
                }}

                additions
                deletions

                reviewRequests(first: 100) {{
                  nodes {{
                    requestedReviewer {{
                      __typename

                      ... on User {{
                        login
                      }}

                      ... on Team {{
                        name
                        slug
                      }}
                    }}
                  }}
                }}

                latestReviews(first: {LATEST_REVIEWS_PAGE_SIZE}) {{
                  nodes {{
                    state
                    submittedAt

                    author {{
                      login
                    }}
                  }}
                }}
              }}

              pageInfo {{
                hasNextPage
                endCursor
              }}
            }}
          }}
        }}
        """

        data = gh_graphql(query)
        connection = data["repository"]["pullRequests"]

        page_nodes = [
            pr
            for pr in connection["nodes"]
            if pr is not None
        ]

        stop = False

        for pr in page_nodes:
            updated_at = pr.get("updatedAt")
            if updated_at and updated_at < since_iso:
                stop = True
                break
            prs.append(pr)

        page_info = connection["pageInfo"]
        if stop or not page_info["hasNextPage"]:
            break

        cursor = page_info["endCursor"]

    return prs


def get_open_prs(
    prs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return open PRs from the fetched dataset."""
    return [
        pr
        for pr in prs
        if pr.get("state") == "OPEN"
    ]

BOT_PATTERN = re.compile(
    r"(dependabot|renovate|github-actions|bot$)",
    re.IGNORECASE,
)


def get_users_by_org(usernames: list[str]) -> set[str]:
    """
    Find users belonging to an organization whose login/name contains USER_WHITELIST_REGEX.
    All users are checked in one GraphQL request.
    """
    candidates = [
        username
        for username in usernames
        if not USER_PATTERN.search(username)
    ]

    if not candidates:
        return set()

    selections: list[str] = []
    for index, username in enumerate(candidates):
        selections.append(
            f"""
            user{index}: user(login: {json.dumps(username)}) {{
              login

              organizations(first: {ORG_PAGE_SIZE}) {{
                nodes {{
                  login
                  name
                }}
              }}
            }}
            """
        )

    query = """
    query {
      %s
    }
    """ % "\n".join(selections)

    data = gh_graphql(query)
    matching_users: set[str] = set()

    for value in data.values():
        if not value:
            continue

        organizations = (value.get("organizations", {}).get("nodes", []))
        for org in organizations:
            if not org:
                continue

            if (USER_PATTERN.search(org.get("login", "")) or USER_PATTERN.search(org.get("name", ""))):
                matching_users.add(value["login"])
                break

    return matching_users


def get_users(
    prs: list[dict[str, Any]],
) -> list[str]:
    """Find users matching the USER_WHITELIST_REGEX username/org filter."""
    candidates: set[str] = set()

    for pr in prs:
        # PR author
        author = pr.get("author") or {}
        author_login = author.get("login")
        if author_login and not BOT_PATTERN.search(author_login):
            candidates.add(author_login)

        # PR reviewers
        for review in pr.get("latestReviews", {}).get("nodes", []):
            if not review:
                continue

            reviewer = review.get("author") or {}
            reviewer_login = reviewer.get("login")
            if reviewer_login and not BOT_PATTERN.search(reviewer_login):
                candidates.add(reviewer_login)

    users = {
        username
        for username in candidates
        if USER_PATTERN.search(username)
    }
    org_users = get_users_by_org(list(candidates))
    users.update(org_users)

    return sorted(users, key=str.lower)


def is_requested(
    pr: dict[str, Any],
    user: str,
) -> bool:
    """Whether GitHub currently requests this user's review."""
    for request in pr.get("reviewRequests", {}).get("nodes", []):
        if not request:
            continue

        reviewer = request.get("requestedReviewer") or {}
        if reviewer.get("__typename") == "User":
            if reviewer.get("login") == user:
                return True

    return False


def get_latest_review(
    pr: dict[str, Any],
    user: str,
) -> dict[str, Any] | None:
    """Find this user's latest review from latestReviews."""
    reviews = (
        pr
        .get("latestReviews", {})
        .get("nodes", [])
    )

    latest: dict[str, Any] | None = None
    for review in reviews:
        if not review:
            continue

        author = review.get("author") or {}

        if author.get("login") != user:
            continue
        if latest is None:
            latest = review
            continue

        current_time = review.get("submittedAt") or ""
        latest_time = latest.get("submittedAt") or ""

        if current_time > latest_time:
            latest = review

    return latest


def has_review_relationship(
    pr: dict[str, Any],
    user: str,
) -> bool:
    return (
        is_requested(pr, user)
        or get_latest_review(pr, user) is not None
    )


def needs_review(
    pr: dict[str, Any],
    user: str,
) -> bool:
    if is_requested(pr, user):
        return True

    latest = get_latest_review(pr, user)
    if latest is None:
        return False

    return latest.get("state") not in {
        "APPROVED",
        "CHANGES_REQUESTED",
    }

def reviewer_status(
    prs: list[dict[str, Any]],
    user: str,
) -> tuple[int, int]:
    assigned = 0
    done = 0

    for pr in prs:
        if not has_review_relationship(pr, user):
            continue

        assigned += 1
        if not needs_review(pr, user):
            done += 1

    return done, assigned


def pr_loc(
    pr: dict[str, Any],
) -> int:
    return (pr.get("additions") or 0) + (pr.get("deletions") or 0)


def review_loc_for_user(
    user: str,
    prs: list[dict[str, Any]],
) -> int:
    return sum(
        pr_loc(pr)
        for pr in prs
        if needs_review(pr, user)
    )


def main() -> None:
    owner, repo = get_repo()
    all_prs = get_recent_prs(
        owner,
        repo,
    )

    if not all_prs:
        print("No recently updated PRs found.")
        return

    open_prs = get_open_prs(all_prs)
    users = get_users(all_prs)

    if not users:
        print("No matching users found.")
        return

    rows = []

    for user in users:
        authored = sum(
            1
            for pr in open_prs
            if pr.get("author", {}).get("login") == user
        )

        done, assigned = reviewer_status(
            open_prs,
            user,
        )

        review_loc = review_loc_for_user(
            user,
            open_prs,
        )

        rows.append(
            {
                "user": user,
                "authored": authored,
                "reviewer": f"{done}/{assigned}",
                "review_loc": review_loc,
            }
        )

    rows.sort(
        key=lambda row: (
            -row["review_loc"],
            -int(row["reviewer"].split("/")[1]),
            int(row["reviewer"].split("/")[0]),
            -row["authored"],
            row["user"].lower(),
        )
    )

    print(
        f"{'User':<30} "
        f"{'Open PRs':>10} "
        f"{'Reviewer':>10} "
        f"{'Not reviewed LOC':>12}"
    )

    print("-" * 68)

    for row in rows:
        print(
            f"{row['user']:<30} "
            f"{row['authored']:>10} "
            f"{row['reviewer']:>10} "
            f"{row['review_loc']:>12}"
        )


if __name__ == "__main__":
    main()