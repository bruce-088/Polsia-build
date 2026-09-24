"""GitHub PR creation via PyGithub. Deliberately narrow: single-file
content changes only via the Contents API (no local git checkout, no
multi-file diffs), and has no agent-triggered caller — the only path that
reaches open_pr() is app/api/v1/code.py's POST /open-pr, always an
explicit, human-triggered action. A PR is never merged automatically;
GitHub's own review is the approval gate, same role a human clicking
/emails/send plays for email."""
from app.config import settings
from app.services.external_access import require_production_write_allowed


def _get_repo():
    if not settings.github_token or not settings.github_repo:
        raise RuntimeError("GitHub is not configured (github_token/github_repo)")

    from github import Github

    client = Github(settings.github_token)
    return client.get_repo(settings.github_repo)


def get_file_content(path: str, ref: str = "main") -> str:
    """Fetch a file's real current content from the target repo, so a
    proposed change can be grounded in what's actually there instead of
    guessed. Raises RuntimeError if GitHub isn't configured."""
    repo = _get_repo()
    content_file = repo.get_contents(path, ref=ref)
    return content_file.decoded_content.decode("utf-8")


def open_pr(
    file_path: str,
    new_content: str,
    branch_name: str,
    pr_title: str,
    pr_body: str,
    base_branch: str = "main",
) -> str:
    """Create a branch off base_branch, update exactly one file on it, and
    open a real PR back to base_branch. Raises RuntimeError if GitHub
    isn't configured. Returns the PR's real URL."""
    require_production_write_allowed("github.open_pr")
    repo = _get_repo()

    base_ref = repo.get_git_ref(f"heads/{base_branch}")
    repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_ref.object.sha)

    existing = repo.get_contents(file_path, ref=branch_name)
    repo.update_file(
        path=file_path,
        message=pr_title,
        content=new_content,
        sha=existing.sha,
        branch=branch_name,
    )

    pr = repo.create_pull(title=pr_title, body=pr_body, head=branch_name, base=base_branch)
    return pr.html_url
