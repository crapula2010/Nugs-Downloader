import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main
import nugs_credentials
import server


PACKAGING_SPEC = importlib.util.spec_from_file_location(
    "build_packages", ROOT / "packaging" / "build_packages.py"
)
build_packages = importlib.util.module_from_spec(PACKAGING_SPEC)
PACKAGING_SPEC.loader.exec_module(build_packages)


class MainDownloaderChangesTest(unittest.TestCase):
    def stream_params(self):
        return main.StreamParams(
            SubscriptionID="sub",
            SubCostplanIDAccessList="plan",
            UserID="user",
            StartStamp="1",
            EndStamp="2",
        )

    def test_watch_release_urls_are_accepted(self):
        self.assertEqual(main.check_url("https://play.nugs.net/watch/release/45613"), ("45613", 0))
        self.assertEqual(main.check_url("https://play.nugs.net/release/45613"), ("45613", 0))

    def test_jsonp_stream_response_is_parsed(self):
        payload = main.parse_stream_response('jsonp_nn0({"streamLink":"https://cdn.example/master.m3u8"});')
        self.assertEqual(payload["streamLink"], "https://cdn.example/master.m3u8")

    def test_parse_timestamps_treats_subscription_dates_as_utc(self):
        start, end = main.parse_timestamps("12/29/2025 12:12:07", "12/29/2026 12:12:07")
        self.assertEqual(start, "1767010327")
        self.assertEqual(end, "1798546327")

    def test_parse_cfg_applies_output_formats_and_track_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "email": "u@example.com",
                        "password": "secret",
                        "format": 4,
                        "videoFormat": 5,
                        "outPath": str(Path(tmp) / "out"),
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "main.py",
                "--audio-output-format",
                "mp3",
                "--video-output-format",
                "mp4",
                "--track-ids",
                "11,22",
                "https://play.nugs.net/watch/release/45613",
            ]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"NUGS_CONFIG_PATH": str(cfg_path)}):
                cfg = main.parse_cfg()

        self.assertEqual(cfg.audioOutputFormat, "mp3")
        self.assertEqual(cfg.videoOutputFormat, "mp4")
        self.assertEqual(cfg.trackIds, [11, 22])
        self.assertEqual(cfg.urls, ["https://play.nugs.net/watch/release/45613"])

    def test_read_config_uses_defaults_when_config_json_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "missing-config.json"
            with patch.dict(os.environ, {"NUGS_CONFIG_PATH": str(cfg_path)}):
                cfg = main.read_config()

        self.assertEqual(cfg.format, 2)
        self.assertEqual(cfg.videoFormat, 2)
        self.assertEqual(cfg.audioOutputFormat, "source")
        self.assertEqual(cfg.videoOutputFormat, "mkv")
        self.assertTrue(cfg.useFfmpegEnvVar)

    def test_parse_cfg_defaults_to_path_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "missing-config.json"
            argv = ["main.py", "https://play.nugs.net/release/45613"]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"NUGS_CONFIG_PATH": str(cfg_path)}):
                cfg = main.parse_cfg()

        self.assertEqual(cfg.ffmpegNameStr, "ffmpeg")

    def test_parse_cfg_falls_back_to_path_ffmpeg_when_local_binary_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({"useFfmpegEnvVar": False}), encoding="utf-8")
            argv = ["main.py", "https://play.nugs.net/release/45613"]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"NUGS_CONFIG_PATH": str(cfg_path)}):
                cfg = main.parse_cfg()

        self.assertEqual(cfg.ffmpegNameStr, "ffmpeg")

    def test_parse_cfg_can_still_use_local_ffmpeg_when_configured_and_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({"useFfmpegEnvVar": False}), encoding="utf-8")
            argv = ["main.py", "https://play.nugs.net/release/45613"]
            with patch.object(sys, "argv", argv), patch.object(Path, "exists", return_value=True), patch.dict(
                os.environ, {"NUGS_CONFIG_PATH": str(cfg_path)}
            ):
                cfg = main.parse_cfg()

        self.assertEqual(cfg.ffmpegNameStr, "./ffmpeg")

    def test_audio_conversion_invokes_ffmpeg_and_removes_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "song.aac"
            source.write_text("audio", encoding="utf-8")
            with patch.object(main, "run_ffmpeg") as run_ffmpeg:
                out = main.convert_audio_file(source, "mp3", "ffmpeg")

        self.assertEqual(out.name, "song.mp3")
        self.assertFalse(source.exists())
        self.assertIn("libmp3lame", run_ffmpeg.call_args.args[0])

    def test_video_package_uses_requested_container_path(self):
        with patch.object(main, "run_ffmpeg") as run_ffmpeg:
            main.package_video_file("/tmp/show.ts", "/tmp/show.mp4", "ffmpeg")

        self.assertEqual(run_ffmpeg.call_args.args[0][-1], "/tmp/show.mp4")

    def test_album_audio_only_skips_video_download(self):
        cfg = main.Config(outPath=tempfile.mkdtemp(), skipVideos=True)
        meta = {
            "artistName": "Band",
            "containerInfo": "Show",
            "products": [{"formatStr": "VIDEO ON DEMAND", "skuID": 123}],
            "songs": [{"trackID": 1, "songTitle": "Song"}],
        }

        with patch.object(main, "process_track") as process_track, patch.object(main, "video") as video:
            main.album("", cfg, self.stream_params(), meta)

        process_track.assert_called_once()
        video.assert_not_called()

    def test_album_video_only_skips_audio_tracks(self):
        cfg = main.Config(outPath=tempfile.mkdtemp(), forceVideo=True)
        meta = {
            "artistName": "Band",
            "containerInfo": "Show",
            "containerID": 45613,
            "products": [{"formatStr": "VIDEO ON DEMAND", "skuID": 123}],
            "songs": [{"trackID": 1, "songTitle": "Song"}],
        }

        with patch.object(main, "process_track") as process_track, patch.object(main, "video") as video:
            main.album("", cfg, self.stream_params(), meta)

        process_track.assert_not_called()
        video.assert_called_once()

    def test_album_video_only_does_not_fall_back_to_audio_when_no_video_exists(self):
        cfg = main.Config(outPath=tempfile.mkdtemp(), forceVideo=True)
        meta = {
            "artistName": "Band",
            "containerInfo": "Audio Show",
            "products": [{"formatStr": "FLAC", "skuID": 123}],
            "songs": [{"trackID": 1, "songTitle": "Song"}],
        }

        with patch.object(main, "process_track") as process_track, patch.object(main, "video") as video:
            main.album("", cfg, self.stream_params(), meta)

        process_track.assert_not_called()
        video.assert_not_called()

    def test_playlist_video_only_does_not_download_audio_tracks(self):
        cfg = main.Config(outPath=tempfile.mkdtemp(), forceVideo=True)

        with patch.object(main, "get_plist_meta") as get_plist_meta, patch.object(main, "process_track") as process_track:
            main.playlist("playlist", "legacy", cfg, self.stream_params(), False)

        get_plist_meta.assert_not_called()
        process_track.assert_not_called()

    def test_video_audio_only_returns_before_fetching_metadata(self):
        cfg = main.Config(outPath=tempfile.mkdtemp(), skipVideos=True)

        with patch.object(main, "get_album_meta") as get_album_meta:
            main.video("45613", "", cfg, self.stream_params(), None, False)

        get_album_meta.assert_not_called()

    def test_config_path_uses_platform_friendly_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "portable-config.json"
            with patch.dict(os.environ, {"NUGS_CONFIG_PATH": str(cfg_path)}):
                self.assertEqual(main.get_config_path(), cfg_path)

    def test_ffmpeg_failure_reports_stderr(self):
        class FakeProc:
            returncode = 1

            def communicate(self):
                return b"", b"ffmpeg missing"

        with patch.object(subprocess, "Popen", return_value=FakeProc()):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg missing"):
                main.run_ffmpeg(["ffmpeg", "-version"])


class ServerChangesTest(unittest.TestCase):
    def setUp(self):
        server.jobs.clear()
        server.pending_queue.clear()
        server.bulk_tasks.clear()
        server.bulk_cancel_event.clear()
        server.catalog_artists_cache = None
        server.catalog_releases_cache.clear()
        server.catalog_artist_media_cache.clear()
        server.queue_paused = False
        server.max_concurrent_jobs = 2

    def test_make_cmd_covers_modes_formats_output_and_tracks(self):
        req = server.DownloadRequest(
            urls=["https://play.nugs.net/watch/release/45613"],
            format=3,
            video_format=4,
            audio_output_format="flac",
            video_output_format="mp4",
            track_ids=[101, 202],
            out_path="/tmp/nugs",
            download_audio=True,
            download_video=False,
        )

        cmd = server._make_cmd(req)

        self.assertIn("--skip-videos", cmd)
        self.assertIn("--audio-output-format", cmd)
        self.assertIn("flac", cmd)
        self.assertIn("--video-output-format", cmd)
        self.assertIn("mp4", cmd)
        self.assertIn("--track-ids", cmd)
        self.assertIn("101,202", cmd)
        self.assertIn("https://play.nugs.net/release/45613", cmd)

    def test_make_cmd_uses_worker_mode_when_packaged(self):
        req = server.DownloadRequest(urls=["https://play.nugs.net/release/1"])

        with patch.object(server.sys, "frozen", True, create=True):
            cmd = server._make_cmd(req)

        self.assertEqual(cmd[:2], [server.PYTHON, "--nugs-worker"])
        self.assertNotIn(str(server.DOWNLOADER_SCRIPT), cmd)
        self.assertNotIn("-u", cmd)

    def test_launcher_worker_mode_does_not_clear_login_or_open_window(self):
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('WORKER_ARG = "--nugs-worker"', launcher)
        self.assertIn("if WORKER_ARG in sys.argv:", launcher)
        self.assertIn("sys.argv.remove(WORKER_ARG)", launcher)
        self.assertIn("downloader_main.main()", launcher)
        main_body = launcher[launcher.index("def main() -> int:") :]
        self.assertLess(main_body.index("if WORKER_ARG in sys.argv:"), main_body.index("destroy_saved_login()"))

    def test_make_cmd_preserves_packaged_output_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "Nugs Output"
            req = server.DownloadRequest(urls=["https://play.nugs.net/release/1"], out_path=str(out_dir))

            cmd = server._make_cmd(req)

        self.assertIn("-o", cmd)
        self.assertIn(str(out_dir), cmd)

    def test_make_cmd_uses_default_output_path_when_missing(self):
        with patch.object(server, "_default_out_path", return_value="/tmp/default-nugs-out"):
            cmd = server._make_cmd(server.DownloadRequest(urls=["https://play.nugs.net/release/1"]))

        self.assertIn("-o", cmd)
        self.assertIn("/tmp/default-nugs-out", cmd)

    def test_make_cmd_video_only_forces_video_and_does_not_skip_videos(self):
        req = server.DownloadRequest(
            urls=["https://play.nugs.net/watch/release/45613"],
            download_audio=False,
            download_video=True,
        )

        cmd = server._make_cmd(req)

        self.assertIn("--force-video", cmd)
        self.assertNotIn("--skip-videos", cmd)

    def test_make_cmd_audio_only_skips_videos_and_does_not_force_video(self):
        req = server.DownloadRequest(
            urls=["https://play.nugs.net/watch/release/45613"],
            download_audio=True,
            download_video=False,
        )

        cmd = server._make_cmd(req)

        self.assertIn("--skip-videos", cmd)
        self.assertNotIn("--force-video", cmd)

    def test_release_summary_detects_audio_video_and_fields(self):
        summary = server._release_summary(
            {
                "containerID": 45613,
                "artistID": 111,
                "artistName": "The Disco Biscuits",
                "containerInfo": "Show",
                "performanceDateYear": 2025,
                "productFormatList": [{"formatStr": "VIDEO ON DEMAND"}, {"formatStr": "FLAC"}],
                "songs": [{"trackID": 1}],
            }
        )

        self.assertTrue(summary["has_audio"])
        self.assertTrue(summary["has_video"])
        self.assertEqual(summary["url"], "https://play.nugs.net/release/45613")
        self.assertEqual(summary["year"], "2025")

    def test_request_summary_includes_show_mode_and_format_details(self):
        req = server.DownloadRequest(
            urls=["https://play.nugs.net/watch/release/45613"],
            format=2,
            video_format=3,
            audio_output_format="flac",
            video_output_format="mp4",
            track_ids=[1, 2],
            download_audio=False,
            download_video=True,
        )

        with patch.object(
            server,
            "_cached_release_detail",
            return_value={
                "release_id": 45613,
                "artist_name": "The Disco Biscuits",
                "title": "Show",
                "date": "2025-01-01",
                "venue": "Venue",
            },
        ):
            summary = server._request_summary(req)

        self.assertEqual(summary["release_id"], 45613)
        self.assertEqual(summary["artist_name"], "The Disco Biscuits")
        self.assertEqual(summary["mode"], "Video only")
        self.assertEqual(summary["video_quality"], "1080p")
        self.assertEqual(summary["video_output_format"], "mp4")
        self.assertNotIn("audio_quality", summary)
        self.assertNotIn("audio_output_format", summary)
        self.assertEqual(summary["track_count"], 2)

        audio_req = server.DownloadRequest(
            urls=["https://play.nugs.net/watch/release/45613"],
            format=2,
            video_format=3,
            audio_output_format="flac",
            video_output_format="mp4",
            download_audio=True,
            download_video=False,
        )
        with patch.object(server, "_cached_release_detail", return_value={}):
            audio_summary = server._request_summary(audio_req)

        self.assertEqual(audio_summary["mode"], "Audio only")
        self.assertEqual(audio_summary["audio_quality"], "FLAC 16/44.1")
        self.assertEqual(audio_summary["audio_output_format"], "flac")
        self.assertNotIn("video_quality", audio_summary)
        self.assertNotIn("video_output_format", audio_summary)

    def test_artist_catalog_excludes_zero_availability_artists(self):
        class FakeResponse:
            status_code = 200
            reason = "OK"

            def json(self):
                return {
                    "Response": {
                        "artists": [
                            {"artistID": 1, "artistName": "Empty", "numShows": 0, "numAlbums": 0},
                            {"artistID": 2, "artistName": "Ready", "numShows": 1, "numAlbums": 0},
                        ]
                    }
                }

        with patch.object(server.nugs.session, "get", return_value=FakeResponse()):
            artists = server._fetch_artists()

        self.assertEqual([artist["name"] for artist in artists], ["Ready"])

    def test_media_filter_uses_cached_ids(self):
        artists = [{"id": 1, "name": "Audio"}, {"id": 2, "name": "Video"}]
        server.catalog_artist_media_cache["audio_only"] = {2}

        self.assertEqual(server._artist_ids_for_media("audio_only", artists), {2})

    def test_video_artist_filter_uses_show_counts_without_release_probe(self):
        artists = [
            {"id": 1, "name": "Audio Only", "num_shows": 0, "num_albums": 3},
            {"id": 2, "name": "Video Band", "num_shows": 4, "num_albums": 1},
        ]

        with patch.object(server, "_fetch_artist_release_page") as fetch_page:
            matching_ids = server._artist_ids_for_media("video", artists)

        self.assertEqual(matching_ids, {2})
        fetch_page.assert_not_called()

    def test_catalog_years_are_filtered_by_media(self):
        artists = [
            {"id": 1, "name": "Audio", "num_shows": 0, "num_albums": 2},
            {"id": 2, "name": "Video", "num_shows": 3, "num_albums": 0},
        ]
        server.catalog_releases_cache[1] = [
            {"year": "2024", "has_audio": True, "has_video": False},
        ]
        server.catalog_releases_cache[2] = [
            {"year": "2025", "has_audio": False, "has_video": True},
        ]

        self.assertEqual(server._catalog_years_for_media("video", artists), ["2025"])
        self.assertEqual(server._catalog_years_for_media("audio", artists), ["2024"])

    def test_catalog_years_endpoint_returns_filtered_payload(self):
        artists = [{"id": 2, "name": "Video", "num_shows": 3, "num_albums": 0}]
        server.catalog_releases_cache[2] = [{"year": "2025", "has_audio": False, "has_video": True}]

        with patch.object(server, "_fetch_artists", return_value=artists), patch.object(
            server, "_has_session_credentials", return_value=True
        ):
            payload = server.catalog_years(media="video")

        self.assertEqual(payload, {"count": 1, "items": ["2025"]})

    def test_catalog_endpoints_are_empty_before_session_login(self):
        with patch.object(server, "_has_session_credentials", return_value=False), patch.object(
            server, "_fetch_artists"
        ) as fetch_artists, patch.object(server, "_fetch_artist_releases") as fetch_releases:
            artists = server.catalog_artists()
            years = server.catalog_years()
            releases = server.catalog_artist_releases(1)

        self.assertEqual(artists, {"count": 0, "items": []})
        self.assertEqual(years, {"count": 0, "items": []})
        self.assertEqual(releases, {"count": 0, "years": [], "items": []})
        fetch_artists.assert_not_called()
        fetch_releases.assert_not_called()

    def test_catalog_release_detail_requires_session_login(self):
        with patch.object(server, "_has_session_credentials", return_value=False):
            with self.assertRaises(server.HTTPException) as raised:
                server.catalog_release_detail(123)

        self.assertEqual(raised.exception.status_code, 401)

    def test_credentials_update_keeps_existing_password_when_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({"email": "old@example.com", "password": "saved"}), encoding="utf-8")
            env = {
                "NUGS_CREDENTIALS_PATH": str(Path(tmp) / "nugs_credentials.enc"),
                "NUGS_CREDENTIALS_KEY_PATH": str(Path(tmp) / "nugs_credentials.key"),
            }
            with patch.object(server, "_config_path", return_value=cfg_path), patch.dict(os.environ, env):
                result = server.update_credentials(server.CredentialsUpdate(email="new@example.com", password=""))
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                encrypted = nugs_credentials.read_credentials(cfg_path)

        self.assertNotIn("email", data)
        self.assertNotIn("password", data)
        self.assertEqual(encrypted["email"], "new@example.com")
        self.assertEqual(encrypted["password"], "saved")
        self.assertTrue(result["has_password"])

    def test_credentials_status_button_state_is_based_on_encrypted_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({"email": "legacy@example.com", "password": "legacy-secret"}), encoding="utf-8")
            env = {
                "NUGS_CREDENTIALS_PATH": str(Path(tmp) / "nugs_credentials.enc"),
                "NUGS_CREDENTIALS_KEY_PATH": str(Path(tmp) / "nugs_credentials.key"),
            }
            with patch.object(server, "_config_path", return_value=cfg_path), patch.dict(os.environ, env):
                status = server.get_credentials_status()
                server.update_credentials(server.CredentialsUpdate(email="saved@example.com", password="secret"))
                saved_status = server.get_credentials_status()

        self.assertFalse(status["has_saved_credentials"])
        self.assertTrue(saved_status["has_saved_credentials"])

    def test_credentials_delete_removes_secrets_and_clears_catalog_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({"email": "legacy@example.com", "password": "legacy-secret"}), encoding="utf-8")
            env = {
                "NUGS_CREDENTIALS_PATH": str(Path(tmp) / "nugs_credentials.enc"),
                "NUGS_CREDENTIALS_KEY_PATH": str(Path(tmp) / "nugs_credentials.key"),
            }
            with patch.object(server, "_config_path", return_value=cfg_path), patch.dict(os.environ, env):
                server.catalog_artists_cache = [{"id": 1, "name": "Cached"}]
                nugs_credentials.write_credentials(cfg_path, {"email": "saved@example.com", "password": "secret"})
                status = server.delete_credentials()
                data = json.loads(cfg_path.read_text(encoding="utf-8"))

        self.assertFalse(status["has_saved_credentials"])
        self.assertFalse(Path(env["NUGS_CREDENTIALS_PATH"]).exists())
        self.assertFalse(Path(env["NUGS_CREDENTIALS_KEY_PATH"]).exists())
        self.assertNotIn("email", data)
        self.assertIsNone(server.catalog_artists_cache)

    def test_main_read_config_overlays_encrypted_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(
                json.dumps({"email": "", "password": "", "format": 2, "outPath": str(Path(tmp) / "out")}),
                encoding="utf-8",
            )
            env = {
                "NUGS_CONFIG_PATH": str(cfg_path),
                "NUGS_CREDENTIALS_PATH": str(Path(tmp) / "nugs_credentials.enc"),
                "NUGS_CREDENTIALS_KEY_PATH": str(Path(tmp) / "nugs_credentials.key"),
            }
            with patch.dict(os.environ, env):
                nugs_credentials.write_credentials(
                    cfg_path,
                    {"email": "secure@example.com", "password": "encrypted-secret"},
                )
                cfg = main.read_config()

        self.assertEqual(cfg.email, "secure@example.com")
        self.assertEqual(cfg.password, "encrypted-secret")

    def test_pause_resume_queue_updates_state(self):
        paused = server.pause_queue()
        resumed = server.resume_queue()

        self.assertTrue(paused.queue_paused)
        self.assertFalse(resumed.queue_paused)

    def test_queue_dispatch_moves_pending_to_running(self):
        server.queue_paused = True
        queued = server._enqueue_request(server.DownloadRequest(urls=["https://play.nugs.net/release/1"]))

        with patch.object(server, "_start_job") as start_job:
            server.queue_paused = False
            server._dispatch_next_jobs()

        self.assertEqual(server.jobs[queued["job_id"]].status, server.JobStatus.RUNNING)
        start_job.assert_called_once_with(server.jobs[queued["job_id"]])

    def test_run_job_success_updates_history_and_status(self):
        class FakeStream(list):
            pass

        class FakeProc:
            def __init__(self, *args, **kwargs):
                self.stdout = FakeStream(['PROGRESS {"kind":"job","percent":100}\n'])
                self.stderr = FakeStream([])
                self.returncode = 0

            def wait(self):
                return 0

        job = server.Job(id="success-job", request=server.DownloadRequest(urls=["https://play.nugs.net/release/1"]))
        server.jobs[job.id] = job

        with patch.object(server, "Popen", FakeProc):
            server._run_job(job)

        self.assertEqual(job.status, server.JobStatus.SUCCESS)
        self.assertEqual(job.exit_code, 0)
        self.assertEqual(job.progress["percent"], 100)

    def test_cancel_pending_job_removes_it_from_queue(self):
        server.max_concurrent_jobs = 0
        job = server._enqueue_request(server.DownloadRequest(urls=["https://play.nugs.net/release/1"]))

        result = server.cancel_job(job["job_id"])

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(server.jobs[job["job_id"]].status, server.JobStatus.CANCELLED)
        self.assertNotIn(job["job_id"], server.pending_queue)

    def test_delete_pending_job_removes_it_from_queue(self):
        server.max_concurrent_jobs = 0
        job = server._enqueue_request(server.DownloadRequest(urls=["https://play.nugs.net/release/1"]))

        result = server.delete_job(job["job_id"])

        self.assertEqual(result["status"], "deleted")
        self.assertNotIn(job["job_id"], server.jobs)
        self.assertNotIn(job["job_id"], server.pending_queue)

    def test_cancel_queue_cancels_all_pending_jobs(self):
        server.max_concurrent_jobs = 0
        first = server._enqueue_request(server.DownloadRequest(urls=["https://play.nugs.net/release/1"]))
        second = server._enqueue_request(server.DownloadRequest(urls=["https://play.nugs.net/release/2"]))

        result = server.cancel_queue()

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cancelled_count"], 2)
        self.assertEqual(server.jobs[first["job_id"]].status, server.JobStatus.CANCELLED)
        self.assertEqual(server.jobs[second["job_id"]].status, server.JobStatus.CANCELLED)
        self.assertEqual(list(server.pending_queue), [])

    def test_cancel_queue_marks_active_bulk_tasks_cancelled(self):
        server.bulk_tasks["bulk-1"] = {"id": "bulk-1", "status": "running", "cancel_requested": False}

        result = server.cancel_queue()

        self.assertEqual(result["cancelled_bulk_count"], 1)
        self.assertEqual(server.bulk_tasks["bulk-1"]["status"], "cancelled")
        self.assertTrue(server.bulk_tasks["bulk-1"]["cancel_requested"])

    def test_bulk_worker_stops_when_cancel_requested_before_enqueue(self):
        req = server.BulkDownloadRequest(scope="all", download_audio=True, download_video=False)
        server.bulk_tasks["bulk-1"] = {"id": "bulk-1", "status": "running", "cancel_requested": True}

        with patch.object(server, "_iter_bulk_releases", return_value=iter([{"url": "https://play.nugs.net/release/1"}])), \
             patch.object(server, "_enqueue_request") as enqueue:
            server._run_bulk_queue("bulk-1", req)

        enqueue.assert_not_called()
        self.assertEqual(server.bulk_tasks["bulk-1"]["status"], "cancelled")

    def test_delete_queue_deletes_non_running_jobs_and_keeps_running(self):
        server.max_concurrent_jobs = 1
        with patch.object(server, "_start_job"):
            running = server._enqueue_request(server.DownloadRequest(urls=["https://play.nugs.net/release/1"]))
            server.max_concurrent_jobs = 0
            pending = server._enqueue_request(server.DownloadRequest(urls=["https://play.nugs.net/release/2"]))
        server.jobs[running["job_id"]].status = server.JobStatus.RUNNING

        result = server.delete_queue()

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["skipped_running_count"], 1)
        self.assertIn(running["job_id"], server.jobs)
        self.assertNotIn(pending["job_id"], server.jobs)
        self.assertNotIn(pending["job_id"], server.pending_queue)

    def test_restart_failed_job_requeues_with_duplicate_check_disabled(self):
        job = server.Job(id="failed", request=server.DownloadRequest(urls=["https://play.nugs.net/release/1"]))
        job.status = server.JobStatus.FAILED
        server.jobs[job.id] = job

        with patch.object(server, "_start_job"):
            result = server.restart_job("failed")

        restarted = server.jobs[result["job_id"]]
        self.assertTrue(restarted.request.download_if_already_downloaded)

    def test_bulk_endpoint_returns_before_catalog_expansion_finishes(self):
        class FakeThread:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            def start(self):
                return None

        with patch.object(server.threading, "Thread", FakeThread), patch.object(server, "_make_cmd"):
            before = time.monotonic()
            response = server.create_bulk_jobs(
                server.BulkDownloadRequest(scope="all", download_audio=True, download_video=False)
            )
            elapsed = time.monotonic() - before

        self.assertLess(elapsed, 0.1)
        self.assertEqual(response["status"], "queued")
        self.assertIn(response["bulk_id"], server.bulk_tasks)

    def test_get_bulk_job_returns_background_status(self):
        server.bulk_tasks["bulk-1"] = {"id": "bulk-1", "status": "complete", "created_count": 3}

        self.assertEqual(server.get_bulk_job("bulk-1")["created_count"], 3)

    def test_favicon_serves_local_icon(self):
        response = server.favicon()

        self.assertEqual(response.media_type, "image/svg+xml")
        self.assertIn("pot-leaf.svg", response.path)
        self.assertIn("Pot leaf", Path(response.path).read_text(encoding="utf-8"))


class WebUiChangesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_removed_redundant_controls_and_text(self):
        self.assertNotIn("Download if already downloaded", self.html)
        self.assertNotIn("Skip chapters", self.html)
        self.assertNotIn("Queue multiple downloads", self.html)
        self.assertNotIn("Job created", self.html)

    def test_login_is_compact_header_login_without_token(self):
        self.assertIn("Update Login", self.html)
        self.assertIn("Login", self.html)
        self.assertIn('id="logout-btn"', self.html)
        self.assertIn("minmax(260px, 1.35fr) minmax(170px, 0.8fr) 112px 92px", self.html)
        self.assertIn("async function logoutCredentials", self.html)
        self.assertIn('api("/credentials", { method: "DELETE" })', self.html)
        self.assertIn("resetCatalogUi()", self.html)
        self.assertIn('logoutBtn.addEventListener("click", logoutCredentials)', self.html)
        self.assertIn('status.has_saved_credentials ? "Update Login" : "Login"', self.html)
        self.assertIn('<link rel="icon" href="/favicon.ico" type="image/svg+xml" />', self.html)
        self.assertIn('src="/favicon.ico"', self.html)
        self.assertIn("brand-lockup", self.html)
        self.assertIn("Login saved", self.html)
        self.assertIn("Login required", self.html)
        self.assertIn("await Promise.all([", self.html)
        self.assertIn("loadArtists(artistSelect.value)", self.html)
        self.assertIn("loadYearsForAvailability(yearFilter.value)", self.html)
        self.assertIn('outPathInput.addEventListener("click", chooseOutputPath)', self.html)
        self.assertIn("/folders/choose", self.html)
        self.assertIn("cfg.default_out_path", self.html)
        self.assertIn("sr-only", self.html)
        self.assertIn('<label class="sr-only" for="nugs_email">Email</label>', self.html)
        self.assertIn('<label class="sr-only" for="nugs_password">Password</label>', self.html)
        self.assertNotIn("<h2>Nugs Login</h2>", self.html)
        self.assertNotIn('id="credential_token"', self.html)
        self.assertNotIn("Save Credentials", self.html)
        self.assertNotIn("password saved", self.html)
        self.assertNotIn("password missing", self.html)
        self.assertNotIn("status.email) document.getElementById(\"nugs_email\").value", self.html)

    def test_download_copy_and_queue_copy_were_renamed(self):
        self.assertIn("Queue Downloads", self.html)
        self.assertIn("Start all", self.html)
        self.assertIn("Pause all", self.html)
        self.assertIn("Start downloads", self.html)
        self.assertIn("Pause downloads", self.html)
        self.assertIn("Current Downloads", self.html)
        self.assertNotIn("<th>Status</th>", self.html)
        self.assertNotIn('data-action="show-folder"', self.html)
        self.assertNotIn('data-action="cancel"', self.html)
        self.assertIn("renderStatus", self.html)
        self.assertIn('data-action="queue-resume"', self.html)
        self.assertIn('data-action="queue-pause"', self.html)
        self.assertIn("setQueuePaused", self.html)
        self.assertIn('aria-label="Start downloads"', self.html)
        self.assertIn('aria-label="Pause downloads"', self.html)
        self.assertIn('aria-label="Cancelled"', self.html)
        self.assertNotIn('data-action="restart"', self.html)
        self.assertIn('data-action="delete"', self.html)
        self.assertIn('aria-label="Delete"', self.html)
        self.assertIn("Cancel all", self.html)
        self.assertIn("/queue/cancel", self.html)
        self.assertIn("Delete all", self.html)
        self.assertIn("/queue/delete", self.html)
        self.assertNotIn("Pause queue", self.html)

    def test_current_downloads_are_collapsible_and_paginated(self):
        self.assertIn('id="downloads-panel"', self.html)
        self.assertIn('id="downloads-summary"', self.html)
        self.assertIn('id="download-status-bar"', self.html)
        self.assertIn("download-status-segment", self.html)
        self.assertIn("accordion-caret", self.html)
        self.assertIn('id="last-updated" class="sr-only"', self.html)
        self.assertIn('id="jobs-page-size"', self.html)
        self.assertIn('id="jobs-prev-page"', self.html)
        self.assertIn('id="jobs-next-page"', self.html)
        self.assertIn("Records per page", self.html)
        self.assertIn("currentJobsPage", self.html)
        self.assertIn("renderPagination", self.html)
        self.assertIn('id="out_path"', self.html)
        self.assertIn('id="start-queue-btn"', self.html)
        self.assertIn("syncQueueToggleButton", self.html)
        self.assertIn("padding: 0 12px 12px;", self.html)
        self.assertIn("downloads-actions", self.html)
        self.assertIn("downloads-output", self.html)

    def test_current_downloads_use_progress_bars(self):
        self.assertIn("<th>Progress</th>", self.html)
        self.assertNotIn("Progress / Queue", self.html)
        self.assertIn("renderJobProgress", self.html)
        self.assertIn("job-progress-bar", self.html)
        self.assertIn("job-progress-fill", self.html)
        self.assertIn("job-progress-label", self.html)
        self.assertIn("Queue #", self.html)
        self.assertIn("style=\"width: ${safePercent}%\"", self.html)

    def test_current_downloads_show_release_and_format_details(self):
        self.assertNotIn("<th>Job</th>", self.html)
        self.assertNotIn('job.id.slice(0, 8)', self.html)
        self.assertIn("<th>Show</th>", self.html)
        self.assertIn("renderShowDetail", self.html)
        self.assertIn("show-detail", self.html)
        self.assertIn("summary.artist_name", self.html)
        self.assertIn("summary.date", self.html)
        self.assertIn("summary.mode", self.html)
        self.assertIn("showAudioDetails", self.html)
        self.assertIn("showVideoDetails", self.html)
        self.assertIn("Audio out:", self.html)
        self.assertIn("Video out:", self.html)

    def test_direct_url_and_download_scope_are_removed(self):
        self.assertNotIn("Use a direct URL instead", self.html)
        self.assertNotIn('id="url-details"', self.html)
        self.assertNotIn('id="url"', self.html)
        self.assertNotIn('id="url_enabled"', self.html)
        self.assertNotIn("Download scope", self.html)
        self.assertNotIn('id="bulk_scope"', self.html)
        self.assertNotIn('id="release_select"', self.html)

    def test_queue_selection_replaces_song_selection(self):
        self.assertIn("Queue Selection", self.html)
        self.assertIn('id="release-check-list"', self.html)
        self.assertIn("song-head", self.html)
        self.assertIn("song-title", self.html)
        self.assertIn('<span class="hint" id="selection-hint">', self.html)
        self.assertIn('id="selection-toggle"', self.html)
        self.assertIn('data-select-all="true"', self.html)
        self.assertIn('id="selection-page-size"', self.html)
        self.assertIn('data-release-url', self.html)
        self.assertIn('data-action="toggle-release"', self.html)
        self.assertIn("song-detail-row", self.html)
        self.assertIn("song-detail-list", self.html)
        self.assertIn("setlist-line", self.html)
        self.assertIn("column-count: 3", self.html)
        self.assertIn('`Set ${setValue}`', self.html)
        self.assertIn('set.songs.join(", ")', self.html)
        self.assertIn("queue-table", self.html)
        self.assertIn("<th>Date</th>", self.html)
        self.assertIn("<th>Band</th>", self.html)
        self.assertIn("<th>Venue</th>", self.html)
        self.assertIn("<th>Media</th>", self.html)
        self.assertNotIn("Song Selection", self.html)
        self.assertNotIn('id="select-all-releases"', self.html)
        self.assertNotIn('id="clear-releases"', self.html)
        self.assertNotIn('<p class="hint" id="selection-hint">', self.html)
        self.assertNotIn('data-track-id', self.html)
        self.assertNotIn("urlEnabled", self.html)
        self.assertNotIn("urlDetails", self.html)

    def test_band_selector_hides_show_counts_and_mode_is_filter(self):
        self.assertIn('<label for="download_mode">Download mode</label>', self.html)
        self.assertNotIn('<label for="download_mode">Mode</label>', self.html)
        self.assertNotIn('id="bulk-options"', self.html)
        self.assertNotIn("artist.num_shows", self.html)
        self.assertNotIn("num_shows || 0", self.html)

    def test_output_dropdown_copy_is_compact(self):
        self.assertNotIn("Config default", self.html)
        self.assertNotIn("Keep source", self.html)
        self.assertIn('<option value="">Default</option>', self.html)
        self.assertIn('<option value="source">Source</option>', self.html)

    def test_dropdowns_have_enhanced_select_styling(self):
        self.assertIn("appearance: none;", self.html)
        self.assertIn("calc(100% - 17px)", self.html)
        self.assertIn("select:hover", self.html)
        self.assertIn("select:disabled", self.html)
        self.assertIn("custom-select-menu", self.html)
        self.assertIn("custom-select-option", self.html)
        self.assertIn("typeaheadCustomSelect", self.html)
        self.assertIn("activateCustomOption", self.html)
        self.assertIn("alphaSorter", self.html)
        self.assertIn("loadYearsForAvailability", self.html)
        self.assertIn("/catalog/years", self.html)
        self.assertIn("enhanceSelects();", self.html)

    def test_ui_smoke_flow_wiring_is_present(self):
        self.assertIn("queueSelectedDownloads", self.html)
        self.assertIn('await api("/queue/pause"', self.html)
        self.assertIn("createJobForUrl", self.html)
        self.assertIn('data-action="queue-resume"', self.html)
        self.assertIn('data-action="queue-pause"', self.html)
        self.assertIn("setQueuePaused(false)", self.html)
        self.assertIn("setQueuePaused(true)", self.html)
        self.assertIn('data-action="delete"', self.html)
        self.assertIn("deleteJob(jobId)", self.html)
        self.assertIn("loadYearsForAvailability(yearFilter.value)", self.html)
        self.assertIn("typeaheadCustomSelect(select, shell, event.key)", self.html)
        self.assertIn('event.target.closest("[data-action]")', self.html)

    def test_icons_use_consistent_outline_style(self):
        self.assertIn(".icon-btn svg", self.html)
        self.assertIn("stroke-linecap: round", self.html)
        self.assertIn("stroke-linejoin: round", self.html)
        self.assertIn("width: 20px", self.html)
        self.assertIn("media-icon", self.html)
        self.assertIn("status-icon", self.html)

    def test_mode_selection_is_sticky_across_option_sync(self):
        self.assertIn("let userSelectedDownloadMode = downloadMode.value || \"both\";", self.html)
        self.assertIn("userSelectedDownloadMode = downloadMode.value;", self.html)
        self.assertIn("value === userSelectedDownloadMode", self.html)

    def test_availability_change_preserves_selected_band(self):
        self.assertIn("const selectedArtistId = artistSelect.value;", self.html)
        self.assertIn("if (selectedArtistId) {", self.html)
        self.assertIn("renderReleases(yearFilter.value, mediaFilter.value);", self.html)


class PackageSecurityTest(unittest.TestCase):
    def test_runtime_package_is_allowlisted(self):
        self.assertEqual(
            build_packages.RUNTIME_FILES,
            ["launcher.py", "main.py", "server.py", "nugs_credentials.py", "requirements.txt"],
        )
        self.assertEqual(build_packages.WEB_FILES, ["index.html", "pot-leaf.svg"])
        blocked = {
            ".agents",
            ".codex",
            ".git",
            "README.md",
            "config.json",
            "config.example.json",
            "token.md",
            "download_history.sqlite3",
            "nugs_credentials.enc",
            "nugs_credentials.key",
            "data",
            "tests",
            "scripts",
            "docker",
            "p3venv",
        }
        self.assertTrue(blocked.issubset(build_packages.DENYLIST_NAMES))

    def test_copy_runtime_excludes_private_and_dev_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "NugsDownloader"
            build_packages.copy_runtime(dest)
            packaged = {str(path.relative_to(dest)) for path in dest.rglob("*") if path.is_file()}

        self.assertEqual(
            packaged,
            {
                "main.py",
                "launcher.py",
                "server.py",
                "nugs_credentials.py",
                "requirements.txt",
                "web/index.html",
                "web/pot-leaf.svg",
            },
        )
        for forbidden in build_packages.DENYLIST_NAMES:
            self.assertFalse(any(forbidden in Path(name).parts for name in packaged), forbidden)

    def test_package_validation_detects_secret_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("NugsDownloader/main.py", "print('ok')")
                zf.writestr("NugsDownloader/config.json", "{}")
                zf.writestr("NugsDownloader/nugs_credentials.enc", "{}")

            hits = build_packages.validate_package(zip_path)

        self.assertIn("NugsDownloader/config.json", hits)
        self.assertIn("NugsDownloader/nugs_credentials.enc", hits)

    def test_packaged_launchers_use_user_state_locations(self):
        script = (ROOT / "packaging" / "build_packages.py").read_text(encoding="utf-8")
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('"Library" / "Application Support" / "NugsDownloader"', launcher)
        self.assertIn("NugsDownloader", launcher)
        self.assertIn("%LOCALAPPDATA%\\NugsDownloader\\config.json", script)
        self.assertIn("NUGS_CREDENTIALS_PATH", launcher)
        self.assertIn("NUGS_CREDENTIALS_KEY_PATH", launcher)
        self.assertIn("NUGS_HISTORY_DB_PATH", launcher)
        self.assertIn("NUGS_DEFAULT_OUT_PATH", launcher)
        self.assertIn("nugs_credentials.enc", launcher)
        self.assertIn("nugs_credentials.key", launcher)
        self.assertIn("download_history.sqlite3", launcher)
        self.assertIn("launcher.py", script)
        self.assertIn("--windowed", script)
        self.assertNotIn('open "http://127.0.0.1:8090/"', script)

    def test_server_history_db_can_live_in_user_state(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("NUGS_HISTORY_DB_PATH", source)
        self.assertIn('WORKDIR / "download_history.sqlite3"', source)
        self.assertIn("NUGS_DEFAULT_OUT_PATH", source)
        self.assertIn("def choose_output_folder", source)
        self.assertIn("choose folder with prompt", source)

    def test_packaged_launchers_refresh_dependencies_when_requirements_change(self):
        script = (ROOT / "packaging" / "build_packages.py").read_text(encoding="utf-8")
        self.assertIn("requirements.sha256", script)
        self.assertIn("hashlib.sha256", script)
        self.assertIn('pip install -r requirements.txt', script)

    def test_macos_app_bundles_runtime_dependencies(self):
        script = (ROOT / "packaging" / "build_packages.py").read_text(encoding="utf-8")
        self.assertIn("build_macos_windowed_app", script)
        self.assertIn('"pyinstaller"', script)
        self.assertIn('"--windowed"', script)
        self.assertIn('"--hidden-import"', script)
        self.assertIn('"webview.platforms.cocoa"', script)
        self.assertIn('"server"', script)
        self.assertIn('"sqlite3"', script)
        self.assertIn('"_sqlite3"', script)
        self.assertIn('"-m", "venv", "--copies"', script)
        self.assertIn('rglob("__pycache__")', script)
        self.assertIn('rglob("*.pyc")', script)
        self.assertNotIn('exec "$PYTHON_BIN" launcher.py', script)
        self.assertNotIn('PYTHON_BIN="$(cd "$(dirname "$0")/../Resources/venv/bin"', script)
        self.assertNotIn('VENV_DIR="$STATE_DIR/.venv"', script)
        self.assertNotIn('pip install -r "$APP_ROOT/requirements.txt"', script)

    def test_packaged_macos_app_uses_original_icon(self):
        script = (ROOT / "packaging" / "build_packages.py").read_text(encoding="utf-8")
        self.assertIn("generate_pot_leaf_png", script)
        self.assertIn("Generate original app icon artwork", script)
        self.assertIn("NugsDownloader.icns", script)
        self.assertIn('"--icon"', script)
        self.assertIn("NugsDownloader", script)
        self.assertIn("macOS icon generation failed", script)
        self.assertIn("return None", script)

    def test_desktop_launcher_uses_standalone_webview(self):
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("import webview", launcher)
        self.assertIn("webview.create_window", launcher)
        self.assertIn("webview.start", launcher)
        self.assertIn('title="Nugs Downloader"', launcher)
        self.assertIn("width=1480", launcher)
        self.assertIn("height=980", launcher)
        self.assertIn("min_size=(1100, 740)", launcher)
        self.assertIn("Standalone WebView is unavailable", launcher)
        self.assertIn("NUGS_LAUNCHER_SMOKE", launcher)
        self.assertIn("launcher smoke ok", launcher)
        self.assertNotIn("import webbrowser", launcher)
        self.assertNotIn("webbrowser.open", launcher)

    def test_desktop_launcher_exposes_common_ffmpeg_paths(self):
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('EXTRA_FFMPEG_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")', launcher)
        self.assertIn("os.pathsep.join", launcher)
        self.assertIn('os.environ["PATH"]', launcher)

    def test_desktop_launcher_uses_own_dynamic_port(self):
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("def find_free_port", launcher)
        self.assertIn("sock.bind((host, 0))", launcher)
        self.assertIn("run_server, args=(port,)", launcher)
        self.assertIn('url=f"http://{HOST}:{port}/"', launcher)
        self.assertNotIn('APP_URL = "http://127.0.0.1:8090/"', launcher)
        self.assertNotIn("port=8090", launcher)

    def test_desktop_launcher_forces_fresh_login_each_open(self):
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("def destroy_saved_login", launcher)
        self.assertIn('("NUGS_CREDENTIALS_PATH", "NUGS_CREDENTIALS_KEY_PATH")', launcher)
        self.assertIn("unlink(missing_ok=True)", launcher)
        self.assertIn('for key in ("email", "password", "token")', launcher)
        self.assertIn("destroy_saved_login()\n    if os.environ.get(SMOKE_ENV)", launcher)
        self.assertIn("finally:\n        destroy_saved_login()", launcher)

    def test_requirements_include_macos_webview_backend(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pywebview", requirements)
        self.assertIn('pyobjc-framework-Cocoa; sys_platform == "darwin"', requirements)
        self.assertIn('pyobjc-framework-WebKit; sys_platform == "darwin"', requirements)

    def test_github_actions_builds_exe_and_dmg_artifacts(self):
        workflow = (ROOT / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("runs-on: macos-latest", workflow)
        self.assertIn("./build-exe.ps1", workflow)
        self.assertIn("Nugs Downloader.exe", workflow)
        self.assertIn("NugsDownloader-Windows-exe", workflow)
        self.assertIn("python packaging/build_packages.py", workflow)
        self.assertIn("NugsDownloader-macOS.dmg", workflow)
        self.assertIn("NugsDownloader-macOS-dmg", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("Smoke test macOS app launcher", workflow)
        self.assertIn("Smoke test Windows exe launcher", workflow)
        self.assertIn("NUGS_LAUNCHER_SMOKE", workflow)

    def test_github_actions_checks_artifacts_for_private_files(self):
        workflow = (ROOT / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
        self.assertIn("Verify clean macOS artifacts", workflow)
        self.assertIn("Verify clean Windows artifacts", workflow)
        self.assertIn("config.json", workflow)
        self.assertIn("token.md", workflow)
        self.assertIn("nugs_credentials.enc", workflow)
        self.assertIn("nugs_credentials.key", workflow)
        self.assertIn("download_history.sqlite3", workflow)

    def test_readme_documents_secure_desktop_packaging(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("standalone desktop window", readme)
        self.assertIn("do not intentionally open the default browser", readme)
        self.assertIn("`config.json` is optional", readme)
        self.assertIn("not required for packaged app users", readme)
        self.assertIn("Credentials are entered in the web UI header", readme)
        self.assertIn("must not be committed or packaged", readme)
        self.assertIn("data/private_sources/nugs/inbox/", readme)
        self.assertIn("GitHub Actions", readme)
        self.assertIn("NugsDownloader-macOS.dmg", readme)
        self.assertIn("Nugs Downloader.exe", readme)
        self.assertIn("original project artwork", readme)
        self.assertNotIn("set your local credentials there", readme)
        self.assertNotIn("Token for Apple/Google logins", readme)

    def test_docker_compose_persists_nugs_state_without_config_mount(self):
        compose = (ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("NUGS_CONFIG_PATH: /app/data/private_sources/nugs/config.json", compose)
        self.assertIn("../data/private_sources/nugs:/app/data/private_sources/nugs", compose)
        self.assertNotIn("../config.json:/app/config.json", compose)


if __name__ == "__main__":
    unittest.main()
