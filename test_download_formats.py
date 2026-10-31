"""Offline tests for original MP3/WAV formats and metadata preservation."""

import argparse
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

import download_juicewrldvault as vault
from test_download_juicewrldvault import AUDIO, FakeClient, Response


def riff_chunk(name, payload):
    return name + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) % 2 else b"")


def wav_with_tags():
    fmt = struct.pack("<HHIIHH", 1, 2, 44100, 176400, 4, 16)
    info = b"INFO" + riff_chunk(b"INAM", b"Original title\0") + riff_chunk(b"IART", b"Original artist\0")
    body = b"WAVE" + riff_chunk(b"fmt ", fmt) + riff_chunk(b"LIST", info) + riff_chunk(b"data", b"\x01\x00" * 32)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def mp3_with_tags_and_suffix():
    value = b"\x00Original title\x00"
    tag = b"TIT2" + len(value).to_bytes(4, "big") + b"\0\0" + value
    n = len(tag)
    size = bytes([(n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127])
    return b"ID3\x03\0\0" + size + tag + AUDIO + b"{}"


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="vault_format_")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.url = "https://example.invalid/song"

    def test_wav_download_uses_wav_extension_and_preserves_all_bytes_and_tags(self):
        data = wav_with_tags()
        guessed_mp3 = self.root / "Song - Artist [1-id].mp3"
        # The catalog or server MIME type can be wrong: the bytes decide the format.
        result = vault.download(FakeClient(Response(data, content_type="audio/mpeg",
                                                    Content_Length=str(len(data)))),
                                self.url, guessed_mp3, "audio")
        self.assertEqual(result, "downloaded")
        self.assertEqual(result.path, guessed_mp3.with_suffix(".wav"))
        self.assertFalse(guessed_mp3.exists())
        self.assertEqual(result.path.read_bytes(), data)
        self.assertIn(b"Original title", result.path.read_bytes())
        self.assertIn(b"Original artist", result.path.read_bytes())
        with wave.open(str(result.path), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate()), (2, 44100))

    def test_mp3_download_uses_mp3_extension_and_preserves_tags_and_trailing_bytes(self):
        data = mp3_with_tags_and_suffix()
        guessed_wav = self.root / "Song - Artist [1-id].wav"
        result = vault.download(FakeClient(Response(data, content_type="audio/wav")),
                                self.url, guessed_wav, "audio")
        self.assertEqual(result.path, guessed_wav.with_suffix(".mp3"))
        self.assertEqual(result.path.read_bytes(), data)
        self.assertFalse(guessed_wav.exists())
        self.assertEqual(vault.validate_media(result.path, "audio"), len(data))

    def test_catalog_title_and_author_determine_filename_without_altering_metadata(self):
        song = {"id": "3789", "title": "DENIAL </3 (demo) [RAW]", "author": "Juice WRLD",
                "song_path": "uploads/denial.wav"}
        args = argparse.Namespace(output=self.root, redownload=None, retry_failed=False, artwork=False)
        job = vault.build_jobs([song], args, {})[0]
        self.assertEqual(job.destination.name, "DENIAL __3 (demo) [RAW] - Juice WRLD.wav")
        self.assertEqual(song["title"], "DENIAL </3 (demo) [RAW]")
        data = wav_with_tags()
        result = vault.download(FakeClient(Response(data, content_type="audio/wav")), job.url,
                                job.destination, "audio")
        self.assertEqual(result.path.read_bytes(), data)
        self.assertEqual(job.source, song["song_path"])

    def test_legacy_wav_with_mp3_name_is_recognized_without_renaming(self):
        songs_dir = self.root / "songs"
        songs_dir.mkdir()
        song = {"id": "3789", "song_path": "audio/test.wav"}
        legacy = songs_dir / f"Old title [{vault.song_identifier('3789')}].mp3"
        data = wav_with_tags()
        legacy.write_bytes(data)
        history = vault.DownloadHistory(self.root / "history.json")
        self.assertEqual(history.scan([song], self.root)["audio"], 1)
        self.assertIn("3789", history.completed["audio"])
        self.assertTrue(legacy.exists())
        self.assertFalse(legacy.with_suffix(".wav").exists())
        self.assertEqual(legacy.read_bytes(), data)

    def test_existing_wav_file_is_recorded_in_history_and_size_checks(self):
        songs_dir = self.root / "songs"
        songs_dir.mkdir()
        song = {"id": "1", "song_path": "audio/test.wav"}
        target = songs_dir / f"Title [{vault.song_identifier('1')}].wav"
        target.write_bytes(wav_with_tags())
        history = vault.DownloadHistory(self.root / "history.json")
        self.assertEqual(history.scan([song], self.root)["audio"], 1)
        self.assertEqual(vault.local_audio_sizes([song], history, self.root)["1"], {target.stat().st_size})

    def test_wav_file_in_output_root_is_used_for_update_checks(self):
        song = {"id": "1"}
        target = self.root / f"Moved [{vault.song_identifier('1')}].wav"
        target.write_bytes(wav_with_tags())
        history = vault.DownloadHistory(self.root / "history.json")
        history.completed["audio"].add("1")
        self.assertEqual(vault.local_audio_sizes([song], history, self.root)["1"], {target.stat().st_size})

    def test_truncated_wav_does_not_replace_existing_file(self):
        target = self.root / "song.wav"
        original = wav_with_tags()
        target.write_bytes(original)
        invalid = original[:-3]
        with self.assertRaisesRegex(RuntimeError, "Truncated WAV"):
            vault.download(FakeClient(Response(invalid, content_type="audio/wav")),
                           self.url, target, "audio", force=True)
        self.assertEqual(target.read_bytes(), original)

    def test_unrecognized_audio_is_not_published_with_an_mp3_extension(self):
        target = self.root / "song.mp3"
        with self.assertRaisesRegex(RuntimeError, "Unsupported audio format"):
            vault.download(FakeClient(Response(b"unknown" + bytes(64), content_type="application/octet-stream")),
                           self.url, target, "audio")
        self.assertFalse(target.exists())

    def test_flac_and_opus_are_saved_with_their_original_format_and_bytes(self):
        def ogg_page(payload, number):
            return (b"OggS" + b"\0" + (b"\x02" if number == 0 else b"\x04")
                    + bytes(8) + (1).to_bytes(4, "little") + number.to_bytes(4, "little")
                    + bytes(4) + b"\x01" + bytes([len(payload)]) + payload)
        flac = b"fLaC" + b"\x80\x00\x00\x22" + bytes(34) + b"\xff\xf8audio"
        opus = ogg_page(b"OpusHead" + bytes(11), 0) + ogg_page(b"OpusTags Original title", 1)
        for ext, data in ((".flac", flac), (".opus", opus)):
            with self.subTest(extension=ext):
                guessed = self.root / f"song-{ext[1:]}.mp3"
                result = vault.download(FakeClient(Response(data, content_type="application/octet-stream")),
                                        self.url, guessed, "audio")
                self.assertEqual(result.path.suffix, ext)
                self.assertEqual(result.path.read_bytes(), data)
                self.assertFalse(guessed.exists())

    def test_m4a_and_mp4_preserve_catalog_extension_and_container_bytes(self):
        def box(kind, payload):
            return struct.pack(">I", len(payload) + 8) + kind + payload
        data = box(b"ftyp", b"isom" + bytes(4) + b"mp42") + box(b"moov", b"tags") + box(b"mdat", b"audio")
        for ext in (".m4a", ".mp4"):
            with self.subTest(extension=ext):
                target = self.root / f"song{ext}"
                result = vault.download(FakeClient(Response(data, content_type="audio/mp4")),
                                        self.url, target, "audio")
                self.assertEqual(result.path, target)
                self.assertEqual(target.read_bytes(), data)

    def test_truncated_extra_formats_are_rejected(self):
        cases = (b"fLaC" + b"\x80\x00\x00\x22" + bytes(10),
                 b"OggS" + bytes(10),
                 struct.pack(">I", 100) + b"ftyp" + b"isom" + bytes(4))
        for n, data in enumerate(cases):
            with self.subTest(n=n), self.assertRaises(RuntimeError):
                vault.download(FakeClient(Response(data)), self.url, self.root / f"song-{n}.mp3", "audio")

    def test_conflicting_existing_destination_is_not_overwritten_when_not_forced(self):
        guessed = self.root / "song.mp3"
        actual = self.root / "song.wav"
        old = wav_with_tags()
        actual.write_bytes(old)
        result = vault.download(FakeClient(Response(old, content_type="audio/wav")),
                                self.url, guessed, "audio")
        self.assertEqual(result, "skipped")
        self.assertEqual(result.path, actual)
        self.assertEqual(actual.read_bytes(), old)

    def test_invalid_conflicting_destination_is_not_overwritten(self):
        guessed = self.root / "song.mp3"
        actual = self.root / "song.wav"
        actual.write_bytes(b"existing but invalid")
        with self.assertRaisesRegex(RuntimeError, "not overwritten"):
            vault.download(FakeClient(Response(wav_with_tags(), content_type="audio/wav")),
                           self.url, guessed, "audio")
        self.assertEqual(actual.read_bytes(), b"existing but invalid")

    def test_download_result_records_detected_destination_in_history(self):
        song = {"id": "3789", "title": "Song", "author": "Artist", "song_path": "audio/song.mp3"}
        args = argparse.Namespace(output=self.root, redownload=None, retry_failed=False, artwork=False)
        job = vault.build_jobs([song], args, {})[0]
        history = vault.DownloadHistory(self.root / "history.json")
        client = vault.Client(delay=0, retries=0)
        data = wav_with_tags()
        with patch.object(client, "open", return_value=Response(data, content_type="audio/wav")), \
                contextlib.redirect_stdout(io.StringIO()):
            result = vault.run_downloads(client, [job], history, self.root / "failures.json", {}, 1, {})
        self.assertEqual(result, 0)
        self.assertIn("3789", history.completed["audio"])
        self.assertEqual(history.sizes["audio"]["3789"], len(data))
        self.assertEqual(job.destination.with_suffix(".wav").read_bytes(), data)
        self.assertEqual(json.loads((self.root / "failures.json").read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
