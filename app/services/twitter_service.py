"""Twitter/X posting via tweepy. Deliberately has no agent-triggered
caller — the only path that reaches this is app/api/v1/social.py's
POST /posts/publish, a separate, explicitly human-triggered action. The
social_media agent only ever produces drafts."""
from app.config import settings


def post_tweet(content: str) -> str:
    """Post a real tweet via the Twitter/X API (OAuth 1.0a user context).
    Raises RuntimeError if Twitter isn't configured, so a misconfigured
    publish fails loudly instead of silently no-op-ing. Returns the real
    tweet id on success."""
    if not all([
        settings.twitter_api_key,
        settings.twitter_api_secret,
        settings.twitter_access_token,
        settings.twitter_access_token_secret,
    ]):
        raise RuntimeError(
            "Twitter/X is not configured (twitter_api_key/twitter_api_secret/"
            "twitter_access_token/twitter_access_token_secret)"
        )

    import tweepy

    client = tweepy.Client(
        consumer_key=settings.twitter_api_key,
        consumer_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_token_secret=settings.twitter_access_token_secret,
    )
    response = client.create_tweet(text=content)
    return str(response.data["id"])
