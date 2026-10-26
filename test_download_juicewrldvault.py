"""Offline regression checks; no website requests or music downloads."""

import contextlib
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import download_juicewrldvault as vault


class Response(io.BytesIO):
    def __init__(self, data, status=200, content_type="audio/mpeg", **headers):
        super().__init__(data)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        for key, value in headers.items():
            self.headers[key.replace("_", "-")] = value


class FakeClient:
    retries = 0

    def __init__(self, response):
        self.response = response
        self.headers = None

    def open(self, url, headers):
        self.headers = headers
        return self.response

    def check_stopped(self):
        pass


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="vault_offline_")
        self.addCleanup(self.folder.cleanup)
        self.target = Path(self.folder.name) / "song.mp3"
        self.url = "https://example.invalid/song"

    def test_complete_download_then_skip(self):
        client = FakeClient(Response(b"abcdef", Content_Length="6"))
        self.assertEqual(vault.download(client, self.url, self.target, "audio"), "downloaded")
        self.assertEqual(self.target.read_bytes(), b"abcdef")
        self.assertEqual(vault.download(client, self.url, self.target, "audio"), "skipped")

    def seed_partial(self, state=None):
        part = self.target.with_name("song.mp3.part")
        part.write_bytes(b"abc")
        vault.save_json(part.with_name(part.name + ".json"), state if state is not None else {
            "url": self.url, "validator": '"v1"'
        })

    def test_range_resume(self):
        self.seed_partial()
        client = FakeClient(Response(b"def", 206, Content_Range="bytes 3-5/6"))
        vault.download(client, self.url, self.target, "audio")
        self.assertEqual(client.headers, {"Range": "bytes=3-", "If-Range": '"v1"'})
        self.assertEqual(self.target.read_bytes(), b"abcdef")

    def test_server_ignores_range_restarts_file(self):
        self.seed_partial()
        client = FakeClient(Response(b"new", Content_Length="3"))
        vault.download(client, self.url, self.target, "audio")
        self.assertEqual(self.target.read_bytes(), b"new")

    def test_invalid_resume_state_restarts_file(self):
        self.seed_partial(["invalid", "state"])
        client = FakeClient(Response(b"new", Content_Length="3"))
        vault.download(client, self.url, self.target, "audio")
        self.assertEqual(client.headers, {})
        self.assertEqual(self.target.read_bytes(), b"new")

    def test_incomplete_download_is_not_published(self):
        client = FakeClient(Response(b"abc", Content_Length="6"))
        with self.assertRaises(ConnectionError):
            vault.download(client, self.url, self.target, "audio")
        self.assertFalse(self.target.exists())

    def test_error_page_is_not_saved_as_music(self):
        client = FakeClient(Response(b"<html>Error</html>", content_type="text/html"))
        with self.assertRaisesRegex(RuntimeError, "Unexpected text/html"):
            vault.download(client, self.url, self.target, "audio")
        self.assertFalse(self.target.exists())

    def test_invalid_range_is_rejected(self):
        self.seed_partial()
        client = FakeClient(Response(b"def", 206, Content_Range="bytes 2-4/6"))
        with self.assertRaisesRegex(RuntimeError, "Invalid partial"):
            vault.download(client, self.url, self.target, "audio")
        self.assertFalse(self.target.exists())

    def test_portable_names(self):
        self.assertEqual(vault.safe_name("../CON"), "_CON")
        name = vault.safe_name("\U0001f3b5" * 110)
        self.assertLessEqual(len(name.encode("utf-8")), 150)
        self.assertNotIn("/", vault.safe_name("../title"))

    def test_changed_catalog_schema_has_clear_error(self):
        client = vault.Client()
        for data in ([], {"ids": "invalid"}):
            with patch.object(client, "json", return_value=data):
                with self.assertRaisesRegex(RuntimeError, "Unexpected catalog"):
                    vault.catalog(client)

    def test_catalog_merges_ids_and_detects_missing_metadata(self):
        client = vault.Client()
        responses = [{"ids": ["1", "2"], "rawIds": ["2"]},
                     {"songs": [{"id": "1"}]}]
        with patch.object(client, "json", side_effect=responses) as request:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "Metadata missing for 1"):
                    vault.catalog(client)
        self.assertEqual(request.call_args.args[1], {"ids": ["1", "2"]})

    def test_nonfinite_cli_options_rejected_before_network(self):
        for option, value in (("--delay", "nan"), ("--timeout", "inf"), ("--timeout", "0")):
            with patch("sys.argv", ["downloader", option, value]), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(vault, "catalog") as catalog:
                with self.assertRaises(SystemExit) as error:
                    vault.main()
                self.assertEqual(error.exception.code, 2)
                catalog.assert_not_called()

    def test_history_survives_removed_media(self):
        path = Path(self.folder.name) / "history.json"
        history = vault.DownloadHistory(path)
        history.completed["audio"].add("song-1")
        history.save()
        restored = vault.DownloadHistory(path)
        self.assertEqual(restored.completed["audio"], {"song-1"})
        self.assertEqual(restored.completed["image"], set())

    def test_history_scan_matches_ids_after_title_changes(self):
        output = Path(self.folder.name)
        (output / "songs").mkdir()
        identifier = vault.song_identifier("song-1")
        (output / "songs" / f"Old title [{identifier}].mp3").write_bytes(b"audio")
        history = vault.DownloadHistory(output / "history.json")
        found = history.scan([{"id": "song-1", "title": "New title"}], output)
        self.assertEqual(found, {"audio": 1, "image": 0})
        self.assertEqual(history.completed["audio"], {"song-1"})

    def test_invalid_history_is_not_silently_overwritten(self):
        path = Path(self.folder.name) / "history.json"
        path.write_text(json.dumps({"version": 2}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Cannot read download history"):
            vault.DownloadHistory(path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 2})

    def test_cancelled_download_makes_no_request(self):
        client = vault.Client()
        client.stop.set()
        with patch.object(client, "open") as request:
            with self.assertRaises(vault.DownloadCancelled):
                vault.download(client, self.url, self.target, "audio")
            request.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_invalid_worker_counts_rejected_before_network(self):
        for workers in ("0", "33"):
            with patch("sys.argv", ["downloader", "--workers", workers]), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(vault, "catalog") as catalog:
                with self.assertRaises(SystemExit) as error:
                    vault.main()
                self.assertEqual(error.exception.code, 2)
                catalog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
