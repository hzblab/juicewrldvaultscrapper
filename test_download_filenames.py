"""Filename allocation, collision safety, and history recovery tests."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import download_juicewrldvault as vault
from test_download_juicewrldvault import AUDIO, FRAME, Response


class FilenameTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="vault_names_")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.output = self.root / "music"
        self.history = self.root / "history.json"

    def invoke(self, songs, *options, responses=None):
        argv = ["downloader", "--output", str(self.output), "--history", str(self.history),
                "--delay", "0", "--retries", "0", *options]
        with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), patch.object(vault, "catalog", return_value=songs), \
                patch.object(vault.Client, "open", side_effect=responses) as request:
            result = vault.main()
        return result, request

    def test_plain_names_number_duplicate_titles_without_ids(self):
        songs = [{"id": "9991", "title": "Same", "author": "Artist"},
                 {"id": "9992", "title": "Same", "author": "Artist"},
                 {"id": "9993", "title": "same", "author": "artist"}]
        names = vault.allocate_filenames(songs)
        self.assertEqual(names, {"9991": "Same - Artist", "9992": "Same - Artist (2)",
                                 "9993": "same - artist (3)"})
        self.assertFalse(any(song_id in names[song_id] for song_id in names))

    def test_filenames_stay_stable_across_filtered_runs_and_new_catalog_entries(self):
        songs = [{"id": "20", "title": "Same", "author": "Artist", "song_path": "20.mp3"},
                 {"id": "30", "title": "Same", "author": "Artist", "song_path": "30.mp3"}]
        result, _ = self.invoke(songs, "--song-id", "30", responses=[Response(AUDIO)])
        self.assertEqual(result, 0)
        history = vault.DownloadHistory(self.history)
        self.assertEqual(history.filenames, {"20": "Same - Artist", "30": "Same - Artist (2)"})
        self.assertEqual((self.output / "songs" / "Same - Artist (2).mp3").read_bytes(), AUDIO)
        expanded = [{"id": "10", "title": "Same", "author": "Artist", "song_path": "10.mp3"}] + songs
        result, _ = self.invoke(expanded, "--song-id", "10", responses=[Response(AUDIO + FRAME)])
        self.assertEqual(result, 0)
        self.assertEqual((self.output / "songs" / "Same - Artist (3).mp3").read_bytes(), AUDIO + FRAME)
        self.assertEqual((self.output / "songs" / "Same - Artist (2).mp3").read_bytes(), AUDIO)
        self.assertEqual(vault.DownloadHistory(self.history).filenames["30"], "Same - Artist (2)")

    def test_preexisting_untracked_file_is_not_overwritten(self):
        self.output.joinpath("songs").mkdir(parents=True)
        old = self.output / "songs" / "Same - Artist.mp3"
        old.write_bytes(AUDIO)
        song = {"id": "77", "title": "Same", "author": "Artist", "song_path": "77.mp3"}
        result, _ = self.invoke([song], responses=[Response(AUDIO + FRAME)])
        self.assertEqual(result, 0)
        self.assertEqual(old.read_bytes(), AUDIO)
        self.assertEqual((old.parent / "Same - Artist (2).mp3").read_bytes(), AUDIO + FRAME)

    def test_history_recognizes_new_names_and_older_id_names(self):
        folder = self.output / "songs"
        folder.mkdir(parents=True)
        songs = [{"id": "1", "title": "One", "author": "Artist"},
                 {"id": "2", "title": "Two", "author": "Artist"}]
        new = folder / "One - Artist.mp3"
        old = folder / f"Old title [{vault.song_identifier('2')}].mp3"
        new.write_bytes(AUDIO)
        old.write_bytes(AUDIO)
        history = vault.DownloadHistory(self.history)
        history.filenames["1"] = "One - Artist"
        history.completed["audio"].add("1")
        found = history.scan(songs, self.output)
        self.assertEqual(found["audio"], 2)
        self.assertEqual(history.completed["audio"], {"1", "2"})
        self.assertTrue(new.exists())
        self.assertTrue(old.exists())

    def test_sync_history_does_not_guess_ambiguous_duplicate_names(self):
        folder = self.output / "songs"
        folder.mkdir(parents=True)
        songs = [{"id": "1", "title": "Same", "author": "Artist"},
                 {"id": "2", "title": "Same", "author": "Artist"}]
        (folder / "Same - Artist.mp3").write_bytes(AUDIO)
        vault.save_json(self.output / "catalog.json", songs)
        with patch("sys.argv", ["downloader", "--output", str(self.output), "--history", str(self.history),
                                "--sync-history"]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "No valid completed songs"):
                vault.main()
        self.assertFalse(self.history.exists())

    def test_legacy_ids_and_new_filenames_are_both_supported_in_size_checks(self):
        folder = self.output / "songs"
        folder.mkdir(parents=True)
        songs = [{"id": "1"}, {"id": "2"}]
        new = folder / "One - Artist.wav"
        old = folder / f"Old [{vault.song_identifier('2')}].mp3"
        new.write_bytes(b"a" * 25)
        old.write_bytes(b"b" * 30)
        history = vault.DownloadHistory(self.history)
        history.completed["audio"].update({"1", "2"})
        history.filenames["1"] = "One - Artist"
        sizes = vault.local_audio_sizes(songs, history, self.output)
        self.assertEqual(sizes, {"1": {25}, "2": {30}})

    def test_invalid_or_duplicate_mapping_cannot_overwrite_other_paths(self):
        for mapping in ({"1": "../outside"}, {"1": "Song", "2": "song"}, {"1": "CON"}):
            vault.save_json(self.history, {"version": 1, "audio": [], "image": [], "filenames": mapping})
            with self.subTest(mapping=mapping), self.assertRaisesRegex(RuntimeError, "invalid or conflicting"):
                vault.DownloadHistory(self.history)


if __name__ == "__main__":
    unittest.main()
