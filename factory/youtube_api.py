"""YouTube endpoints on the existing authenticated FastAPI router."""

import os
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update

from factory.contracts import (
    YouTubeApproval,
    YouTubeChannelConfirmation,
    YouTubePublication,
    YouTubeReconciliation,
)
from factory.db import Conflict, digest, now, targets
from factory.youtube import YouTubePublisher
from factory.youtube_db import connections, runtime
from factory.youtube_oauth import (
    AccessTokens,
    OAuthConfig,
    YouTubeError,
    begin,
    connection,
    finish_connection,
    live_enabled,
    revoke,
)
from factory import youtube_publications as publications


def http_client():
    return httpx.Client(
        timeout=float(os.getenv("YOUTUBE_REQUEST_TIMEOUT", "35")), trust_env=False
    )


def require_live():
    if not live_enabled():
        raise YouTubeError("youtube_live_provider_disabled")
    OAuthConfig.load()


def public_record(ledger, key):
    row, state = publications.get(ledger, key)
    # Never serialize encrypted credential/session blobs, leases, or OAuth state.
    return {
        **row,
        "provider": "youtube",
        "snapshot_hash": digest(row["snapshot"]),
        "upload": {
            k: state[k]
            for k in [
                "phase",
                "offset",
                "size",
                "attempts",
                "error",
                "created",
                "updated",
                "verified",
                "remote",
            ]
        },
        "url": ("https://www.youtube.com/watch?v=" + row["post_id"])
        if row["post_id"]
        else None,
    }


def register(router, operator):
    def ledger():
        from factory.api import ledger as current

        return current

    @router.get("/integrations/youtube/status", dependencies=[Depends(operator)])
    def status():
        configured = True
        code = None
        try:
            OAuthConfig.load()
        except YouTubeError as exc:
            configured, code = False, exc.code
        rows = ledger().list_rows(connections, connections.c.id == "youtube")
        account = (
            {k: v for k, v in rows[0].items() if k != "credentials"} if rows else None
        )
        return {
            "provider": os.getenv("PUBLISHER_PROVIDER", "mock"),
            "enabled": live_enabled(),
            "configured": configured,
            "configuration_error": code,
            "connection": account,
        }

    @router.get("/integrations/youtube/connect", dependencies=[Depends(operator)])
    def connect(request: Request, response: Response):
        require_live()
        browser = secrets.token_urlsafe(32)
        url = begin(ledger(), browser)
        config = OAuthConfig.load()
        response.set_cookie(
            "youtube_oauth_browser",
            browser,
            max_age=600,
            httponly=True,
            secure=urlsplit(config.redirect).scheme == "https",
            samesite="lax",
            path="/api/v1/integrations/youtube",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"authorization_url": url}

    @router.get("/integrations/youtube/callback", response_class=HTMLResponse)
    def callback(request: Request, code: str = "", state: str = "", error: str = ""):
        # Browser callback cannot carry the operator header: single-use state+PKCE
        # and an HttpOnly browser binding authenticate this one OAuth exchange.
        require_live()
        if error:
            from factory.youtube_oauth import consume

            consume(ledger(), state, request.cookies.get("youtube_oauth_browser", ""))
            raise YouTubeError("youtube_oauth_consent_denied")
        with http_client() as http:
            finish_connection(
                ledger(),
                http,
                code,
                state,
                request.cookies.get("youtube_oauth_browser", ""),
            )
        response = HTMLResponse(
            "<p>YouTube authorization received. Return to the operator dashboard and confirm the displayed channel ID. Uploads remain disabled until confirmation.</p>",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'",
            },
        )
        response.delete_cookie(
            "youtube_oauth_browser", path="/api/v1/integrations/youtube"
        )
        return response

    @router.post("/integrations/youtube/confirm", dependencies=[Depends(operator)])
    def confirm(body: YouTubeChannelConfirmation):
        require_live()
        with ledger().write() as db:
            row = (
                db.execute(
                    ledger().lock(
                        select(connections).where(connections.c.id == "youtube")
                    )
                )
                .mappings()
                .first()
            )
            if (
                not row
                or row.channel_id != body.channel_id
                or row.revision != body.revision
                or row.state != "CONFIRM_CHANNEL"
            ):
                raise Conflict(
                    "Channel/revision mismatch; reconnect or review the current channel"
                )
            db.execute(
                update(connections)
                .where(connections.c.id == "youtube")
                .values(state="CONNECTED", updated=now().isoformat())
            )
        return {"state": "CONNECTED", "channel_id": body.channel_id}

    @router.post("/integrations/youtube/disconnect", dependencies=[Depends(operator)])
    def disconnect():
        with http_client() as http:
            revoke(ledger(), http)
        return {"state": "DISCONNECTED"}

    @router.post(
        "/publications/youtube", dependencies=[Depends(operator)], status_code=201
    )
    def create(body: YouTubePublication):
        require_live()
        key = publications.create(ledger(), body)
        return public_record(ledger(), key)

    @router.get("/publications/{key}", dependencies=[Depends(operator)])
    def get(key: str):
        return public_record(ledger(), key)

    @router.post("/publications/{key}/approve", dependencies=[Depends(operator)])
    def approve(key: str, body: YouTubeApproval):
        require_live()
        publications.approve(ledger(), key, body)
        return public_record(ledger(), key)

    @router.patch("/publications/{key}", dependencies=[Depends(operator)])
    def edit(key: str, body: YouTubePublication):
        require_live()
        with publications.publication_lock(ledger(), key) as acquired:
            if not acquired:
                raise Conflict("Publication worker is active")
            row, upload = publications.get(ledger(), key)
            if (
                upload["phase"] != "NEW"
                or row["post_id"]
                or row["state"] not in {"DRAFT", "FAILED", "CANCELLED", "AUTH_REQUIRED"}
            ):
                raise Conflict(
                    "Only unsent metadata can be edited; started sessions require reconciliation"
                )
            account = connection(ledger())
            source, _ = publications.source_snapshot(ledger(), body.job, body.part)
            if (
                body.channel_id != account["channel_id"]
                or digest(["youtube", body.channel_id, source["sha256"]]) != key
            ):
                raise Conflict("Editing cannot change source artifact or channel")
            snapshot = {
                **body.model_dump(mode="json"),
                **source,
                "provider": "youtube",
                "mode": "live",
                "account": "youtube:" + body.channel_id,
                "connection_revision": account["revision"],
            }
            with ledger().write() as db:
                db.execute(
                    update(targets)
                    .where(targets.c.id == key)
                    .values(snapshot=snapshot, approval="", state="DRAFT")
                )
                db.execute(
                    update(runtime)
                    .where(runtime.c.id == key)
                    .values(
                        approved_hash=None,
                        attempts=0,
                        error=None,
                        updated=now().isoformat(),
                    )
                )
        return public_record(ledger(), key)

    @router.post("/publications/{key}/retry", dependencies=[Depends(operator)])
    def retry(key: str):
        require_live()
        row, upload = publications.get(ledger(), key)
        account = connection(ledger())
        if account["revision"] != row["snapshot"]["connection_revision"]:
            raise Conflict("Reconnection requires explicit reapproval of this snapshot")
        if row["state"] not in {"RETRY_PENDING", "AUTH_REQUIRED", "FAILED"} or upload[
            "error"
        ] not in {
            "youtube_network_error",
            "youtube_token_network_error",
            "youtube_token_service_unavailable",
            "youtube_service_unavailable",
            "youtube_rate_limited",
            "youtube_quota_exhausted",
            "youtube_auth_required",
            "youtube_local_artifact_unavailable",
        }:
            raise Conflict(
                "This state requires approval, metadata correction, or reconciliation instead of upload retry"
            )
        with publications.publication_lock(ledger(), key) as acquired:
            if not acquired:
                raise Conflict("Publication worker is active")
            with ledger().write() as db:
                db.execute(
                    update(targets)
                    .where(targets.c.id == key)
                    .values(state="RETRY_PENDING", due=now().isoformat())
                )
                db.execute(
                    update(runtime)
                    .where(runtime.c.id == key)
                    .values(
                        token=None,
                        expires=None,
                        attempts=0,
                        error=None,
                        updated=now().isoformat(),
                    )
                )
        return public_record(ledger(), key)

    @router.post("/publications/{key}/reconcile", dependencies=[Depends(operator)])
    def reconcile(key: str, body: YouTubeReconciliation):
        require_live()
        with publications.publication_lock(ledger(), key) as acquired:
            if not acquired:
                raise Conflict("Publication worker is active")
            row, upload = publications.get(ledger(), key)
            if not row["approval"] or upload["approved_hash"] != digest(
                row["snapshot"]
            ):
                raise Conflict("Live approval is required")
            account = connection(ledger())
            if account["channel_id"] != row["snapshot"]["channel_id"]:
                raise Conflict("Wrong connected channel")
            identifier = row["post_id"] or body.video_id
            if identifier:
                if not row["post_id"] and not body.confirm_remote_identity:
                    raise Conflict(
                        "Confirm the Studio video matches this source before attaching its ID"
                    )
                with http_client() as http:
                    remote = YouTubePublisher(
                        http, AccessTokens(ledger(), http)
                    ).verify(identifier, account["channel_id"])
                state, error = publications.remote_state(remote, row["snapshot"])
                with ledger().write() as db:
                    db.execute(
                        update(targets)
                        .where(targets.c.id == key)
                        .values(
                            post_id=identifier,
                            state=state,
                            analytics={"provider": "youtube", "url": remote["url"]},
                            due=(now() + timedelta(seconds=300)).isoformat(),
                        )
                    )
                    db.execute(
                        update(runtime)
                        .where(runtime.c.id == key)
                        .values(
                            remote=remote,
                            phase="COMPLETE",
                            error=error,
                            token=None,
                            expires=None,
                            verified=now().isoformat(),
                            updated=now().isoformat(),
                        )
                    )
            elif upload["session"]:
                # Worker queries this same session. It cannot initiate a replacement.
                with ledger().write() as db:
                    db.execute(
                        update(targets)
                        .where(targets.c.id == key)
                        .values(state="RETRY_PENDING", due=now().isoformat())
                    )
                    db.execute(
                        update(runtime)
                        .where(runtime.c.id == key)
                        .values(token=None, expires=None, updated=now().isoformat())
                    )
            else:
                raise Conflict(
                    "No safe automatic reconciliation: inspect YouTube Studio; provide its video ID with confirm_remote_identity=true if found. No replacement upload was started."
                )
        return public_record(ledger(), key)

    @router.post("/publications/{key}/cancel", dependencies=[Depends(operator)])
    def cancel(key: str):
        with publications.publication_lock(ledger(), key) as acquired:
            if not acquired:
                raise Conflict("Publication worker is active")
            row, upload = publications.get(ledger(), key)
            if upload["phase"] != "NEW" or row["post_id"]:
                raise Conflict(
                    "Started uploads and remote schedules must be inspected/cancelled in YouTube Studio"
                )
            with ledger().write() as db:
                db.execute(
                    update(targets).where(targets.c.id == key).values(state="CANCELLED")
                )
                db.execute(
                    update(runtime)
                    .where(runtime.c.id == key)
                    .values(
                        token=None, expires=None, error=None, updated=now().isoformat()
                    )
                )
        return public_record(ledger(), key)
