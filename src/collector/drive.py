from __future__ import annotations

import io
import json
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

LOGGER = logging.getLogger(__name__)
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass(frozen=True)
class DriveObject:
    file_id: str
    name: str
    mime_type: str
    modified_time: str | None = None


@dataclass(frozen=True)
class JsonDocument:
    file_id: str
    etag: str | None
    data: dict[str, Any]


class PreconditionFailed(RuntimeError):
    pass


class DriveClient:
    def __init__(self, service_account_json: str, root_folder_id: str) -> None:
        info = json.loads(service_account_json)
        self.credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[DRIVE_SCOPE]
        )
        self.service = build("drive", "v3", credentials=self.credentials, cache_discovery=False)
        self.session = AuthorizedSession(self.credentials)
        self.root_folder_id = root_folder_id
        self.folders: dict[str, str] = {}

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def list_named(self, parent_id: str, name: str) -> list[DriveObject]:
        query = (
            f"'{parent_id}' in parents and trashed=false and "
            f"name='{self._escape(name)}'"
        )
        response = (
            self.service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id,name,mimeType,modifiedTime,createdTime)",
                orderBy="createdTime,id",
                pageSize=100,
            )
            .execute()
        )
        return [
            DriveObject(
                file_id=item["id"],
                name=item["name"],
                mime_type=item["mimeType"],
                modified_time=item.get("modifiedTime"),
            )
            for item in response.get("files", [])
        ]

    def ensure_folder(self, parent_id: str, name: str) -> str:
        candidates = [f for f in self.list_named(parent_id, name) if f.mime_type == FOLDER_MIME]
        if candidates:
            return candidates[0].file_id
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = self.service.files().create(body=body, fields="id").execute()
        return created["id"]

    def ensure_structure(self) -> dict[str, str]:
        for name in ("Contratos", "Empenhos", "Processos", "Logs", "Estado"):
            self.folders[name] = self.ensure_folder(self.root_folder_id, name)
        return dict(self.folders)

    def get_folder(self, name: str) -> str:
        if name not in self.folders:
            self.ensure_structure()
        return self.folders[name]

    def delete_file(self, file_id: str) -> None:
        self.service.files().delete(fileId=file_id).execute()

    def create_bytes(
        self,
        parent_id: str,
        name: str,
        content: bytes,
        mime_type: str,
        app_properties: dict[str, str] | None = None,
    ) -> str:
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        body: dict[str, Any] = {"name": name, "parents": [parent_id]}
        if app_properties:
            body["appProperties"] = app_properties
        created = (
            self.service.files()
            .create(body=body, media_body=media, fields="id")
            .execute()
        )
        return created["id"]

    def ensure_json_file(
        self,
        parent_id: str,
        name: str,
        initial_data: dict[str, Any],
        state_key: str,
    ) -> str:
        candidates = self.list_named(parent_id, name)
        if candidates:
            return candidates[0].file_id
        created_id = self.create_bytes(
            parent_id,
            name,
            json.dumps(initial_data, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json",
            {"collector_state_key": state_key},
        )
        # Nomes não são únicos no Drive. Eleição determinística pelo arquivo mais antigo.
        candidates = self.list_named(parent_id, name)
        canonical = candidates[0].file_id
        if created_id != canonical:
            self.delete_file(created_id)
        return canonical

    def read_json(self, file_id: str) -> JsonDocument:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        response = self.session.get(url, timeout=60)
        response.raise_for_status()
        data = response.json() if response.content else {}
        return JsonDocument(file_id=file_id, etag=response.headers.get("ETag"), data=data)

    def update_json_if_match(
        self,
        file_id: str,
        data: dict[str, Any],
        etag: str | None,
    ) -> str | None:
        url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        response = self.session.patch(
            url,
            headers=headers,
            data=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
            timeout=60,
        )
        if response.status_code == 412:
            raise PreconditionFailed("arquivo do Drive foi alterado por outro processo")
        response.raise_for_status()
        return response.headers.get("ETag")

    def download_file(self, file_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().get_media(fileId=file_id)
        temporary = destination.with_suffix(destination.suffix + ".download")
        with temporary.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request, chunksize=1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        temporary.replace(destination)

    def download_named(self, parent_id: str, name: str, destination: Path) -> str | None:
        candidates = self.list_named(parent_id, name)
        if not candidates:
            return None
        self.download_file(candidates[0].file_id, destination)
        return candidates[0].file_id

    def upload_file(
        self,
        parent_id: str,
        path: Path,
        name: str | None = None,
        mime_type: str | None = None,
        app_properties: dict[str, str] | None = None,
    ) -> str:
        resolved_mime = mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body: dict[str, Any] = {"name": name or path.name, "parents": [parent_id]}
        if app_properties:
            body["appProperties"] = app_properties
        media = MediaFileUpload(str(path), mimetype=resolved_mime, resumable=True, chunksize=1024 * 1024)
        request = self.service.files().create(body=body, media_body=media, fields="id")
        response = None
        while response is None:
            _, response = request.next_chunk()
        return response["id"]

    def update_file_content(self, file_id: str, path: Path, mime_type: str) -> None:
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True, chunksize=1024 * 1024)
        request = self.service.files().update(fileId=file_id, media_body=media, fields="id")
        response = None
        while response is None:
            _, response = request.next_chunk()

    def upload_or_replace_named(
        self,
        parent_id: str,
        path: Path,
        name: str,
        mime_type: str,
        backup_prefix: str | None = None,
    ) -> str:
        candidates = self.list_named(parent_id, name)
        if not candidates:
            return self.upload_file(parent_id, path, name=name, mime_type=mime_type)
        file_id = candidates[0].file_id
        if backup_prefix:
            timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup_name = f"{backup_prefix}-{timestamp}-{file_id}.bak"
            self.service.files().copy(
                fileId=file_id,
                body={"name": backup_name, "parents": [parent_id]},
                fields="id",
            ).execute()
        self.update_file_content(file_id, path, mime_type)
        return file_id

    def find_by_hash(self, parent_id: str, sha256: str) -> str | None:
        query = (
            f"'{parent_id}' in parents and trashed=false and "
            f"appProperties has {{ key='sha256' and value='{self._escape(sha256)}' }}"
        )
        response = self.service.files().list(q=query, spaces="drive", fields="files(id)", pageSize=2).execute()
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def upload_document(
        self,
        folder_name: str,
        path: Path,
        final_name: str,
        sha256: str,
        source_url: str,
    ) -> str:
        folder_id = self.get_folder(folder_name)
        existing = self.find_by_hash(folder_id, sha256)
        if existing:
            return existing
        return self.upload_file(
            folder_id,
            path,
            name=final_name,
            mime_type="application/pdf",
            app_properties={"sha256": sha256, "source_url": source_url[:124]},
        )

    def upload_log(self, log_path: Path, run_id: str) -> str:
        return self.upload_file(
            self.get_folder("Logs"),
            log_path,
            name=f"collector-{run_id}.jsonl",
            mime_type="application/x-ndjson",
        )
