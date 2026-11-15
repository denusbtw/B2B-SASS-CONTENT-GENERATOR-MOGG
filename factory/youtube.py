"""Official YouTube v3 transport; no receipt fabrication or automatic session replacement."""

import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from factory.youtube_oauth import API, YouTubeError

UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"


def video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise YouTubeError("youtube_upload_response_uncertain", ambiguous=True)
    return value


def validate_session(session):
    parsed = urlsplit(session)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.googleapis.com"
        or parsed.path != "/upload/youtube/v3/videos"
        or parsed.fragment
    ):
        raise YouTubeError("youtube_invalid_upload_session", ambiguous=True)
    return session


def retry_seconds(response):
    value = response.headers.get("Retry-After", "0")
    try:
        return min(86400, max(0, int(value)))
    except ValueError:
        try:
            return min(
                86400,
                max(
                    0,
                    (
                        parsedate_to_datetime(value) - datetime.now(timezone.utc)
                    ).total_seconds(),
                ),
            )
        except (ValueError, TypeError):
            return 0


class YouTubePublisher:
    provider = "youtube"

    def __init__(self, http, tokens):
        self.http, self.tokens = http, tokens
        self.chunk_size = int(os.getenv("YOUTUBE_UPLOAD_CHUNK_BYTES", str(8 * 1024**2)))
        if (
            self.chunk_size <= 0
            or self.chunk_size % (256 * 1024)
            or self.chunk_size > 64 * 1024**2
        ):
            raise YouTubeError("youtube_chunk_size_must_be_256KiB_multiple_up_to_64MiB")

    def dispatch(self, ledger):
        from factory.youtube_publications import dispatch_youtube

        return dispatch_youtube(ledger, self)

    def request(self, method, url, **kwargs):
        headers = kwargs.pop("headers", {})
        try:
            for refresh in (False, True):
                response = self.http.request(
                    method,
                    url,
                    headers={
                        **headers,
                        "Authorization": "Bearer " + self.tokens.get(refresh=refresh),
                    },
                    follow_redirects=False,
                    **kwargs,
                )
                if response.status_code != 401:
                    break
        except httpx.RequestError:
            raise YouTubeError(
                "youtube_network_error", retry=True, ambiguous=method in {"POST", "PUT"}
            ) from None
        if response.status_code in {200, 201, 308}:
            return response
        try:
            reasons = {
                e.get("reason")
                for e in response.json().get("error", {}).get("errors", [])
            }
        except (ValueError, AttributeError, TypeError):
            reasons = set()
        if response.status_code == 401:
            raise YouTubeError("youtube_auth_required", auth=True)
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}:
            raise YouTubeError("youtube_quota_exhausted")
        if (
            response.status_code == 429
            or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}
            or response.status_code in {408, 500, 502, 503, 504}
        ):
            raise YouTubeError(
                "youtube_rate_limited"
                if response.status_code in {403, 429}
                else "youtube_service_unavailable",
                retry=True,
                retry_after=retry_seconds(response),
            )
        if response.status_code in {404, 410} and url.startswith(UPLOAD):
            raise YouTubeError(
                "youtube_session_expired_reconciliation_required", ambiguous=True
            )
        raise YouTubeError(
            "youtube_permission_denied"
            if response.status_code == 403
            else "youtube_metadata_or_request_rejected"
        )

    def channel(self):
        response = self.request(
            "GET", API + "/channels", params={"part": "snippet", "mine": "true"}
        )
        try:
            items = response.json()["items"]
            if len(items) != 1:
                raise ValueError
            return items[0]["id"]
        except (ValueError, KeyError, TypeError):
            raise YouTubeError("youtube_channel_lookup_invalid", auth=True) from None

    @staticmethod
    def metadata(snapshot):
        snippet = {"title": snapshot["title"], "description": snapshot["description"]}
        if snapshot.get("tags"):
            snippet["tags"] = snapshot["tags"]
        if snapshot.get("category_id"):
            snippet["categoryId"] = snapshot["category_id"]
        status = {
            "privacyStatus": "private"
            if snapshot.get("publish_at")
            else snapshot["visibility"],
            "selfDeclaredMadeForKids": snapshot["made_for_kids"],
            "containsSyntheticMedia": snapshot["contains_synthetic_media"],
        }
        if snapshot.get("publish_at"):
            status["publishAt"] = snapshot["publish_at"]
        return {"snippet": snippet, "status": status}

    @staticmethod
    def completed(response):
        try:
            return video_id(response.json()["id"])
        except (ValueError, KeyError, TypeError):
            raise YouTubeError(
                "youtube_upload_response_uncertain", ambiguous=True
            ) from None

    def upload(self, path, snapshot, state, save, guard):
        size = path.stat().st_size
        session = state.get("session_url")
        chunk_size = state.get("chunk_size") or self.chunk_size
        if not session:
            if state["phase"] != "NEW":
                raise YouTubeError("youtube_session_creation_uncertain", ambiguous=True)
            guard()
            save(phase="INITIATING")
            response = self.request(
                "POST",
                UPLOAD,
                params={"uploadType": "resumable", "part": "snippet,status"},
                json=self.metadata(snapshot),
                headers={
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(size),
                },
            )
            session = validate_session(response.headers.get("Location", ""))
            save(session_url=session, chunk_size=chunk_size, phase="SESSION")
        validate_session(session)
        guard()
        response = self.request(
            "PUT", session, content=b"", headers={"Content-Range": f"bytes */{size}"}
        )
        if response.status_code != 308:
            key = self.completed(response)
            save(video_id=key, phase="COMPLETE", offset=size)
            return key
        offset = self.offset(response, size)
        save(offset=offset)
        with path.open("rb") as handle:
            while offset < size:
                guard()
                handle.seek(offset)
                chunk = handle.read(chunk_size)
                if not chunk:
                    raise YouTubeError("youtube_local_artifact_changed")
                # Any network error leaves the session intact. The next claim queries
                # its offset; it never guesses whether the final bytes were received.
                response = self.request(
                    "PUT",
                    session,
                    content=chunk,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
                    },
                )
                if response.status_code == 308:
                    next_offset = self.offset(response, size)
                    if not offset < next_offset <= offset + len(chunk):
                        raise YouTubeError("youtube_upload_no_progress", retry=True)
                    offset = next_offset
                    save(offset=offset)
                else:
                    key = self.completed(response)
                    save(video_id=key, phase="COMPLETE", offset=size)
                    return key
        raise YouTubeError("youtube_completion_response_missing", ambiguous=True)

    @staticmethod
    def offset(response, size):
        raw = response.headers.get("Range", "")
        if not raw:
            return 0
        match = re.fullmatch(r"bytes=0-(\d+)", raw)
        if not match or not 0 < int(match[1]) + 1 <= size:
            raise YouTubeError("youtube_invalid_upload_offset", ambiguous=True)
        return int(match[1]) + 1

    def verify(self, key, channel_id):
        key = video_id(key)
        response = self.request(
            "GET",
            API + "/videos",
            params={"id": key, "part": "snippet,status,processingDetails"},
        )
        try:
            items = response.json()["items"]
            if (
                len(items) != 1
                or items[0]["id"] != key
                or items[0]["snippet"]["channelId"] != channel_id
            ):
                raise ValueError
            item = items[0]
            return {
                "video_id": key,
                "url": "https://www.youtube.com/watch?v=" + key,
                "status": item["status"],
                "processing": item.get("processingDetails", {}),
            }
        except (ValueError, KeyError, TypeError):
            raise YouTubeError(
                "youtube_remote_identity_unverified", ambiguous=True
            ) from None
