"""Test POST /api/v1/code/open-pr — the sole path that ever opens a real
PR, always explicit/human-triggered, never called by an agent."""
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_open_pr_requires_auth(api_client):
    resp = await api_client.post(
        "/api/v1/code/open-pr",
        json={"file_path": "README.md", "new_content": "x", "pr_title": "Test"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_open_pr_not_configured(api_client, auth_headers):
    with patch(
        "app.api.v1.code.open_pr", side_effect=RuntimeError("GitHub is not configured")
    ):
        resp = await api_client.post(
            "/api/v1/code/open-pr",
            json={"file_path": "README.md", "new_content": "x", "pr_title": "Test"},
            headers=auth_headers,
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_open_pr_success(api_client, auth_headers):
    with patch(
        "app.api.v1.code.open_pr", return_value="https://github.com/bruce-088/Polsia-build/pull/1"
    ) as mock_open:
        resp = await api_client.post(
            "/api/v1/code/open-pr",
            json={"file_path": "README.md", "new_content": "x", "pr_title": "Test", "pr_body": "body"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "opened"
    assert data["pr_url"] == "https://github.com/bruce-088/Polsia-build/pull/1"
    assert mock_open.call_count == 1
    assert mock_open.call_args.kwargs["file_path"] == "README.md"
    assert mock_open.call_args.kwargs["pr_title"] == "Test"


@pytest.mark.asyncio
async def test_open_pr_github_error_returns_502(api_client, auth_headers):
    with patch("app.api.v1.code.open_pr", side_effect=Exception("GitHub 5xx")):
        resp = await api_client.post(
            "/api/v1/code/open-pr",
            json={"file_path": "README.md", "new_content": "x", "pr_title": "Test"},
            headers=auth_headers,
        )
    assert resp.status_code == 502
