"""Synthetic offline coverage for selection, recovery, locks and concurrent work."""

import argparse
import contextlib
from email.message import Message
from email.utils import formatdate
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import download_juicewrldvault as vault
from test_download_juicewrldvault import AUDIO, FRAME, FakeClient, Response

PNG = b"\x89PNG\r\n\x1a\n" + bytes(16) + b"\0\0\0\0IEND\xaeB`\x82"
SONGS = [{"id": "1", "title": "Night Drive", "author": "Juice WRLD",
          "vault_categories": ["songs"], "song_path": "audio/one-v1.mp3", "image_path": "one.png"},
         {"id": "2", "title": "Night Drive (Raw)", "author": "Juice WRLD",
          "vault_categories": ["raw"], "song_path": "audio/two-v1.mp3", "image_path": "two.png"},
         {"id": "3", "title": "Studio Beat", "author": "Producer",
          "vault_categories": ["instrumental"], "song_path": "audio/three-v1.mp3"}]


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="vault_controls_")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.output = self.root / "media"
        self.history = self.root / "history.json"

    def args(self, **changes):
        values = dict(output=self.output, history=self.history, song_id=None,
                      redownload=None, search=None, track_type="all", retry_failed=False,
                      limit=None, artwork=False, sync_history=False, list_only=False,
                      delay=0, retries=0, timeout=1, workers=2)
        values.update(changes)
        return argparse.Namespace(**values)

    def invoke(self, *options, responses=None):
        argv = ["downloader", "--output", str(self.output), "--history", str(self.history),
                "--delay", "0", "--retries", "0", *options]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", argv), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr), patch.object(vault, "catalog", return_value=SONGS), \
                patch.object(vault.Client, "open", side_effect=responses) as request:
            result = vault.main()
        return result, stdout.getvalue(), stderr.getvalue(), request

    def test_search_is_case_insensitive_and_combines_with_type_and_limit(self):
        args = self.args(search="NIGHT", track_type="raw", limit=1)
        self.assertEqual([s["id"] for s in vault.select_songs(SONGS, args, {})], ["2"])
        self.assertEqual(vault.select_songs(SONGS, self.args(search="absent"), {}), [])
        self.assertEqual([s["id"] for s in vault.select_songs(
            SONGS, self.args(search="PRODUCER"), {})], ["3"])

    def test_ids_select_exact_tracks_and_redownload_only_forces_requested_ids(self):
        args = self.args(song_id=["1"], redownload=["2"])
        selected = vault.select_songs(SONGS, args, {})
        jobs = vault.build_jobs(selected, args, {})
        self.assertEqual([(j.song_id, j.force) for j in jobs], [("1", False), ("2", True)])
        with self.assertRaisesRegex(RuntimeError, "absent from the catalog: missing"):
            vault.select_songs(SONGS, self.args(song_id=["missing"]), {})

    def test_catalog_categories_come_from_pool_membership(self):
        pool = {"ids": [1], "rawIds": [1, 2], "instrumentalIds": [3]}
        client = vault.Client()
        with patch.object(client, "json", side_effect=[pool, {"songs": SONGS}]), \
                contextlib.redirect_stdout(io.StringIO()):
            songs = vault.catalog(client)
        self.assertEqual([s["vault_categories"] for s in songs],
                         [["songs", "raw"], ["raw"], ["instrumental"]])

    def test_retry_legacy_report_rebuilds_urls_and_paths(self):
        report = self.root / "failures.json"
        vault.save_json(report, [{"id": "2", "file": "C:/old/two.png", "url": "https://evil.invalid"}])
        failures = vault.load_failures(report)
        args = self.args(retry_failed=True)
        jobs = vault.build_jobs(vault.select_songs(SONGS, args, failures), args, failures)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, "image")
        self.assertTrue(jobs[0].force)
        self.assertEqual(jobs[0].destination.parent, self.output / "artwork")
        self.assertEqual(jobs[0].url, vault.MEDIA + "/images/two.png")

    def test_malformed_failure_report_does_not_get_overwritten(self):
        report = self.root / "failures.json"
        report.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Cannot read failure report"):
            vault.load_failures(report)
        self.assertEqual(report.read_text(encoding="utf-8"), "not json")
        with self.assertRaisesRegex(RuntimeError, "No failures.json"):
            vault.load_failures(self.root / "missing.json", required=True)

    def test_list_only_prints_ids_without_history_or_media_requests(self):
        result, stdout, _, request = self.invoke("--list-only", "--search", "night", "--type", "raw")
        self.assertEqual(result, 0)
        self.assertIn("2 | Night Drive (Raw)", stdout)
        self.assertNotIn("1 | Night Drive |", stdout)
        request.assert_not_called()
        self.assertFalse(self.history.exists())
        self.assertFalse(Path(str(self.history) + ".lock").exists())
        self.assertEqual(len(json.loads((self.output / "catalog.json").read_text())), 3)

    def test_redownload_keeps_old_file_on_failure_then_retries_successfully(self):
        args = self.args(song_id=["1"])
        job = vault.build_jobs(SONGS[:1], args, {})[0]
        job.destination.parent.mkdir(parents=True)
        job.destination.write_bytes(AUDIO)
        history = vault.DownloadHistory(self.history)
        history.record("1", "audio", job.destination)
        history.save()
        result, _, _, _ = self.invoke("--redownload", "1", responses=[Response(b"bad")])
        self.assertEqual(result, 1)
        self.assertEqual(job.destination.read_bytes(), AUDIO)
        self.assertNotIn("1", vault.DownloadHistory(self.history).completed["audio"])
        report = self.output / "failures.json"
        self.assertIn(("1", "audio"), vault.load_failures(report))
        result, _, _, request = self.invoke("--song-id", "1")
        self.assertEqual(result, 0)
        request.assert_not_called()
        self.assertIn(("1", "audio"), vault.load_failures(report))
        replacement = AUDIO + FRAME
        result, _, _, request = self.invoke("--retry-failed", responses=[Response(replacement)])
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(job.destination.read_bytes(), replacement)
        self.assertEqual(vault.load_failures(report), {})
        self.assertIn("1", vault.DownloadHistory(self.history).completed["audio"])

    def test_retry_resumes_partial_instead_of_discarding_it(self):
        job = vault.build_jobs(SONGS[:1], self.args(), {})[0]
        job.destination.parent.mkdir(parents=True)
        part = job.destination.with_name(job.destination.name + ".part")
        part.write_bytes(FRAME)
        vault.save_json(part.with_name(part.name + ".json"), {"url": job.url, "validator": '"v1"'})
        vault.save_json(self.output / "failures.json", [job.failure("Interrupted")])
        result, _, _, request = self.invoke("--retry-failed", responses=[
            Response(FRAME, 206, Content_Range="bytes 417-833/834")])
        self.assertEqual(result, 0)
        self.assertEqual(request.call_args.args[1]["Range"], "bytes=417-")
        self.assertEqual(job.destination.read_bytes(), AUDIO)

    def test_unavailable_retry_ids_do_not_block_available_tracks(self):
        self.output.mkdir()
        report = self.output / "failures.json"
        vault.save_json(report, [{"id": "1", "kind": "audio"}, {"id": "gone", "kind": "audio"}])
        result, _, stderr, request = self.invoke("--retry-failed", responses=[Response(AUDIO)])
        self.assertEqual(result, 1)
        self.assertEqual(request.call_count, 1)
        self.assertIn("Unavailable IDs", stderr)
        self.assertEqual(set(vault.load_failures(report)), {("gone", "audio")})

    def test_history_cannot_overwrite_output_control_files_or_temporary_files(self):
        for name in ("catalog.json", "catalog.json.tmp", "failures.json.tmp", ".download.lock", "songs"):
            with self.subTest(name=name), self.assertRaises(SystemExit) as error:
                self.invoke("--history", str(self.output / name))
            self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.output.exists())

    def test_inconsistent_range_headers_or_validator_do_not_modify_partial(self):
        target = self.root / "track.mp3"
        part = target.with_name("track.mp3.part")
        for headers in ({"Content_Length": "416"}, {"ETag": '"v2"'}):
            part.write_bytes(FRAME)
            vault.save_json(part.with_name(part.name + ".json"), {
                "url": "https://example.invalid", "validator": '"v1"'})
            client = FakeClient(Response(FRAME, 206, Content_Range="bytes 417-833/834", **headers))
            with self.subTest(headers=headers), self.assertRaises(RuntimeError):
                vault.download(client, "https://example.invalid", target, "audio")
            self.assertEqual(part.read_bytes(), FRAME)
            self.assertFalse(target.exists())

    def test_retry_artwork_does_not_download_audio_or_require_artwork_flag(self):
        self.output.mkdir()
        vault.save_json(self.output / "failures.json", [{"id": "2", "kind": "image"}])
        result, _, _, request = self.invoke("--retry-failed", responses=[Response(PNG, content_type="image/png")])
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 1)
        self.assertFalse((self.output / "songs").exists())
        self.assertIn("2", vault.DownloadHistory(self.history).completed["image"])

    def test_filtered_retry_preserves_unselected_failures(self):
        self.output.mkdir()
        vault.save_json(self.output / "failures.json", [
            {"id": "1", "kind": "audio"}, {"id": "2", "kind": "audio"}])
        result, _, _, _ = self.invoke("--retry-failed", "--song-id", "1", responses=[Response(AUDIO)])
        self.assertEqual(result, 0)
        self.assertEqual(set(vault.load_failures(self.output / "failures.json")), {("2", "audio")})

    def test_sync_history_is_offline_and_preserves_moved_file_records(self):
        self.output.mkdir()
        vault.save_json(self.output / "catalog.json", SONGS)
        job = vault.build_jobs(SONGS[:1], self.args(), {})[0]
        job.destination.parent.mkdir()
        job.destination.write_bytes(AUDIO)
        result, _, _, request = self.invoke("--sync-history")
        self.assertEqual(result, 0)
        request.assert_not_called()
        job.destination.unlink()
        result, stdout, _, request = self.invoke("--song-id", "1")
        self.assertEqual(result, 0)
        request.assert_not_called()
        self.assertIn("1 skipped", stdout)

    def test_invalid_option_combinations_fail_before_files_or_network(self):
        combinations = [("--retry-failed", "--redownload", "1"),
                        ("--list-only", "--redownload", "1"),
                        ("--sync-history", "--search", "night"),
                        ("--list-only", "--sync-history"), ("--workers", "33")]
        for options in combinations:
            with self.subTest(options=options), self.assertRaises(SystemExit) as error:
                self.invoke(*options)
            self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.output.exists())

    def test_mp3_validation_rejects_truncation_id3_only_and_disguised_html(self):
        target = self.root / "track.mp3"
        for data in (AUDIO[:-1], b"ID3\x04\x00\x00\x00\x00\x00\x00", b"<html>error</html>", b""):
            target.write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaises(RuntimeError):
                vault.validate_media(target, "audio")
        target.write_bytes(b"ID3\x04\0\0\0\0\0\0" + AUDIO + b"TAG" + bytes(125))
        vault.validate_media(target, "audio")

    def test_invalid_audio_without_content_length_is_not_published(self):
        target = self.root / "track.mp3"
        with self.assertRaisesRegex(RuntimeError, "Truncated MP3"):
            vault.download(FakeClient(Response(AUDIO[:-1])), "https://example.invalid", target, "audio")
        self.assertFalse(target.exists())

    def test_invalid_local_track_is_forgotten_but_moved_tracks_stay_recorded(self):
        job = vault.build_jobs(SONGS[:1], self.args(), {})[0]
        job.destination.parent.mkdir(parents=True)
        job.destination.write_bytes(AUDIO)
        history = vault.DownloadHistory(self.history)
        history.record("1", "audio", job.destination)
        history.completed["audio"].add("moved")
        job.destination.write_bytes(AUDIO[:-1])
        with contextlib.redirect_stderr(io.StringIO()):
            history.scan(SONGS, self.output)
        self.assertNotIn("1", history.completed["audio"])
        self.assertIn("moved", history.completed["audio"])

    def test_legacy_history_loads_and_new_size_records_detect_truncation(self):
        vault.save_json(self.history, {"version": 1, "audio": ["1"], "image": []})
        history = vault.DownloadHistory(self.history)
        target = self.root / "track.mp3"
        target.write_bytes(AUDIO)
        history.record("1", "audio", target)
        history.save()
        restored = vault.DownloadHistory(self.history)
        target.write_bytes(FRAME * 3)
        with self.assertRaisesRegex(RuntimeError, "Invalid audio size"):
            vault.validate_media(target, "audio", restored.sizes["audio"]["1"])

    def test_changed_catalog_audio_is_downloaded_even_if_id_is_completed(self):
        job = vault.build_jobs(SONGS[:1], self.args(), {})[0]
        job.destination.parent.mkdir(parents=True)
        job.destination.write_bytes(AUDIO)
        history = vault.DownloadHistory(self.history)
        history.record("1", "audio", job.destination, "audio/one-old.mp3")
        history.save()
        replacement = AUDIO + FRAME
        result, stdout, _, request = self.invoke("--song-id", "1", responses=[Response(replacement)])
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 1)
        self.assertIn("Updated audio detected", stdout)
        self.assertEqual(job.destination.read_bytes(), replacement)
        self.assertEqual(vault.DownloadHistory(self.history).audio_sources["1"], "audio/one-v1.mp3")
        result, _, _, request = self.invoke("--song-id", "1")
        self.assertEqual(result, 0)
        request.assert_not_called()

    def test_failed_update_retains_old_source_and_is_retried_on_normal_run(self):
        job = vault.build_jobs(SONGS[:1], self.args(), {})[0]
        job.destination.parent.mkdir(parents=True)
        job.destination.write_bytes(AUDIO)
        history = vault.DownloadHistory(self.history)
        history.record("1", "audio", job.destination, "audio/one-old.mp3")
        history.save()
        result, _, _, _ = self.invoke("--song-id", "1", responses=[Response(b"bad")])
        self.assertEqual(result, 1)
        self.assertEqual(job.destination.read_bytes(), AUDIO)
        self.assertEqual(vault.DownloadHistory(self.history).audio_sources["1"], "audio/one-old.mp3")
        replacement = AUDIO + FRAME
        result, _, _, request = self.invoke("--song-id", "1", responses=[Response(replacement)])
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(job.destination.read_bytes(), replacement)

    def test_legacy_history_uses_previous_catalog_for_first_change_check(self):
        self.output.mkdir()
        vault.save_json(self.history, {"version": 1, "audio": ["1"], "image": []})
        vault.save_json(self.output / "catalog.json", [{"id": "1", "song_path": "audio/old.mp3"}])
        result, stdout, _, request = self.invoke("--song-id", "1", responses=[Response(AUDIO)])
        self.assertEqual(result, 0)
        self.assertIn("Earlier changes may not be detectable", stdout)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(vault.DownloadHistory(self.history).audio_sources["1"], "audio/one-v1.mp3")

    def test_baseline_for_unselected_legacy_tracks_is_not_lost(self):
        self.output.mkdir()
        vault.save_json(self.history, {"version": 1, "audio": ["1", "2"], "image": []})
        vault.save_json(self.output / "catalog.json", [{"id": "1", "song_path": "audio/one-v1.mp3"},
                                                         {"id": "2", "song_path": "audio/old-two.mp3"}])
        result, _, _, request = self.invoke("--song-id", "1")
        self.assertEqual(result, 0)
        request.assert_not_called()
        self.assertEqual(vault.DownloadHistory(self.history).audio_sources["2"], "audio/old-two.mp3")
        result, _, _, request = self.invoke("--song-id", "2", responses=[Response(AUDIO)])
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 1)

    def test_legacy_without_previous_catalog_warns_and_tracks_future_changes(self):
        self.output.mkdir()
        vault.save_json(self.history, {"version": 1, "audio": ["1"], "image": []})
        result, stdout, _, request = self.invoke("--song-id", "1")
        self.assertEqual(result, 0)
        self.assertIn("Earlier changes may not be detectable", stdout)
        request.assert_not_called()
        self.assertEqual(vault.DownloadHistory(self.history).audio_sources["1"], "audio/one-v1.mp3")

    def test_update_size_check_finds_moved_file_in_output_root(self):
        self.output.mkdir()
        job = vault.build_jobs(SONGS[:1], self.args(), {})[0]
        moved = self.output / job.destination.name
        moved.write_bytes(AUDIO)
        history = vault.DownloadHistory(self.history)
        history.completed["audio"].add("1")
        history.filenames["1"] = job.destination.stem
        history.audio_sources["1"] = SONGS[0]["song_path"]
        history.save()
        replacement = AUDIO + FRAME
        with patch.object(vault, "remote_audio_size", return_value=len(replacement)) as head:
            result, stdout, _, request = self.invoke("--song-id", "1", "--check-updates",
                                                     responses=[Response(replacement)])
        self.assertEqual(result, 0)
        self.assertEqual(head.call_count, 1)
        self.assertEqual(request.call_count, 1)
        self.assertIn("Audio size changed", stdout)
        self.assertEqual(moved.read_bytes(), AUDIO)
        self.assertEqual(job.destination.read_bytes(), replacement)

    def test_update_size_check_leaves_same_size_file_alone(self):
        self.output.mkdir()
        job = vault.build_jobs(SONGS[:1], self.args(), {})[0]
        (self.output / job.destination.name).write_bytes(AUDIO)
        history = vault.DownloadHistory(self.history)
        history.completed["audio"].add("1")
        history.filenames["1"] = job.destination.stem
        history.audio_sources["1"] = SONGS[0]["song_path"]
        history.save()
        with patch.object(vault, "remote_audio_size", return_value=len(AUDIO)):
            result, _, _, request = self.invoke("--song-id", "1", "--check-updates")
        self.assertEqual(result, 0)
        request.assert_not_called()

    def test_update_size_check_reports_unknown_size_and_retains_history(self):
        self.output.mkdir()
        vault.save_json(self.history, {"version": 1, "audio": ["1"], "image": [],
                                      "audio_sources": {"1": SONGS[0]["song_path"]}})
        with patch.object(vault, "remote_audio_size") as head:
            result, _, stderr, request = self.invoke("--song-id", "1", "--check-updates")
        self.assertEqual(result, 1)
        self.assertIn("no local or recorded size", stderr)
        head.assert_not_called()
        request.assert_not_called()
        self.assertIn("1", vault.DownloadHistory(self.history).completed["audio"])

    def test_update_check_errors_are_visible_without_forcing_download(self):
        self.output.mkdir()
        job = vault.build_jobs(SONGS[:1], self.args(), {})[0]
        (self.output / job.destination.name).write_bytes(AUDIO)
        vault.save_json(self.history, {"version": 1, "audio": ["1"], "image": [],
                                      "audio_sources": {"1": SONGS[0]["song_path"]},
                                      "filenames": {"1": job.destination.stem}})
        with patch.object(vault, "remote_audio_size", side_effect=OSError("offline")):
            result, _, stderr, request = self.invoke("--song-id", "1", "--check-updates")
        self.assertEqual(result, 1)
        self.assertIn("Cannot check ID 1: offline", stderr)
        request.assert_not_called()

    def test_remote_size_uses_head_without_reading_the_audio_body(self):
        client = vault.Client(delay=0, retries=0)
        response = Response(b"", Content_Length="12345")
        with patch.object(client, "open", return_value=response) as request:
            self.assertEqual(vault.remote_audio_size(client, "1"), 12345)
        self.assertEqual(request.call_args.kwargs["method"], "HEAD")

    def test_interrupted_read_retains_partial_and_resumes(self):
        target = self.root / "track.mp3"
        client = vault.Client(delay=0, retries=0)

        class InterruptingResponse(Response):
            def read(self, size=-1):
                data = super().read(min(size, len(FRAME)))
                client.stop.set()
                return data

        with patch.object(client, "open", return_value=InterruptingResponse(
                AUDIO, ETag='"v1"', Content_Length=str(len(AUDIO)))):
            with self.assertRaises(vault.DownloadCancelled):
                vault.download(client, "https://example.invalid", target, "audio")
        self.assertFalse(target.exists())
        self.assertEqual(target.with_name("track.mp3.part").read_bytes(), FRAME)
        resumed = FakeClient(Response(FRAME, 206, Content_Range="bytes 417-833/834"))
        vault.download(resumed, "https://example.invalid", target, "audio")
        self.assertEqual(resumed.headers["Range"], "bytes=417-")
        self.assertEqual(target.read_bytes(), AUDIO)

    def test_two_processes_cannot_share_lock_and_lock_releases_after_crash(self):
        path = self.root / "history.lock"
        child = ("import sys; from pathlib import Path; from download_juicewrldvault import RunLock\n"
                 "try:\n with RunLock(Path(sys.argv[1])): pass\n"
                 "except RuntimeError: sys.exit(9)\n")
        with vault.RunLock(path):
            blocked = subprocess.run([sys.executable, "-c", child, str(path)], capture_output=True, timeout=10)
            self.assertEqual(blocked.returncode, 9, blocked.stderr.decode())
        released = subprocess.run([sys.executable, "-c", child, str(path)], capture_output=True, timeout=10)
        self.assertEqual(released.returncode, 0, released.stderr.decode())
        crash = ("import os,sys; from pathlib import Path; from download_juicewrldvault import RunLock; "
                 "lock=RunLock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)")
        crashed = subprocess.run([sys.executable, "-c", crash, str(path)], capture_output=True, timeout=10)
        self.assertEqual(crashed.returncode, 0, crashed.stderr.decode())
        with vault.RunLock(path):
            pass

    def test_output_lock_blocks_different_history_before_catalog_request(self):
        self.output.mkdir()
        with vault.RunLock(self.output / ".download.lock"), patch.object(vault, "catalog") as request:
            argv = ["downloader", "--output", str(self.output), "--history", str(self.history)]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "Cannot lock"):
                    vault.main()
            request.assert_not_called()

    def test_shared_history_lock_blocks_different_outputs(self):
        with vault.RunLock(Path(str(self.history) + ".lock")), patch.object(vault, "catalog") as request:
            argv = ["downloader", "--output", str(self.output), "--history", str(self.history)]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "Cannot lock"):
                    vault.main()
            request.assert_not_called()

    def test_workers_execute_concurrently_with_bounded_pending_window(self):
        client = vault.Client(delay=0)
        history = vault.DownloadHistory(self.history)
        jobs = [vault.Job(str(n), "https://example.invalid", self.root / f"{n}.mp3", "audio")
                for n in range(6)]
        barrier = threading.Barrier(2)
        windows = []
        real_wait = vault.wait

        def worker(client, url, path, kind, force, progress, fresh):
            barrier.wait(timeout=5)
            path.write_bytes(AUDIO)
            progress.advance(len(AUDIO))
            return "downloaded"

        def check_window(pending, **kwargs):
            windows.append(len(pending))
            return real_wait(pending, **kwargs)

        with patch.object(vault, "download", side_effect=worker), patch.object(vault, "wait", side_effect=check_window), \
                contextlib.redirect_stdout(io.StringIO()):
            result = vault.run_downloads(client, jobs, history, self.root / "failures.json", {}, 2, {})
        self.assertEqual(result, 0)
        self.assertEqual(len(history.completed["audio"]), 6)
        self.assertLessEqual(max(windows), 4)
        self.assertEqual(vault.load_failures(self.root / "failures.json"), {})

    def test_interrupt_preserves_all_pending_jobs_for_retry(self):
        client = vault.Client(delay=0)
        history = vault.DownloadHistory(self.history)
        jobs = [vault.Job(str(n), "https://example.invalid", self.root / f"{n}.mp3", "audio")
                for n in range(10)]

        def interrupted_worker(client, *args):
            self.assertTrue(client.stop.wait(5))
            raise vault.DownloadCancelled()

        with patch.object(vault, "download", side_effect=interrupted_worker), \
                patch.object(vault, "wait", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                vault.run_downloads(client, jobs, history, self.root / "failures.json", {}, 2, {})
        self.assertTrue(client.stop.is_set())
        self.assertEqual(len(vault.load_failures(self.root / "failures.json")), 10)
        self.assertFalse(history.completed["audio"])

    def test_shared_request_pacing_applies_across_workers(self):
        client = vault.Client(delay=0.02)
        starts = []

        def transport(*args, **kwargs):
            starts.append(time.monotonic())
            return Response(AUDIO)

        with patch.object(vault, "urlopen", side_effect=transport):
            with vault.ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(client.open, "https://example.invalid") for _ in range(3)]
                for future in futures:
                    future.result().close()
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(b - a >= 0.018 for a, b in zip(starts, starts[1:])))

    def test_rate_limit_http_date_and_numeric_retry_after_share_cooldown(self):
        for code, value in ((429, formatdate(1010, usegmt=True)), (503, "10")):
            client = vault.Client(delay=0)
            headers = Message()
            headers["Retry-After"] = value
            error = HTTPError("https://example.invalid", code, "limited", headers, None)
            clock = [5.0]

            def advance(seconds):
                clock[0] += seconds

            with patch.object(vault.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(vault.time, "time", return_value=1000), \
                    patch.object(client, "wait", side_effect=advance), \
                    contextlib.redirect_stderr(io.StringIO()):
                client.backoff(0, error)
                self.assertEqual(client._cooldown_until, 15)
                # Simulate another worker arriving before the shared cooldown ends.
                clock[0] = 6
                client.pace()
                self.assertEqual(clock[0], 15)

    def test_progress_reports_speed_eta_and_separate_counts(self):
        with patch.object(vault.time, "monotonic", return_value=10):
            progress = vault.Progress(2, {"audio": 3})
        progress.advance(2 * 1024 * 1024)
        progress.finish("audio", "downloaded")
        output = io.StringIO()
        with patch.object(vault.time, "monotonic", return_value=14), contextlib.redirect_stdout(output):
            progress.report(force=True)
        self.assertIn("Songs: 1 downloaded, 3 skipped", output.getvalue())
        self.assertIn("Artwork: 0 downloaded", output.getvalue())
        self.assertIn("0.50 MiB/s | elapsed 4s | ETA 4s", output.getvalue())


if __name__ == "__main__":
    unittest.main()
