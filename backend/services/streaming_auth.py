"""User-authorized tokens for streaming platforms (Spotify, SoundCloud).

The Client Credentials token the sync engine uses is app-level and read-only:
it can browse playlists but can never create or modify one. Writing playlists
to Spotify or SoundCloud needs an access token tied to the user's own
account, obtained through the OAuth Authorization Code flow with PKCE:

  1. Soundloom builds an authorize URL (with a one-time code_verifier) and
     the browser is sent to it.
  2. The user logs in on the platform and approves.
  3. The platform redirects back to /api/connections/callback?code=...
  4. Soundloom exchanges the code for access + refresh tokens and stores
     them (settings.json), refreshing automatically when they expire.

Security notes:
  * The code_verifier is kept in-process only, per state value, so a code
    arriving without a matching live handshake cannot be exchanged.
  * Tokens live in the local settings file beside the rest of the user's
    configuration; they are never logged.
"""
import base64
import hashlib
import secrets
import time

import aiohttp

from .. import config

SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SOUNDCLOUD_AUTHORIZE_URL = "https://secure.soundcloud.com/authorize"
SOUNDCLOUD_TOKEN_URL = "https://secure.soundcloud.com/oauth/token"

_HANDSHAKE_TIMEOUT = aiohttp.ClientTimeout(total=30)

# code -> (verifier, platform, created_at); lives only for the redirect round-trip.
_pending: dict[str, dict] = {}
_PENDING_TTL = 600.0


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_pkce_pair() -> tuple[str, str]:
    """A (verifier, challenge) pair per RFC 7636 / OAuth 2.1."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _expiry_from(payload: dict) -> float:
    return time.time() + int(payload.get("expires_in", 3600)) - 60


def _settings_key(platform: str) -> str:
    if platform == "spotify":
        return "spotify_user_token"
    if platform == "soundcloud":
        return "soundcloud_user_token"
    raise ValueError(f"Unknown platform: {platform}")


def _creds(platform: str) -> tuple[str, str, str]:
    """(client_id, client_secret, redirect_uri) from settings."""
    p = platform
    return (
        config.get(f"{p}_client_id", ""),
        config.get(f"{p}_client_secret", ""),
        config.get(f"{p}_redirect_uri", ""),
    )


def configured(platform: str) -> bool:
    client_id, _, redirect_uri = _creds(platform)
    return bool(client_id and redirect_uri)


def connected(platform: str) -> bool:
    tok = config.get(_settings_key(platform), "") or ""
    return isinstance(tok, dict) and bool(tok.get("access") or tok.get("refresh"))


def clear(platform: str) -> None:
    config.update({_settings_key(platform): ""})


def begin_auth(platform: str) -> dict:
    """Build the authorize URL the browser must open. Returns auth info."""
    client_id, _, redirect_uri = _creds(platform)
    if not client_id or not redirect_uri:
        raise ValueError(
            f"{platform.capitalize()} app credentials are missing. Add the client id "
            f"and register the redirect URI shown in Settings."
        )
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)
    _pending[state] = {"verifier": verifier, "platform": platform, "created": time.time()}

    # Drop stale handshakes nobody finished.
    now = time.time()
    for s in [s for s, v in _pending.items() if now - v["created"] > _PENDING_TTL]:
        _pending.pop(s, None)

    if platform == "spotify":
        url = (
            f"{SPOTIFY_AUTHORIZE_URL}?client_id={client_id}"
            f"&response_type=code&redirect_uri={redirect_uri}"
            f"&code_challenge_method=S256&code_challenge={challenge}"
            f"&state={state}"
            "&scope=playlist-modify-public%20playlist-modify-private"
        )
    elif platform == "soundcloud":
        url = (
            f"{SOUNDCLOUD_AUTHORIZE_URL}?client_id={client_id}"
            f"&response_type=code&redirect_uri={redirect_uri}"
            f"&code_challenge_method=S256&code_challenge={challenge}"
            f"&state={state}"
        )
    else:
        raise ValueError(f"Unknown platform: {platform}")
    return {"auth_url": url, "state": state}


def platform_for_state(state: str) -> str:
    """Which platform a pending handshake state belongs to (raises if unknown)."""
    entry = _pending.get(state or "")
    if not entry:
        raise ValueError(
            "No pending authorization handshake for this state. Start the "
            "connection again from the Playlists tab."
        )
    return entry["platform"]


def _pop_verifier(state: str, platform: str) -> str:
    entry = _pending.pop(state or "", None)
    if not entry:
        raise ValueError(
            "No pending authorization handshake for this state. Start the "
            "connection again from the Playlists tab."
        )
    if entry["platform"] != platform:
        raise ValueError("Authorization state does not match the platform.")
    if time.time() - entry["created"] > _PENDING_TTL:
        raise ValueError("Authorization handshake expired; start again.")
    return entry["verifier"]


async def complete_auth(platform: str, code: str, state: str) -> dict:
    """Exchange the redirect's ?code for tokens and store them."""
    verifier = _pop_verifier(state, platform)
    client_id, client_secret, redirect_uri = _creds(platform)
    payload = await _token_request(platform, {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": verifier,
    })
    store = {
        "access": payload.get("access_token", ""),
        "refresh": payload.get("refresh_token", ""),
        "expiry": _expiry_from(payload),
    }
    config.update({_settings_key(platform): store})
    return store


async def _token_request(platform: str, form: dict) -> dict:
    url = SPOTIFY_TOKEN_URL if platform == "spotify" else SOUNDCLOUD_TOKEN_URL
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form, timeout=_HANDSHAKE_TIMEOUT) as resp:
            if resp.status >= 400:
                text = (await resp.text())[:300]
                raise ValueError(f"{platform.capitalize()} token exchange failed ({resp.status}): {text}")
            return await resp.json(content_type=None)


async def get_access_token(platform: str) -> str:
    """A live user access token, refreshing first when needed."""
    raw = config.get(_settings_key(platform), "") or ""
    if not isinstance(raw, dict) or not (raw.get("access") or raw.get("refresh")):
        raise ValueError(
            f"Not connected to {platform}. Connect your account from the Playlists tab first."
        )
    if raw.get("access") and time.time() < float(raw.get("expiry") or 0):
        return raw["access"]
    refresh = raw.get("refresh", "")
    if not refresh:
        raise ValueError(
            f"{platform.capitalize()} session expired without a refresh token. Reconnect."
        )
    payload = await _token_request(platform, {
        "grant_type": "refresh_token",
        "client_id": _creds(platform)[0],
        "client_secret": _creds(platform)[1],
        "refresh_token": refresh,
    })
    store = {
        "access": payload.get("access_token", ""),
        # SoundCloud refresh tokens are single-use; Spotify usually re-sends one.
        "refresh": payload.get("refresh_token", "") or refresh,
        "expiry": _expiry_from(payload),
    }
    config.update({_settings_key(platform): store})
    return store["access"]
