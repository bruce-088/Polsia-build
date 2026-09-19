"""Test POST /api/v1/social/posts/publish — the sole path that ever posts a
real tweet, always explicit/human-triggered, never called by an agent."""
import pytest
from sqlalchemy import select
from unittest.mock import patch


@pytest.mark.asyncio
async def test_publish_post_requires_auth(api_client):
    resp = await api_client.post("/api/v1/social/posts/publish", json={"content": "Hello"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_publish_post_not_configured(api_client, auth_headers):
    """No Twitter credentials set — fails loudly, not silently."""
    from app.config import settings

    originals = (
        settings.twitter_api_key,
        settings.twitter_api_secret,
        settings.twitter_access_token,
        settings.twitter_access_token_secret,
    )
    settings.twitter_api_key = ""
    settings.twitter_api_secret = ""
    settings.twitter_access_token = ""
    settings.twitter_access_token_secret = ""
    try:
        resp = await api_client.post(
            "/api/v1/social/posts/publish", json={"content": "Hello"}, headers=auth_headers
        )
        assert resp.status_code == 400
    finally:
        (
            settings.twitter_api_key,
            settings.twitter_api_secret,
            settings.twitter_access_token,
            settings.twitter_access_token_secret,
        ) = originals


@pytest.mark.asyncio
async def test_publish_post_success(api_client, auth_headers, async_db_session):
    from app.models.social import SocialPost

    with patch("app.api.v1.social.post_tweet", return_value="1234567890") as mock_post:
        resp = await api_client.post(
            "/api/v1/social/posts/publish", json={"content": "Hello world!"}, headers=auth_headers
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["tweet_id"] == "1234567890"
    mock_post.assert_called_once_with("Hello world!")

    result = await async_db_session.execute(select(SocialPost))
    posts = result.scalars().all()
    assert len(posts) == 1
    assert posts[0].tweet_id == "1234567890"
    assert posts[0].status == "published"


@pytest.mark.asyncio
async def test_publish_post_twitter_error(api_client, auth_headers):
    with patch("app.api.v1.social.post_tweet", side_effect=Exception("Twitter 5xx")):
        resp = await api_client.post(
            "/api/v1/social/posts/publish", json={"content": "Hello"}, headers=auth_headers
        )
    assert resp.status_code == 502
