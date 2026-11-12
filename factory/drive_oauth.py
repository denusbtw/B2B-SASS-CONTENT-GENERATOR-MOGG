"""One-time desktop consent and in-memory access tokens for Google Drive only."""

import argparse
import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class DriveError(Exception):
    def __init__(self, code: str, *, retry: bool = False):
        super().__init__(code)
        self.code = code
        self.retry = retry


@dataclass(frozen=True, repr=False)
class OAuthClient:
    client_id: str
    client_secret: str

    @classmethod
    def read(cls, path: Path):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))["installed"]
            client = cls(config["client_id"], config["client_secret"])
            if not all(
                isinstance(value, str) and value
                for value in (client.client_id, client.client_secret)
            ):
                raise ValueError
            return client
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DriveError("desktop_client_config_invalid") from exc

    def authorization_url(self, redirect: str, state: str, verifier: str) -> str:
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent select_account",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return AUTHORIZE_URL + "?" + urlencode(params)

    def exchange(self, http: httpx.Client, fields: dict[str, str]) -> dict:
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
            raise DriveError("oauth_network_error", retry=True) from None
        if response.status_code != 200:
            retry = response.status_code == 429 or response.status_code >= 500
            raise DriveError(
                "oauth_temporarily_unavailable"
                if retry
                else "oauth_reconnect_required",
                retry=retry,
            )
        try:
            token = response.json()
            if token.get("scope", SCOPE).split() != [SCOPE]:
                raise DriveError("oauth_scope_mismatch")
            if not isinstance(token["access_token"], str) or not token["access_token"]:
                raise ValueError
            return token
        except (AttributeError, KeyError, ValueError, TypeError):
            raise DriveError("oauth_invalid_response") from None


class AccessTokens:
    def __init__(self, http: httpx.Client, client: OAuthClient, refresh_token: str):
        self.http = http
        self.client = client
        self.refresh_token = refresh_token
        self.access_token = ""
        self.expires = 0.0

    def get(self, *, refresh: bool = False) -> str:
        if refresh or monotonic() >= self.expires:
            result = self.client.exchange(
                self.http,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
            try:
                expires = int(result["expires_in"])
                if expires <= 0:
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise DriveError("oauth_invalid_expiry") from None
            self.access_token = result["access_token"]
            self.expires = monotonic() + max(0, expires - 60)
        return self.access_token


def secret_path(name: str, filename: str) -> Path:
    return Path(os.getenv(name, f"data/drive-secrets/{filename}"))


def load_tokens(http: httpx.Client) -> AccessTokens:
    client = OAuthClient.read(secret_path("DRIVE_OAUTH_CLIENT_FILE", "client.json"))
    try:
        refresh = json.loads(
            secret_path("DRIVE_OAUTH_TOKEN_FILE", "token.json").read_text(
                encoding="utf-8"
            )
        )
        if refresh["client_id"] != client.client_id or refresh["scope"] != SCOPE:
            raise ValueError
        token = refresh["refresh_token"]
        if not isinstance(token, str) or not token:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise DriveError("drive_authorization_missing_or_invalid") from None
    return AccessTokens(http, client, token)


def receive_code(client: OAuthClient, verifier: str) -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    result: list[str] = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return  # Callback URLs contain the authorization code.

        def do_GET(self):
            request = urlsplit(self.path)
            query = parse_qs(request.query)
            valid = request.path == "/oauth/callback" and compare_digest(
                query.get("state", [""])[0].encode(),
                state.encode(),
            )
            if valid and query.get("code"):
                result.append(query["code"][0])
            elif valid and query.get("error"):
                result.append("")
            self.send_response(200 if result and result[0] else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(
                b"Authorization received. Return to the terminal."
                if result and result[0]
                else b"Authorization was not completed."
            )

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        print("Open this URL in your browser and choose the intended Google account:")
        print(client.authorization_url(redirect, state, verifier), flush=True)
        server.timeout = 1
        deadline = monotonic() + 300
        while not result and monotonic() < deadline:
            server.handle_request()
    if not result or not result[0]:
        raise DriveError("oauth_consent_denied_or_timed_out")
    return result[0], redirect


def main():
    parser = argparse.ArgumentParser(description="Authorize Google Drive on the host.")
    parser.add_argument(
        "--replace", action="store_true", help="Reconnect the same Google account."
    )
    args = parser.parse_args()
    try:
        client = OAuthClient.read(secret_path("DRIVE_OAUTH_CLIENT_FILE", "client.json"))
        destination = secret_path("DRIVE_OAUTH_TOKEN_FILE", "token.json")
        if destination.exists() and not args.replace:
            raise DriveError("drive_token_already_exists")
        verifier = secrets.token_urlsafe(64)
        code, redirect = receive_code(client, verifier)
        with httpx.Client(timeout=20, trust_env=False) as http:
            token = client.exchange(
                http,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect,
                    "code_verifier": verifier,
                },
            )
        refresh = token.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise DriveError("oauth_refresh_token_missing")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "client_id": client.client_id,
                        "scope": SCOPE,
                        "refresh_token": refresh,
                    },
                    handle,
                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        print("Google Drive authorization saved. Start or restart the Drive worker.")
    except (DriveError, OSError) as exc:
        raise SystemExit(
            exc.code if isinstance(exc, DriveError) else "drive_token_storage_failed"
        ) from None


if __name__ == "__main__":
    main()
