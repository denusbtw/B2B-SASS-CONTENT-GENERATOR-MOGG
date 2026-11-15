"""YouTube-only OAuth, encrypted credentials, browser-bound single-use states."""

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import time
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, insert, select, update

from factory.db import now, uid
from factory.youtube_db import connections, oauth_states

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(Exception):
    def __init__(
        self, code, *, retry=False, auth=False, ambiguous=False, retry_after=0
    ):
        super().__init__(code)
        self.code, self.retry, self.auth, self.ambiguous = code, retry, auth, ambiguous
        self.retry_after = retry_after


def live_enabled():
    return (
        os.getenv("PUBLISHER_PROVIDER", "mock") == "youtube"
        and os.getenv("YOUTUBE_ENABLED", "false").lower() == "true"
    )


def cipher():
    try:
        key = os.getenv("YOUTUBE_TOKEN_ENCRYPTION_KEY", "")
        if not key and os.getenv("YOUTUBE_TOKEN_ENCRYPTION_KEY_FILE"):
            key = (
                Path(os.environ["YOUTUBE_TOKEN_ENCRYPTION_KEY_FILE"])
                .read_text()
                .strip()
            )
        return Fernet(key.encode())
    except (ValueError, OSError):
        raise YouTubeError("youtube_encryption_key_missing_or_invalid") from None


def encrypt(data):
    return cipher().encrypt(json.dumps(data).encode()).decode()


def decrypt(value):
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError):
        raise YouTubeError("youtube_secret_decryption_failed", auth=True) from None


@dataclass(frozen=True, repr=False)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect: str

    @classmethod
    def load(cls):
        try:
            data = {}
            if os.getenv("YOUTUBE_CLIENT_FILE"):
                data = json.loads(Path(os.environ["YOUTUBE_CLIENT_FILE"]).read_text())[
                    "web"
                ]
            client = cls(
                os.getenv("YOUTUBE_CLIENT_ID") or data.get("client_id", ""),
                os.getenv("YOUTUBE_CLIENT_SECRET") or data.get("client_secret", ""),
                os.getenv(
                    "YOUTUBE_REDIRECT_URI",
                    "http://127.0.0.1:8080/api/v1/integrations/youtube/callback",
                ),
            )
            url = urlsplit(client.redirect)
            if (
                not client.client_id
                or not client.client_secret
                or url.query
                or url.fragment
                or url.username
                or (
                    url.scheme != "https"
                    and not (
                        url.scheme == "http"
                        and url.hostname in {"127.0.0.1", "localhost"}
                    )
                )
            ):
                raise ValueError
            cipher()
            timeout = float(os.getenv("YOUTUBE_REQUEST_TIMEOUT", "35"))
            attempts = int(os.getenv("YOUTUBE_MAX_ATTEMPTS", "8"))
            poll = int(os.getenv("YOUTUBE_STATUS_POLL_SECONDS", "300"))
            chunk = int(os.getenv("YOUTUBE_UPLOAD_CHUNK_BYTES", str(8 * 1024**2)))
            if (
                not 1 <= timeout <= 60
                or not 1 <= attempts <= 50
                or not 30 <= poll <= 86400
                or not 0 < chunk <= 64 * 1024**2
                or chunk % (256 * 1024)
            ):
                raise ValueError
            return client
        except (OSError, KeyError, ValueError, TypeError):
            raise YouTubeError("youtube_oauth_configuration_invalid") from None

    def exchange(self, http, fields):
        try:
            response = http.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    **fields,
                },
            )
        except httpx.RequestError:
            raise YouTubeError("youtube_token_network_error", retry=True) from None
        if response.status_code != 200:
            raise YouTubeError(
                "youtube_auth_required"
                if response.status_code in {400, 401, 403}
                else "youtube_token_service_unavailable",
                auth=response.status_code in {400, 401, 403},
                retry=response.status_code in {429, 500, 502, 503, 504},
            )
        try:
            data = response.json()
            if not data.get("access_token") or int(data.get("expires_in", 0)) <= 0:
                raise ValueError
            if not set(SCOPES).issubset(
                set(data.get("scope", " ".join(SCOPES)).split())
            ):
                raise YouTubeError("youtube_required_scope_missing", auth=True)
            data["expires_at"] = time() + int(data["expires_in"]) - 60
            return data
        except (ValueError, KeyError, TypeError):
            raise YouTubeError("youtube_invalid_token_response", auth=True) from None


def begin(ledger, browser):
    config = OAuthConfig.load()
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    payload = {
        "verifier": verifier,
        "browser": hashlib.sha256(browser.encode()).hexdigest(),
    }
    with ledger.write() as db:
        db.execute(
            delete(oauth_states).where(oauth_states.c.expires < now().isoformat())
        )
        db.execute(
            insert(oauth_states).values(
                id=hashlib.sha256(state.encode()).hexdigest(),
                payload=encrypt(payload),
                expires=(now() + timedelta(minutes=10)).isoformat(),
            )
        )
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.redirect,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            )
            .decode()
            .rstrip("="),
        }
    )


def consume(ledger, state, browser):
    key = hashlib.sha256(state.encode()).hexdigest()
    with ledger.write() as db:
        row = (
            db.execute(
                ledger.lock(select(oauth_states).where(oauth_states.c.id == key))
            )
            .mappings()
            .first()
        )
        if not row or row.expires < now().isoformat():
            raise YouTubeError("youtube_oauth_state_invalid")
        payload = decrypt(row.payload)
        if not browser or not secrets.compare_digest(
            payload["browser"], hashlib.sha256(browser.encode()).hexdigest()
        ):
            raise YouTubeError("youtube_oauth_browser_mismatch")
        db.execute(delete(oauth_states).where(oauth_states.c.id == key))
    return payload["verifier"]


def connection(ledger, *, confirmed=True):
    rows = ledger.list_rows(connections, connections.c.id == "youtube")
    if not rows or (confirmed and rows[0]["state"] != "CONNECTED"):
        raise YouTubeError(
            "youtube_channel_confirmation_or_reconnect_required", auth=True
        )
    return rows[0]


class AccessTokens:
    def __init__(self, ledger, http, revision=None):
        self.ledger, self.http, self.revision = ledger, http, revision

    def get(self, *, refresh=False):
        row = connection(self.ledger)
        if self.revision and self.revision != row["revision"]:
            raise YouTubeError(
                "youtube_connection_changed_reapproval_required", auth=True
            )
        data = decrypt(row["credentials"])
        if not data.get("refresh_token"):
            raise YouTubeError("youtube_offline_consent_required", auth=True)
        if refresh or data.get("expires_at", 0) <= time():
            try:
                renewed = OAuthConfig.load().exchange(
                    self.http,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": data["refresh_token"],
                    },
                )
            except YouTubeError as exc:
                if exc.auth:
                    with self.ledger.write() as db:
                        db.execute(
                            update(connections)
                            .where(
                                connections.c.id == "youtube",
                                connections.c.revision == row["revision"],
                            )
                            .values(state="AUTH_REQUIRED", updated=now().isoformat())
                        )
                raise
            data.update(renewed)
            with self.ledger.write() as db:
                changed = db.execute(
                    update(connections)
                    .where(
                        connections.c.id == "youtube",
                        connections.c.revision == row["revision"],
                        connections.c.state == "CONNECTED",
                    )
                    .values(credentials=encrypt(data), updated=now().isoformat())
                ).rowcount
                if not changed:
                    raise YouTubeError("youtube_connection_changed", auth=True)
        if not data.get("access_token"):
            raise YouTubeError("youtube_auth_required", auth=True)
        return data["access_token"]


def finish_connection(ledger, http, code, state, browser):
    verifier = consume(ledger, state, browser)
    config = OAuthConfig.load()
    data = config.exchange(
        http,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect,
            "code_verifier": verifier,
        },
    )
    if not data.get("refresh_token"):
        raise YouTubeError("youtube_offline_consent_required", auth=True)
    try:
        response = http.get(
            API + "/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": "Bearer " + data["access_token"]},
        )
    except httpx.RequestError:
        raise YouTubeError("youtube_channel_lookup_unavailable", retry=True) from None
    if response.status_code != 200:
        raise YouTubeError("youtube_channel_lookup_denied", auth=True)
    items = response.json().get("items", [])
    if len(items) != 1 or not items[0].get("id"):
        raise YouTubeError("youtube_single_channel_required")
    item = items[0]
    with ledger.write() as db:
        # Reconnection is explicit and invalidates previous approval revisions.
        db.execute(delete(connections).where(connections.c.id == "youtube"))
        db.execute(
            insert(connections).values(
                id="youtube",
                channel_id=item["id"],
                channel_title=item["snippet"]["title"],
                revision=uid(),
                state="CONFIRM_CHANNEL",
                credentials=encrypt(data),
                updated=now().isoformat(),
            )
        )
    return {
        "channel_id": item["id"],
        "channel_title": item["snippet"]["title"],
        "state": "CONFIRM_CHANNEL",
    }


def revoke(ledger, http):
    row = connection(ledger, confirmed=False)
    # Disable local use before calling the external revocation endpoint.
    with ledger.write() as db:
        db.execute(
            update(connections)
            .where(
                connections.c.id == "youtube", connections.c.revision == row["revision"]
            )
            .values(state="DISCONNECTED", updated=now().isoformat())
        )
    data = decrypt(row["credentials"])
    try:
        response = http.post(
            "https://oauth2.googleapis.com/revoke",
            data={"token": data["refresh_token"]},
        )
    except httpx.RequestError:
        raise YouTubeError(
            "youtube_revocation_pending_retry_disconnect", retry=True
        ) from None
    if response.status_code not in {200, 400}:
        raise YouTubeError("youtube_revocation_pending_retry_disconnect", retry=True)
    with ledger.write() as db:
        db.execute(
            delete(connections).where(
                connections.c.id == "youtube", connections.c.revision == row["revision"]
            )
        )
