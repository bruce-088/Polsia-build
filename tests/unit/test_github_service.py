"""Test github_service — GitHub not configured guards, and the orchestration
logic in open_pr (branch creation -> file update -> PR creation), with
PyGithub's Github class mocked entirely."""
import pytest
from unittest.mock import MagicMock, patch

from app.services.github_service import get_file_content, open_pr


def test_get_file_content_raises_when_not_configured():
    from app.config import settings

    original = (settings.github_token, settings.github_repo)
    settings.github_token, settings.github_repo = "", ""
    try:
        with pytest.raises(RuntimeError):
            get_file_content("README.md")
    finally:
        settings.github_token, settings.github_repo = original


def test_open_pr_raises_when_not_configured():
    from app.config import settings

    original = (settings.github_token, settings.github_repo, settings.sandbox_mode)
    settings.github_token, settings.github_repo = "", ""
    settings.sandbox_mode = False
    try:
        with pytest.raises(RuntimeError):
            open_pr("README.md", "new content", "agent/test", "Test PR", "body")
    finally:
        settings.github_token, settings.github_repo, settings.sandbox_mode = original


def test_open_pr_creates_branch_updates_file_and_opens_pr():
    from app.config import settings

    original = (settings.github_token, settings.github_repo, settings.sandbox_mode)
    settings.github_token, settings.github_repo = "ghp_test", "bruce-088/Polsia-build"
    settings.sandbox_mode = False
    try:
        mock_repo = MagicMock()
        mock_base_ref = MagicMock()
        mock_base_ref.object.sha = "base-sha-123"
        mock_repo.get_git_ref.return_value = mock_base_ref

        mock_existing_file = MagicMock()
        mock_existing_file.sha = "file-sha-456"
        mock_repo.get_contents.return_value = mock_existing_file

        mock_pr = MagicMock()
        mock_pr.html_url = "https://github.com/bruce-088/Polsia-build/pull/1"
        mock_repo.create_pull.return_value = mock_pr

        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo

        with patch("github.Github", return_value=mock_client):
            url = open_pr(
                file_path="README.md",
                new_content="new content",
                branch_name="agent/test-123",
                pr_title="Test PR",
                pr_body="Test body",
            )

        assert url == "https://github.com/bruce-088/Polsia-build/pull/1"
        mock_repo.get_git_ref.assert_called_once_with("heads/main")
        mock_repo.create_git_ref.assert_called_once_with(
            ref="refs/heads/agent/test-123", sha="base-sha-123"
        )
        mock_repo.update_file.assert_called_once_with(
            path="README.md",
            message="Test PR",
            content="new content",
            sha="file-sha-456",
            branch="agent/test-123",
        )
        mock_repo.create_pull.assert_called_once_with(
            title="Test PR", body="Test body", head="agent/test-123", base="main"
        )
    finally:
        settings.github_token, settings.github_repo, settings.sandbox_mode = original
