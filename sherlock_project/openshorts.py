"""
OpenShorts integration: turn (live) streams into short vertical clips.

Configuration (environment variables):
    OPENSHORTS_API_URL  Base URL, default https://api.openshorts.app
                        (use e.g. http://localhost:8000 for a self-hosted instance)
    OPENSHORTS_API_KEY  API key (osk_...), sent as X-API-Key. Not needed for self-hosted.

Usage:
    python -m sherlock_project.openshorts URL                  # finished video / VOD
    python -m sherlock_project.openshorts URL --live 120       # record 120s of a live stream first
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional

import requests

DEFAULT_API_URL = "https://api.openshorts.app"
TERMINAL_OK = {"completed", "complete", "done", "success", "succeeded", "finished"}
TERMINAL_FAIL = {"failed", "error", "cancelled", "canceled"}


class OpenShortsError(Exception):
    pass


class OpenShortsClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 60):
        self.base_url = (base_url or os.environ.get("OPENSHORTS_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENSHORTS_API_KEY")
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = requests.request(
            method, self.base_url + path, headers=self._headers(), timeout=kwargs.pop("timeout", self.timeout), **kwargs
        )
        if not resp.ok:
            raise OpenShortsError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def process_url(self, url: str, **options) -> str:
        """Submit a video URL. Returns the job id. Options: target_clips, clip_min_seconds, ...,
        source_start, source_end, webhook_url, webhook_secret."""
        form = {"url": url, "acknowledged": "true"}
        form.update({k: str(v) for k, v in options.items() if v is not None})
        return self._job_id(self._request("POST", "/api/process", data=form))

    def process_file(self, path: str, **options) -> str:
        """Upload a local video (reserve slot, PUT bytes) and process it. Returns the job id."""
        slot = self._request("POST", "/api/uploads", json={"filename": os.path.basename(path)})
        upload_id = slot.get("upload_id") or slot.get("id")
        if not upload_id:
            raise OpenShortsError(f"Unexpected upload response: {slot}")
        with open(path, "rb") as fh:
            self._request("PUT", f"/api/uploads/{upload_id}", data=fh, timeout=600)
        form = {"upload_id": upload_id, "acknowledged": "true"}
        form.update({k: str(v) for k, v in options.items() if v is not None})
        return self._job_id(self._request("POST", "/api/process", data=form))

    @staticmethod
    def _job_id(payload: dict) -> str:
        job_id = payload.get("job_id") or payload.get("id")
        if not job_id:
            raise OpenShortsError(f"No job id in response: {payload}")
        return job_id

    def status(self, job_id: str) -> dict:
        return self._request("GET", f"/api/status/{job_id}")

    def wait(self, job_id: str, poll: float = 5.0, timeout: float = 3600.0) -> dict:
        """Poll until the job finishes; returns the final status payload."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.status(job_id)
            state = str(data.get("status", "")).lower()
            if state in TERMINAL_OK:
                return data
            if state in TERMINAL_FAIL:
                raise OpenShortsError(f"Job {job_id} failed: {data}")
            time.sleep(poll)
        raise OpenShortsError(f"Job {job_id} did not finish within {timeout}s")


def resolve_stream_url(url: str) -> str:
    """Resolve a page URL (e.g. YouTube) to a direct media URL using yt-dlp, if installed."""
    if shutil.which("yt-dlp") is None:
        return url
    out = subprocess.run(["yt-dlp", "-g", "-f", "best", url], capture_output=True, text=True, timeout=120)
    lines = out.stdout.split()
    if out.returncode != 0 or not lines:
        raise OpenShortsError(f"yt-dlp could not resolve {url}: {out.stderr[:300]}")
    return lines[0]


def record_stream(url: str, seconds: int, out_path: str) -> str:
    """Record `seconds` of a live stream to an mp4 with ffmpeg."""
    if shutil.which("ffmpeg") is None:
        raise OpenShortsError("ffmpeg is required to record a live stream")
    media = resolve_stream_url(url)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", media, "-t", str(seconds), "-c", "copy", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise OpenShortsError(f"ffmpeg failed: {proc.stderr[:300]}")
    return out_path


def clip_live_stream(client: OpenShortsClient, url: str, seconds: int, **options) -> dict:
    """Record the stream, upload the recording and wait for the finished clips."""
    with tempfile.TemporaryDirectory() as tmp:
        path = record_stream(url, seconds, os.path.join(tmp, "live.mp4"))
        job_id = client.process_file(path, **options)
    return client.wait(job_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create short clips from a (live) stream via OpenShorts")
    parser.add_argument("url", help="Video or stream URL")
    parser.add_argument("--live", type=int, metavar="SECONDS", help="Record this many seconds from a live stream first")
    parser.add_argument("--clips", type=int, help="Target number of clips")
    parser.add_argument("--min-seconds", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument("--no-wait", action="store_true", help="Print the job id and exit")
    args = parser.parse_args(argv)

    client = OpenShortsClient()
    options = {
        "target_clips": args.clips,
        "clip_min_seconds": args.min_seconds,
        "clip_max_seconds": args.max_seconds,
    }
    try:
        if args.live:
            result = clip_live_stream(client, args.url, args.live, **options)
        else:
            job_id = client.process_url(args.url, **options)
            print(f"job: {job_id}")
            if args.no_wait:
                return 0
            result = client.wait(job_id)
    except OpenShortsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for clip in result.get("clips") or result.get("result", {}).get("clips", []):
        print(clip.get("download_url") or clip.get("video_url") or clip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
