from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter

from collector.config import Settings, SourceConfig
from collector.crawler import DiscoveredDocument, DiscoveryResult
from collector.downloader import DownloadResult
from collector.main import _process_queue, run
from collector.state import StateStore
from tests.fakes import FakeBudget


def make_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


class NoPauseLimiter:
    def after_download(self):
        return None


class FakeHttp:
    def __init__(self, pdf_path: Path) -> None:
        self.pdf_path = pdf_path
        self.rate_limiter = NoPauseLimiter()

    def download(self, *args, **kwargs):
        return DownloadResult(
            path=self.pdf_path,
            original_url="https://example.gov/a.pdf",
            final_url="https://example.gov/a.pdf",
            file_name="a.pdf",
            content_type="application/pdf",
            size=self.pdf_path.stat().st_size,
            etag='"abc"',
            last_modified=None,
            http_status=200,
            elapsed_seconds=0.1,
            daily_request_count=1,
        )


class UploadFailDrive:
    def upload_document(self, *args, **kwargs):
        raise RuntimeError("upload falhou")


def test_upload_failure_does_not_mark_complete(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    make_pdf(pdf)
    settings = Settings(
        min_delay_seconds=0,
        max_delay_seconds=0,
        min_pause_seconds=0,
        max_pause_seconds=0,
        max_files_per_run=1,
        work_dir=tmp_path / "work",
        sources=(
            SourceConfig(
                name="fonte",
                base_url="https://example.gov",
                enabled=True,
                document_types=("contratos",),
            ),
        ),
    )
    state = StateStore(tmp_path / "state.sqlite3")
    state.enqueue_many(
        "fonte",
        "example.gov",
        [("https://example.gov/a.pdf", "https://example.gov/a.pdf", "contratos")],
        500,
    )
    downloaded, uploaded = _process_queue(
        settings,
        FakeHttp(pdf),
        FakeBudget(),
        state,
        UploadFailDrive(),
        "state-folder",
    )
    row = state.connection.execute("SELECT status FROM queue").fetchone()
    assert downloaded == 1
    assert uploaded == 0
    assert row[0] == "retry"
    assert not state.has_document_url("https://example.gov/a.pdf")
    state.close()


class FakeDriveForRun:
    def __init__(self, *_args, **_kwargs) -> None:
        self.folders = {"Contratos": "c", "Empenhos": "e", "Processos": "p", "Logs": "l", "Estado": "s"}

    def ensure_structure(self):
        return dict(self.folders)

    def ensure_json_file(self, parent_id, name, initial_data, state_key):
        return state_key

    def read_json(self, file_id):
        from collector.drive import JsonDocument
        return JsonDocument(file_id, "1", {"owner": None, "workflow_run_id": None, "expires_at": None})

    def update_json_if_match(self, file_id, data, etag):
        return "2"

    def download_named(self, parent_id, name, destination):
        return None

    def upload_or_replace_named(self, parent_id, path, name, mime_type, backup_prefix=None):
        return "state-id"

    def upload_log(self, path, run_id):
        return "log-id"


class FakeLock:
    def __init__(self, *_args, **_kwargs):
        pass

    def acquire(self, *args, **kwargs):
        return None

    def release(self):
        return None


class FakeCrawler:
    def __init__(self, *_args, **_kwargs):
        pass

    def discover(self, source, max_pages, max_documents):
        return DiscoveryResult(
            (DiscoveredDocument("https://example.gov/a.pdf", "https://example.gov/a.pdf", "contratos"),),
            1,
        )


class FakeBudgetForRun:
    def __init__(self, *_args, **_kwargs):
        pass

    def increment(self, *args, **kwargs):
        return 1

    def get(self, *args, **kwargs):
        return {"requests": 0, "downloads": 0}


class FakeHttpForRun:
    def __init__(self, *_args, **_kwargs):
        pass


def test_dry_run_does_not_process_download_queue(tmp_path: Path, monkeypatch) -> None:
    import collector.main as main_module

    monkeypatch.setattr(main_module, "DriveClient", FakeDriveForRun)
    monkeypatch.setattr(main_module, "SharedDriveLock", FakeLock)
    monkeypatch.setattr(main_module, "Crawler", FakeCrawler)
    monkeypatch.setattr(main_module, "DailyBudget", FakeBudgetForRun)
    monkeypatch.setattr(main_module, "HttpClient", FakeHttpForRun)
    monkeypatch.setattr(
        main_module,
        "_process_queue",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("não deveria baixar em DRY_RUN")),
    )

    settings = Settings(
        dry_run=True,
        work_dir=tmp_path / "work",
        log_dir=tmp_path / "logs",
        google_service_account_json='{"type":"service_account"}',
        google_drive_folder_id="root",
        sources=(SourceConfig(name="fonte", base_url="https://example.gov", enabled=True),),
    )
    assert run(settings) == 0
