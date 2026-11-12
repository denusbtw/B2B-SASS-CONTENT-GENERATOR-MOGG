"""Drive v3 transport for a single app-managed folder."""

import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from factory.drive_oauth import AccessTokens, DriveError

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "AI Video Publisher"
FOLDER_MIME = "application/vnd.google-apps.folder"
MARKER = "ai-video-publisher-v1"
FIELDS = "id,name,mimeType,parents,ownedByMe,trashed,appProperties,size,webViewLink"
CHUNK_SIZE = 1024 * 1024


def valid_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise DriveError("drive_invalid_file_id")
    return value


class Drive:
    def __init__(self, http: httpx.Client, tokens: AccessTokens):
        self.http = http
        self.tokens = tokens

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
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
            raise DriveError("drive_network_error", retry=True) from None
        if response.status_code == 401:
            raise DriveError("oauth_reconnect_required")
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise DriveError("drive_temporarily_unavailable", retry=True)
        if response.status_code == 403:
            try:
                reasons = {
                    error.get("reason") for error in response.json()["error"]["errors"]
                }
            except (KeyError, TypeError, ValueError, AttributeError):
                reasons = set()
            if reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
                raise DriveError("drive_rate_limited", retry=True)
            raise DriveError("drive_permission_or_quota_denied")
        return response

    @staticmethod
    def body(response: httpx.Response) -> dict:
        if not response.is_success:
            raise DriveError(f"drive_http_{response.status_code}")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            raise DriveError("drive_invalid_response", retry=True) from None

    def allocate(self) -> str:
        data = self.body(
            self.request(
                "GET", f"{API}/files/generateIds", params={"count": 1, "space": "drive"}
            )
        )
        ids = data.get("ids")
        if not isinstance(ids, list) or len(ids) != 1:
            raise DriveError("drive_invalid_allocation")
        return valid_id(ids[0])

    def get_file(self, file_id: str) -> dict | None:
        response = self.request(
            "GET", f"{API}/files/{valid_id(file_id)}", params={"fields": FIELDS}
        )
        return None if response.status_code == 404 else self.body(response)

    def find_folder(self) -> str | None:
        query = (
            f"name = '{FOLDER}' and mimeType = '{FOLDER_MIME}' "
            f"and trashed = false and appProperties has {{ key='factory' and value='{MARKER}' }}"
        )
        data = self.body(
            self.request(
                "GET",
                f"{API}/files",
                params={
                    "q": query,
                    "spaces": "drive",
                    "fields": f"nextPageToken,files({FIELDS})",
                    "pageSize": 2,
                },
            )
        )
        matches = data.get("files", [])
        if data.get("nextPageToken") or len(matches) > 1:
            raise DriveError("drive_multiple_managed_folders")
        if matches:
            file_id = valid_id(matches[0].get("id"))
            self.verify_folder(file_id, matches[0])
            return file_id
        return None

    @staticmethod
    def verify_folder(folder_id: str, folder: dict) -> None:
        if (
            folder.get("id") != folder_id
            or folder.get("name") != FOLDER
            or folder.get("mimeType") != FOLDER_MIME
            or folder.get("trashed") is not False
            or folder.get("ownedByMe") is not True
            or (folder.get("appProperties") or {}).get("factory") != MARKER
        ):
            raise DriveError("drive_destination_invalid")

    def prepare_folder(self, folder_id: str, *, confirmed: bool) -> None:
        folder = self.get_file(folder_id)
        if folder is None:
            if confirmed:
                raise DriveError("drive_destination_unavailable")
            response = self.request(
                "POST",
                f"{API}/files",
                params={"fields": FIELDS},
                json={
                    "id": valid_id(folder_id),
                    "name": FOLDER,
                    "mimeType": FOLDER_MIME,
                    "appProperties": {"factory": MARKER},
                },
            )
            folder = (
                self.get_file(folder_id)
                if response.status_code == 409
                else self.body(response)
            )
            if folder is None:
                raise DriveError("drive_folder_reconciliation_pending", retry=True)
        self.verify_folder(folder_id, folder)

    @staticmethod
    def verify_upload(
        data: dict, file_id: str, folder_id: str, sha256: str, size: int
    ) -> str:
        if (
            data.get("id") != file_id
            or data.get("parents") != [folder_id]
            or data.get("mimeType") != "video/mp4"
            or data.get("trashed") is not False
            or (data.get("appProperties") or {}).get("sha256") != sha256
            or str(data.get("size")) != str(size)
        ):
            raise DriveError("drive_upload_metadata_mismatch")
        return f"https://drive.google.com/file/d/{valid_id(file_id)}/view"

    def upload(
        self, path: Path, name: str, file_id: str, folder_id: str, sha256: str
    ) -> str:
        size = path.stat().st_size
        if not size:
            raise DriveError("drive_empty_video")
        existing = self.get_file(file_id)
        if existing is not None:
            return self.verify_upload(existing, file_id, folder_id, sha256, size)
        response = self.request(
            "POST",
            UPLOAD,
            params={"uploadType": "resumable", "fields": FIELDS},
            headers={
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
            json={
                "id": valid_id(file_id),
                "name": name,
                "mimeType": "video/mp4",
                "parents": [valid_id(folder_id)],
                "appProperties": {"sha256": sha256},
            },
        )
        if response.status_code == 409:
            existing = self.get_file(file_id)
            if existing is None:
                raise DriveError("drive_upload_reconciliation_pending", retry=True)
            return self.verify_upload(existing, file_id, folder_id, sha256, size)
        if not response.is_success:
            self.body(response)
        session = response.headers.get("Location", "")
        endpoint = urlsplit(session)
        if (
            endpoint.scheme != "https"
            or endpoint.netloc != "www.googleapis.com"
            or endpoint.path != "/upload/drive/v3/files"
        ):
            raise DriveError("drive_invalid_upload_session")
        offset = 0
        with path.open("rb") as source:
            while offset < size:
                source.seek(offset)
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    raise DriveError("drive_local_video_changed")
                try:
                    response = self.request(
                        "PUT",
                        session,
                        content=chunk,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
                        },
                    )
                except DriveError as exc:
                    if not exc.retry:
                        raise
                    response = self.request(
                        "PUT",
                        session,
                        content=b"",
                        headers={"Content-Range": f"bytes */{size}"},
                    )
                if response.status_code == 308:
                    match = re.fullmatch(
                        r"bytes=0-(\d+)", response.headers.get("Range", "")
                    )
                    if not match or not offset < (
                        next_offset := int(match[1]) + 1
                    ) <= offset + len(chunk):
                        raise DriveError("drive_upload_no_progress", retry=True)
                    offset = next_offset
                    continue
                if response.status_code in {404, 409, 410}:
                    raise DriveError("drive_upload_reconciliation_pending", retry=True)
                data = self.body(response)
                return self.verify_upload(data, file_id, folder_id, sha256, size)
        raise DriveError("drive_upload_confirmation_missing", retry=True)
