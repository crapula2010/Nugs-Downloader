#!/usr/bin/env python3
"""Downloader CLI for nugs.net content.

This script handles authentication, album and playlist downloads, video
downloads, HLS decryption, and ffmpeg-based packaging.

Dependencies:
    pip install -r requirements.txt

Usage:
    python main.py <url> [<url>...]

The downloader reads defaults from `config.json` in the same directory,
or from the path in `NUGS_CONFIG_PATH` when set.
"""

import argparse
import base64
import binascii
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import humanize
import m3u8
import requests
from Crypto.Cipher import AES
from urllib.parse import parse_qs, urlparse, urlencode

# --- Constants --------------------------------------------------------------

devKey = "x7f54tgbdyc64y656thy47er4"
clientId = "Eg7HuH873H65r5rt325UytR5429"
layout = "%m/%d/%Y %H:%M:%S"
userAgent = "NugsNet/3.26.724 (Android; 7.1.2; Asus; ASUS_Z01QD; Scale/2.0; en)"
userAgentTwo = "nugsnetAndroid"
authUrl = "https://id.nugs.net/connect/token"
streamApiBase = "https://streamapi.nugs.net/"
subInfoUrl = "https://subscriptions.nugs.net/api/v1/me/subscriptions"
userInfoUrl = "https://id.nugs.net/connect/userinfo"
playerUrl = "https://play.nugs.net/"
sanRegexStr = r"[\\/:*?\"<>|]"
chapsFileFname = "chapters_nugs_dl_tmp.txt"
durRegex = r"Duration: ([\d:.]+)"
bitrateRegex = r"[\w]+(?:_(\d+)k_v\d+)"

# Regex patterns used to determine the type of URL input.
regexStrings = [
    r"^https://play.nugs.net/release/(\d+)$",
    r"^https://play.nugs.net/#/playlists/playlist/(\d+)$",
    r"^https://play.nugs.net/library/playlist/(\d+)$",
    r"(^https://2nu.gs/[a-zA-Z\d]+$)",
    r"^https://play.nugs.net/#/videos/artist/\d+/.+/(\d+)$",
    r"^https://play.nugs.net/artist/(\d+)(?:/albums|/latest|)$",
    r"^https://play.nugs.net/livestream/(\d+)/exclusive$",
    r"^https://play.nugs.net/watch/livestreams/exclusive/(\d+)$",
    r"^https://play.nugs.net/#/my-webcasts/\d+-(\d+)-\d+-\d+$",
    r"^https://www.nugs.net/on/demandware.store/Sites-NugsNet-Site/default/(?:Stash-QueueVideo|NugsVideo-GetStashVideo)\?([a-zA-Z0-9=%&-]+$)",
    r"^https://play.nugs.net/library/webcast/(\d+)$",
]

qualityMap = {
    ".alac16/": {"Specs": "16-bit / 44.1 kHz ALAC", "Extension": ".m4a", "Format": 1},
    ".flac16/": {"Specs": "16-bit / 44.1 kHz FLAC", "Extension": ".flac", "Format": 2},
    # .mqa24/ must be above .flac?
    ".mqa24/": {"Specs": "24-bit / 48 kHz MQA", "Extension": ".flac", "Format": 3},
    ".flac?": {"Specs": "FLAC", "Extension": ".flac", "Format": 2},
    ".s360/": {"Specs": "360 Reality Audio", "Extension": ".mp4", "Format": 4},
    ".aac150/": {"Specs": "150 Kbps AAC", "Extension": ".m4a", "Format": 5},
    ".m4a?": {"Specs": "AAC", "Extension": ".m4a", "Format": 5},
    ".m3u8?": {"Extension": ".m4a", "Format": 6},
}

resolveRes = {
    1: "480",
    2: "720",
    3: "1080",
    4: "1440",
    5: "2160",
}

trackFallback = {1: 2, 2: 5, 3: 2, 4: 3}
resFallback = {"720": "480", "1080": "720", "1440": "1080"}

session = requests.Session()

# --- Data structures --------------------------------------------------------

@dataclass
class Config:
    email: str = ""
    password: str = ""
    urls: List[str] = field(default_factory=list)
    format: int = 2
    outPath: str = ""
    videoFormat: int = 2
    wantRes: str = ""
    token: str = ""
    useFfmpegEnvVar: bool = False
    ffmpegNameStr: str = ""
    forceVideo: bool = False
    skipVideos: bool = False
    skipChapters: bool = False


@dataclass
class Args:
    Urls: List[str]
    Format: int
    VideoFormat: int
    OutPath: Optional[str]
    ForceVideo: bool
    SkipVideos: bool
    SkipChapters: bool


@dataclass
class StreamParams:
    SubscriptionID: str
    SubCostplanIDAccessList: str
    UserID: str
    StartStamp: str
    EndStamp: str


# --- Helpers ---------------------------------------------------------------

def handle_err(err_text: str, err: Exception, panic: bool = False) -> None:
    if panic:
        raise
    print(f"{err_text}\n{err}")


def get_script_dir() -> Path:
    # Match the behavior in Go when running from compiled exe vs source.
    return Path(__file__).resolve().parent


def get_config_path() -> Path:
    config_path = os.environ.get("NUGS_CONFIG_PATH", "").strip()
    if config_path:
        return Path(config_path).expanduser()
    return get_script_dir() / "config.json"


def read_txt_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return [line.strip() for line in f if line.strip()]


def contains(lines: List[str], value: str) -> bool:
    return any(line.strip().lower() == value.strip().lower() for line in lines)


def process_urls(urls: List[str]) -> List[str]:
    processed: List[str] = []
    txt_paths: List[str] = []
    for url in urls:
        if url.endswith(".txt") and url not in txt_paths:
            txt_lines = read_txt_file(url)
            for txt_line in txt_lines:
                if txt_line not in processed:
                    processed.append(txt_line.rstrip("/"))
            txt_paths.append(url)
        else:
            if url not in processed:
                processed.append(url.rstrip("/"))
    return processed


def read_config() -> Config:
    config_path = get_config_path()
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Config(**data)


def parse_args() -> Args:
    parser = argparse.ArgumentParser(description="Nugs Downloader (Python port)")
    parser.add_argument("urls", nargs="+", help="URLs or .txt file with URLs")
    parser.add_argument("-f", "--format", type=int, default=-1,
                        help="Track download format (1=ALAC,2=FLAC,3=MQA,4=360,5=AAC)")
    parser.add_argument("-F", "--video-format", type=int, default=-1,
                        help="Video download format (1=480p,...,5=4K)")
    parser.add_argument("-o", "--out", dest="out_path", default="",
                        help="Where to download to")
    parser.add_argument("--force-video", action="store_true",
                        help="Force video download when available")
    parser.add_argument("--skip-videos", action="store_true",
                        help="Skip videos in artist URLs")
    parser.add_argument("--skip-chapters", action="store_true",
                        help="Skip embedding chapters into video")
    args = parser.parse_args()
    return Args(
        Urls=args.urls,
        Format=args.format,
        VideoFormat=args.video_format,
        OutPath=args.out_path,
        ForceVideo=args.force_video,
        SkipVideos=args.skip_videos,
        SkipChapters=args.skip_chapters,
    )


def make_dirs(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def file_exists(path: str) -> bool:
    p = Path(path)
    return p.is_file()


def sanitise(filename: str) -> str:
    san = re.sub(sanRegexStr, "_", filename)
    return san.rstrip("\t")


def emit_progress(kind: str, path: str, downloaded: int, total: int, extra: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {
        "kind": kind,
        "path": path,
        "downloaded": downloaded,
        "total": total,
        "percent": int(downloaded * 100 / total) if total > 0 else 0,
        "ts": int(time.time()),
    }
    if extra:
        payload.update(extra)
    print("PROGRESS " + json.dumps(payload), flush=True)


def emit_file_event(state: str, kind: str, path: str, extra: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {
        "state": state,
        "kind": kind,
        "path": path,
        "ts": int(time.time()),
    }
    if extra:
        payload.update(extra)
    print("FILE " + json.dumps(payload), flush=True)


def auth(email: str, pwd: str) -> str:
    data = {
        "client_id": clientId,
        "grant_type": "password",
        "scope": "openid profile email nugsnet:api nugsnet:legacyapi offline_access",
        "username": email,
        "password": pwd,
    }
    headers = {
        "User-Agent": userAgent,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r = session.post(authUrl, data=data, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"Auth failed: {r.status_code} {r.reason}")
    return r.json()["access_token"]


def get_user_info(token: str) -> str:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": userAgent}
    r = session.get(userInfoUrl, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"User info failed: {r.status_code} {r.reason}")
    return r.json()["sub"]


def get_sub_info(token: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": userAgent}
    r = session.get(subInfoUrl, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"Sub info failed: {r.status_code} {r.reason}")
    return r.json()


def get_plan(sub_info: Dict[str, Any]) -> Tuple[str, bool]:
    if sub_info.get("plan") and sub_info["plan"].get("description"):
        return sub_info["plan"]["description"], False
    return sub_info.get("promo", {}).get("plan", {}).get("description", ""), True


def parse_timestamps(start: str, end: str) -> Tuple[str, str]:
    start_ts = int(datetime.strptime(start, layout).timestamp())
    end_ts = int(datetime.strptime(end, layout).timestamp())
    return str(start_ts), str(end_ts)


def parse_stream_params(user_id: str, sub_info: Dict[str, Any], is_promo: bool) -> StreamParams:
    start_stamp, end_stamp = parse_timestamps(sub_info["startedAt"], sub_info["endsAt"])
    plan = sub_info["promo"]["plan"] if is_promo else sub_info["plan"]
    return StreamParams(
        SubscriptionID=sub_info["legacySubscriptionId"],
        SubCostplanIDAccessList=plan["planId"],
        UserID=user_id,
        StartStamp=start_stamp,
        EndStamp=end_stamp,
    )


def check_url(url: str) -> Tuple[str, int]:
    for i, regex_str in enumerate(regexStrings):
        match = re.match(regex_str, url)
        if match:
            return match.group(1), i
    return "", 0


def extract_leg_token(token_str: str) -> Tuple[str, str]:
    payload = token_str.split(".")[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    obj = json.loads(decoded)
    return obj["legacy_token"], obj["legacy_uguid"]


def get_album_meta(album_id: str) -> Dict[str, Any]:
    params = {
        "method": "catalog.container",
        "containerID": album_id,
        "vdisp": "1",
    }
    headers = {"User-Agent": userAgent}
    r = session.get(streamApiBase + "api.aspx", params=params, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"Album meta failed: {r.status_code} {r.reason}")
    return r.json()


def get_plist_meta(plist_id: str, email: str, legacy_token: str, cat: bool) -> Dict[str, Any]:
    path = "api.aspx" if cat else "secureApi.aspx"
    params: Dict[str, Any] = {}
    if cat:
        params = {"method": "catalog.playlist", "plGUID": plist_id}
    else:
        params = {
            "method": "user.playlist",
            "playlistID": plist_id,
            "developerKey": devKey,
            "user": email,
            "token": legacy_token,
        }
    headers = {"User-Agent": userAgentTwo}
    r = session.get(streamApiBase + path, params=params, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"Playlist meta failed: {r.status_code} {r.reason}")
    return r.json()


def get_artist_meta(artist_id: str) -> List[Dict[str, Any]]:
    all_meta: List[Dict[str, Any]] = []
    offset = 1
    while True:
        params = {
            "method": "catalog.containersAll",
            "limit": "100",
            "artistList": artist_id,
            "availType": "1",
            "vdisp": "1",
            "startOffset": str(offset),
        }
        headers = {"User-Agent": userAgent}
        r = session.get(streamApiBase + "api.aspx", params=params, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"Artist meta failed: {r.status_code} {r.reason}")
        obj = r.json()
        containers = obj.get("Response", {}).get("Containers", [])
        if not containers:
            break
        all_meta.append(obj)
        offset += len(containers)
    return all_meta


def get_purchased_man_url(sku_id: int, show_id: str, user_id: str, ugu_id: str) -> str:
    params = {
        "skuId": sku_id,
        "showId": show_id,
        "uguid": ugu_id,
        "nn_userID": user_id,
        "app": "1",
    }
    headers = {"User-Agent": userAgentTwo}
    r = session.get(streamApiBase + "bigriver/vidPlayer.aspx", params=params, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"Purchased man URL failed: {r.status_code} {r.reason}")
    return r.json()["fileURL"]


def get_stream_meta(track_id: int, sku_id: int, fmt: int, stream_params: StreamParams) -> str:
    params: Dict[str, Any] = {
        "app": "1",
        "subscriptionID": stream_params.SubscriptionID,
        "subCostplanIDAccessList": stream_params.SubCostplanIDAccessList,
        "nn_userID": stream_params.UserID,
        "startDateStamp": stream_params.StartStamp,
        "endDateStamp": stream_params.EndStamp,
    }
    if fmt == 0:
        params.update({"skuId": sku_id, "containerID": track_id, "chap": "1"})
    else:
        params.update({"platformID": fmt, "trackID": track_id})
    headers = {"User-Agent": userAgentTwo}
    r = session.get(streamApiBase + "bigriver/subPlayer.aspx", params=params, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"Stream meta failed: {r.status_code} {r.reason}")
    return r.json()["streamLink"]


def query_quality(stream_url: str) -> Optional[Dict[str, Any]]:
    for k, v in qualityMap.items():
        if k in stream_url:
            quality = dict(v)
            quality["URL"] = stream_url
            return quality
    return None


def download_track(track_path: str, url: str) -> None:
    headers = {"Referer": playerUrl, "User-Agent": userAgent, "Range": "bytes=0-"}
    r = session.get(url, headers=headers, stream=True)
    if r.status_code not in (200, 206):
        raise RuntimeError(f"Download failed: {r.status_code} {r.reason}")
    total = int(r.headers.get("Content-Length", 0))
    downloaded = 0
    last_emit = time.time()
    with open(track_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - last_emit >= 10:
                emit_progress("audio", track_path, downloaded, total)
                last_emit = now
    emit_progress("audio", track_path, downloaded, total, {"done": True})


def get_track_qual(quals: List[Dict[str, Any]], want_fmt: int) -> Optional[Dict[str, Any]]:
    for q in quals:
        if q.get("Format") == want_fmt:
            return q
    return None


def extract_bitrate(man_url: str) -> str:
    match = re.search(bitrateRegex, man_url)
    return match.group(1) if match else ""


def get_manifest_base(manifest_url: str) -> Tuple[str, str]:
    u = urlparse(manifest_url)
    path = u.path
    last_slash = path.rfind("/")
    base = f"{u.scheme}://{u.netloc}{path[:last_slash+1]}"
    return base, f"?{u.query}"


def parse_hls_master(qual: Dict[str, Any]) -> None:
    r = session.get(qual["URL"])
    if r.status_code != 200:
        raise RuntimeError(f"HLS master fetch failed: {r.status_code} {r.reason}")
    playlist = m3u8.load(r.text)
    variants = sorted(playlist.playlists, key=lambda v: v.stream_info.bandwidth, reverse=True)
    if not variants:
        raise RuntimeError("No variants found in master playlist")
    variant = variants[0]
    bitrate = extract_bitrate(variant.uri)
    if not bitrate:
        raise RuntimeError("No regex match for manifest bitrate")
    qual["Specs"] = f"{bitrate} Kbps AAC"
    man_base, q = get_manifest_base(qual["URL"])
    qual["URL"] = man_base + variant.uri + q


def get_key(key_url: str) -> bytes:
    r = session.get(key_url)
    if r.status_code != 200:
        raise RuntimeError(f"Key fetch failed: {r.status_code} {r.reason}")
    return r.content[:16]


def decrypt_track(key: bytes, iv: bytes) -> bytes:
    enc_data = Path("temp_enc.ts").read_bytes()
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(enc_data)
    padding_len = decrypted[-1]
    return decrypted[: -padding_len]


def ts_to_aac(dec_data: bytes, out_path: str, ffmpeg_name: str) -> None:
    proc = requests.compat.os.popen
    import subprocess

    cmd = [ffmpeg_name, "-i", "pipe:", "-c:a", "copy", out_path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr = p.communicate(dec_data)[1].decode("utf-8", errors="ignore")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr}")


def hls_only(track_path: str, man_url: str, ffmpeg_name_str: str) -> None:
    r = session.get(man_url)
    if r.status_code != 200:
        raise RuntimeError(f"HLS master fetch failed: {r.status_code} {r.reason}")
    playlist = m3u8.loads(r.text)
    media = playlist

    man_base, q = get_manifest_base(man_url)
    if not media.segments:
        raise RuntimeError("No segments in HLS playlist")
    ts_url = man_base + media.segments[0].uri + q

    key = media.keys[0]
    key_bytes = get_key(man_base + key.uri)
    iv = bytes.fromhex(key.iv[2:])

    download_track("temp_enc.ts", ts_url)
    dec_data = decrypt_track(key_bytes, iv)
    Path("temp_enc.ts").unlink(missing_ok=True)
    ts_to_aac(dec_data, track_path, ffmpeg_name_str)


def check_if_hls_only(quals: List[Dict[str, Any]]) -> bool:
    return all(".m3u8?" in q.get("URL", "") for q in quals)


def process_track(fol_path: str, track_num: int, track_total: int, cfg: Config, track: Dict[str, Any], stream_params: StreamParams) -> None:
    orig_want_fmt = cfg.format
    want_fmt = orig_want_fmt
    quals: List[Dict[str, Any]] = []
    chosen_qual: Optional[Dict[str, Any]] = None

    for i in (1, 4, 7, 10):
        stream_url = get_stream_meta(track["trackID"], 0, i, stream_params)
        if not stream_url:
            raise RuntimeError("The API didn't return a track stream URL")
        quality = query_quality(stream_url)
        if not quality:
            print("The API returned an unsupported format, URL:", stream_url)
            continue
        quals.append(quality)

    if not quals:
        raise RuntimeError("the api didn't return any formats")

    is_hls_only = check_if_hls_only(quals)

    if is_hls_only:
        print("HLS-only track. Only AAC is available, tags currently unsupported.")
        chosen_qual = quals[0]
        parse_hls_master(chosen_qual)
    else:
        while True:
            chosen_qual = get_track_qual(quals, want_fmt)
            if chosen_qual:
                break
            want_fmt = trackFallback.get(want_fmt, want_fmt)
        if chosen_qual is None:
            raise RuntimeError("no track format was chosen")
        if want_fmt != orig_want_fmt and orig_want_fmt != 4:
            print("Unavailable in your chosen format.")

    track_fname = f"{track_num:02d}. {sanitise(track['songTitle'])}{chosen_qual['Extension']}"
    track_path = Path(fol_path) / track_fname
    if track_path.exists():
        print(f"Track already exists locally: {track_path}")
        emit_file_event("exists", "audio", str(track_path))
        return

    print(f"Downloading track {track_num} of {track_total}: {track['songTitle']} - {chosen_qual['Specs']}")
    if is_hls_only:
        hls_only(str(track_path), chosen_qual["URL"], cfg.ffmpegNameStr)
    else:
        download_track(str(track_path), chosen_qual["URL"])
    emit_file_event("created", "audio", str(track_path), {"format": chosen_qual.get("Specs", "")})


def get_video_sku(products: List[Dict[str, Any]]) -> int:
    for product in products:
        fmt = product.get("formatStr")
        if fmt in ("VIDEO ON DEMAND", "LIVE HD VIDEO"):
            return int(product.get("skuID", 0))
    return 0


def get_lstream_sku(products: List[Dict[str, Any]]) -> int:
    for product in products:
        if product.get("formatStr") == "LIVE HD VIDEO":
            return int(product.get("skuID", 0))
    return 0


def get_resolution_height(resolution: Any) -> str:
    if isinstance(resolution, tuple) and len(resolution) >= 2:
        return str(resolution[1])
    if isinstance(resolution, str) and "x" in resolution:
        return resolution.split("x", 1)[1]
    return str(resolution)


def format_resolution_value(resolution: Any) -> str:
    if isinstance(resolution, tuple) and len(resolution) >= 2:
        return f"{resolution[0]}x{resolution[1]}"
    return str(resolution)


def get_vid_variant(variants: List, want_res: str) -> Optional:
    for v in variants:
        resolution = getattr(v.stream_info, "resolution", None)
        if resolution and get_resolution_height(resolution) == want_res:
            return v
    return None


def format_res(res: str) -> str:
    return "4K" if res == "2160" else f"{res}p"


def choose_variant(manifest_url: str, want_res: str) -> Tuple:
    orig_want_res = want_res
    r = session.get(manifest_url)
    if r.status_code != 200:
        raise RuntimeError(f"Manifest fetch failed: {r.status_code} {r.reason}")
    playlist = m3u8.loads(r.text)
    variants = sorted(playlist.playlists, key=lambda v: v.stream_info.bandwidth, reverse=True)
    if want_res == "2160":
        variant = variants[0]
        var_res = get_resolution_height(variant.stream_info.resolution)
        return variant, format_res(var_res)
    while True:
        variant = get_vid_variant(variants, want_res)
        if variant:
            break
        want_res = resFallback.get(want_res, want_res)
        if want_res == orig_want_res:
            break
    if not variant:
        raise RuntimeError("No variant was chosen.")
    if want_res != orig_want_res:
        print("Unavailable in your chosen format.")
    return variant, format_res(want_res)


def get_seg_urls(manifest_url: str, query: str) -> List[str]:
    r = session.get(manifest_url)
    if r.status_code != 200:
        raise RuntimeError(f"Segment list fetch failed: {r.status_code} {r.reason}")
    playlist = m3u8.loads(r.text)
    return [segment.uri + query for segment in playlist.segments if segment]


def download_video(video_path: str, url: str) -> None:
    headers = {"Range": "bytes=0-"}
    r = session.get(url, headers=headers, stream=True)
    if r.status_code not in (200, 206):
        raise RuntimeError(f"Video download failed: {r.status_code} {r.reason}")
    total = int(r.headers.get("Content-Length", 0))
    downloaded = 0
    last_emit = time.time()
    with open(video_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - last_emit >= 10:
                emit_progress("video", video_path, downloaded, total)
                last_emit = now
    emit_progress("video", video_path, downloaded, total, {"done": True})


def download_lstream(video_path: str, base_url: str, seg_urls: List[str]) -> None:
    seg_total = len(seg_urls)
    last_emit = time.time()
    with open(video_path, "wb") as f:
        for idx, seg in enumerate(seg_urls, start=1):
            print(f"\rSegment {idx} of {seg_total}.", end="")
            r = session.get(base_url + seg, stream=True)
            if r.status_code != 200:
                raise RuntimeError(f"Segment download failed: {r.status_code} {r.reason}")
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
            now = time.time()
            if now - last_emit >= 10:
                emit_progress(
                    "video",
                    video_path,
                    idx,
                    seg_total,
                    {"unit": "segments", "segment": idx, "segments": seg_total},
                )
                last_emit = now
    print("")
    emit_progress(
        "video",
        video_path,
        seg_total,
        seg_total,
        {"unit": "segments", "segment": seg_total, "segments": seg_total, "done": True},
    )


def ts_to_mkv(ts_path: str, mkv_path: str, ffmpeg_name: str) -> None:
    import subprocess

    args = [ffmpeg_name, "-hide_banner", "-y", "-i", ts_path, "-c", "copy", mkv_path]
    proc = subprocess.Popen(args, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="ignore"))


def get_lstream_container(containers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for c in reversed(containers):
        if c.get("availabilityTypeStr") == "AVAILABLE" and c.get("containerTypeStr") == "Show":
            return c
    return None


def parse_lstream_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    container = get_lstream_container(meta.get("Response", {}).get("Containers", []))
    if not container:
        return {}
    return {"Response": container}


def album(album_id: str, cfg: Config, stream_params: StreamParams, art_resp: Optional[Dict[str, Any]] = None) -> None:
    if album_id:
        obj = get_album_meta(album_id)
        meta = obj.get("Response", {})
    else:
        meta = art_resp or {}
    tracks = meta.get("songs", [])

    track_total = len(tracks)
    sku_id = get_video_sku(meta.get("products", []))

    if sku_id == 0 and track_total < 1:
        raise RuntimeError("release has no tracks or videos")

    should_try_video_after_audio = False

    if sku_id != 0:
        if cfg.skipVideos:
            if track_total < 1:
                print("Video-only album, skipped.")
                return
        elif cfg.forceVideo or track_total < 1:
            video(album_id, "", cfg, stream_params, meta, False)
            return
        else:
            # Both audio and video requested.
            should_try_video_after_audio = True

    album_folder = f"{meta.get('artistName','')} - {meta.get('containerInfo','').rstrip(' ')}"
    if len(album_folder) > 120:
        album_folder = album_folder[:120]
        print("Album folder name was chopped because it exceeds 120 characters.")

    album_path = Path(cfg.outPath) / sanitise(album_folder)
    make_dirs(str(album_path))

    for idx, track in enumerate(tracks, start=1):
        try:
            process_track(str(album_path), idx, track_total, cfg, track, stream_params)
        except Exception as e:
            handle_err("Track failed.", e, False)

    if should_try_video_after_audio:
        try:
            video(album_id, "", cfg, stream_params, meta, False)
        except Exception as e:
            handle_err("Video failed.", e, False)


def get_album_total(meta: List[Dict[str, Any]]) -> int:
    total = 0
    for m in meta:
        total += len(m.get("Response", {}).get("Containers", []))
    return total


def artist(artist_id: str, cfg: Config, stream_params: StreamParams) -> None:
    meta = get_artist_meta(artist_id)
    if not meta:
        raise RuntimeError("The API didn't return any artist metadata.")
    print(meta[0].get("Response", {}).get("Containers", [])[0].get("ArtistName", ""))
    album_total = get_album_total(meta)
    idx = 1
    for m in meta:
        for container in m.get("Response", {}).get("Containers", []):
            print(f"Item {idx} of {album_total}:")
            idx += 1
            try:
                if cfg.skipVideos:
                    album("", cfg, stream_params, container)
                else:
                    album(str(container.get("ContainerID", "")), cfg, stream_params, None)
            except Exception as e:
                handle_err("Item failed.", e, False)


def playlist(plist_id: str, legacy_token: str, cfg: Config, stream_params: StreamParams, cat: bool) -> None:
    obj = get_plist_meta(plist_id, cfg.email, legacy_token, cat)
    meta = obj.get("Response", {})
    plist_name = meta.get("playListName", "")
    print(plist_name)
    if len(plist_name) > 120:
        plist_name = plist_name[:120]
        print("Playlist folder name was chopped because it exceeds 120 characters.")
    plist_path = Path(cfg.outPath) / sanitise(plist_name)
    make_dirs(str(plist_path))
    items = meta.get("Items", [])
    for idx, item in enumerate(items, start=1):
        try:
            process_track(str(plist_path), idx, len(items), cfg, item.get("track", {}), stream_params)
        except Exception as e:
            handle_err("Track failed.", e, False)


def resolve_cat_plist_id(plist_url: str) -> str:
    r = session.get(plist_url)
    if r.status_code != 200:
        raise RuntimeError(f"Catalog playlist resolution failed: {r.status_code} {r.reason}")
    u = urlparse(r.url)
    q = parse_qs(u.query)
    plGUID = q.get("plGUID", [""])[0]
    if not plGUID:
        raise RuntimeError("not a catalog playlist")
    return plGUID


def catalog_plist(plist_id: str, legacy_token: str, cfg: Config, stream_params: StreamParams) -> None:
    resolved = resolve_cat_plist_id(plist_id)
    playlist(resolved, legacy_token, cfg, stream_params, True)


def paid_lstream(query: str, ugu_id: str, cfg: Config, stream_params: StreamParams) -> None:
    q = parse_qs(query)
    show_id = q.get("showID", [""])[0]
    if not show_id:
        raise RuntimeError("url didn't contain a show id parameter")
    video(show_id, ugu_id, cfg, stream_params, None, True)


def video(video_id: str, ugu_id: str, cfg: Config, stream_params: StreamParams, meta: Optional[Dict[str, Any]], is_lstream: bool) -> None:
    if meta is None:
        obj = get_album_meta(video_id)
        meta = obj.get("Response", {})

    video_fname = f"{meta.get('artistName','')} - {meta.get('containerInfo','').rstrip(' ')}"
    print(video_fname)
    if len(video_fname) > 110:
        video_fname = video_fname[:110]
        print("Video filename was chopped because it exceeds 120 characters.")

    if is_lstream:
        sku_id = get_lstream_sku(meta.get("productFormatList", []))
    else:
        sku_id = get_video_sku(meta.get("products", []))

    if sku_id == 0:
        raise RuntimeError("no video available")

    if not ugu_id:
        manifest_url = get_stream_meta(meta.get("containerID", 0), sku_id, 0, stream_params)
    else:
        manifest_url = get_purchased_man_url(sku_id, video_id, stream_params.UserID, ugu_id)

    if not manifest_url:
        raise RuntimeError("the api didn't return a video manifest url")

    variant, ret_res = choose_variant(manifest_url, cfg.wantRes)
    vid_path_no_ext = Path(cfg.outPath) / sanitise(f"{video_fname}_{ret_res}")
    vid_ts = str(vid_path_no_ext) + ".ts"
    vid_mkv = str(vid_path_no_ext) + ".mkv"

    if Path(vid_mkv).exists():
        print(f"Video already exists locally: {vid_mkv}")
        emit_file_event("exists", "video", vid_mkv)
        return

    if Path(vid_ts).exists():
        print(f"Found existing TS, deleting and restarting download: {vid_ts}")
        Path(vid_ts).unlink(missing_ok=True)

    man_base, query = get_manifest_base(manifest_url)
    seg_urls = get_seg_urls(man_base + variant.uri, query)
    is_lstream = seg_urls and seg_urls[0] != seg_urls[1] if len(seg_urls) > 1 else False

    frame_rate = getattr(variant.stream_info, "frame_rate", None)
    if frame_rate is None:
        frame_rate = getattr(variant.stream_info, "framerate", None)
    if not is_lstream and frame_rate is not None:
        print(f"{float(frame_rate):.3f} FPS, ", end="")
    print(
        f"{variant.stream_info.bandwidth // 1000} Kbps, {ret_res} "
        f"({format_resolution_value(variant.stream_info.resolution)})"
    )

    if is_lstream:
        download_lstream(vid_ts, man_base, seg_urls)
    else:
        download_video(vid_ts, man_base + seg_urls[0])
    emit_file_event("created", "video_ts", vid_ts)

    print("Packaging TS into MKV container...")
    ts_to_mkv(vid_ts, vid_mkv, cfg.ffmpegNameStr)
    emit_file_event("created", "video", vid_mkv)
    print(f"Saved video to: {vid_mkv}")
    Path(vid_ts).unlink(missing_ok=True)


def parse_cfg() -> Config:
    cfg = read_config()
    args = parse_args()
    if args.Format != -1:
        cfg.format = args.Format
    if args.VideoFormat != -1:
        cfg.videoFormat = args.VideoFormat
    if not (1 <= cfg.format <= 5):
        raise ValueError("track Format must be between 1 and 5")
    if not (1 <= cfg.videoFormat <= 5):
        raise ValueError("video format must be between 1 and 5")
    cfg.wantRes = resolveRes[cfg.videoFormat]
    if args.OutPath:
        cfg.outPath = args.OutPath
    if not cfg.outPath:
        cfg.outPath = "Nugs downloads"
    if cfg.token:
        cfg.token = cfg.token.removeprefix("Bearer ")
    cfg.ffmpegNameStr = "ffmpeg" if cfg.useFfmpegEnvVar else "./ffmpeg"
    cfg.urls = process_urls(args.Urls)
    cfg.forceVideo = args.ForceVideo
    cfg.skipVideos = args.SkipVideos
    cfg.skipChapters = args.SkipChapters
    return cfg


def print_banner() -> None:
    print(r"""
 _____                ____                _           _         
|   | |_ _ ___ ___   |    \ ___ _ _ _ ___| |___ ___ _| |___ ___ 
| | | | | | . |_ -|  |  |  | . | | | |   | | . | .'| . | -_|  _|
|_|___|___|_  |___|  |____/|___|_____|_|_|_|___|__,|___|___|_|  
      |___|""")


def main() -> None:
    print_banner()
    os.chdir(get_script_dir())
    cfg = parse_cfg()
    make_dirs(cfg.outPath)

    token = cfg.token
    if not token:
        token = auth(cfg.email, cfg.password)

    user_id = get_user_info(token)
    sub_info = get_sub_info(token)
    legacy_token, ugu_id = extract_leg_token(token)

    plan_desc, is_promo = get_plan(sub_info)
    if not sub_info.get("isContentAccessible"):
        plan_desc = "no active subscription"
    print(f"Signed in successfully - {plan_desc}\n")

    stream_params = parse_stream_params(user_id, sub_info, is_promo)
    had_item_error = False

    for idx, url in enumerate(cfg.urls, start=1):
        print(f"Item {idx} of {len(cfg.urls)}:")
        item_id, media_type = check_url(url)
        if not item_id:
            print("Invalid URL:", url)
            had_item_error = True
            continue
        try:
            if media_type == 0:
                album(item_id, cfg, stream_params, None)
            elif media_type in (1, 2):
                playlist(item_id, legacy_token, cfg, stream_params, False)
            elif media_type == 3:
                catalog_plist(item_id, legacy_token, cfg, stream_params)
            elif media_type in (4, 10):
                video(item_id, "", cfg, stream_params, None, False)
            elif media_type == 5:
                artist(item_id, cfg, stream_params)
            elif media_type in (6, 7, 8):
                video(item_id, "", cfg, stream_params, None, True)
            elif media_type == 9:
                paid_lstream(item_id, ugu_id, cfg, stream_params)
        except Exception as e:
            had_item_error = True
            handle_err("Item failed.", e, False)

    if had_item_error:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
