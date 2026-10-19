"""
Posts new YouTube Shorts from one channel to a Discord channel as real video files.
Runs on GitHub Actions - nothing touches your own PC.

Required environment variables (set as GitHub secrets):
  YT_CHANNEL_ID        channel ID (UCxxxx...), @handle, or channel URL
  DISCORD_WEBHOOK_URL  the webhook URL from your Discord channel
Optional:
  MAX_UPLOAD_MB        upload size cap, default 9.5 (Discord's standard limit is 10 MB)
"""

import glob
import json
import os
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
import yt_dlp

CHANNEL_ID = os.environ["YT_CHANNEL_ID"].strip()
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"].strip()
MAX_BYTES = int(float(os.environ.get("MAX_UPLOAD_MB") or "9.5") * 1024 * 1024)
STATE_FILE = Path("posted.json")
COOKIES = "cookies.txt" if Path("cookies.txt").exists() else None
MAX_ATTEMPTS = 5  # give up on a video after this many failed runs

FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def load_state():
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    state["seen"] = state["seen"][-300:]  # keep the file small
    STATE_FILE.write_text(json.dumps(state, indent=2))


def channel_base():
    """Accepts a channel ID (UC...), an @handle, or a full channel URL."""
    c = CHANNEL_ID
    if c.startswith("http"):
        return c.rstrip("/").removesuffix("/shorts").removesuffix("/videos")
    if c.startswith("@"):
        return f"https://www.youtube.com/{c}"
    return f"https://www.youtube.com/channel/{c}"


def fetch_shorts_tab():
    """List the newest Shorts straight from the channel's Shorts tab."""
    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": "in_playlist", "playlistend": 15}
    if COOKIES:
        opts["cookiefile"] = COOKIES
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_base() + "/shorts", download=False)
    items = [(e["id"], e.get("title") or "") for e in (info.get("entries") or []) if e.get("id")]
    return list(reversed(items))  # oldest first


def fetch_rss():
    """Backup: the channel RSS feed (only works with a UC... channel ID)."""
    if not CHANNEL_ID.startswith("UC"):
        raise RuntimeError("RSS backup needs a UC... channel ID")
    with urllib.request.urlopen(FEED_URL, timeout=30) as resp:
        root = ET.fromstring(resp.read())
    items = []
    for entry in root.findall("a:entry", NS):
        vid = entry.find("yt:videoId", NS).text
        title = entry.find("a:title", NS).text or ""
        items.append((vid, title))
    return list(reversed(items))


def fetch_feed():
    """Return [(video_id, title)] oldest first, trying the Shorts tab then RSS."""
    errors = []
    for name, fn in (("Shorts tab", fetch_shorts_tab), ("RSS feed", fetch_rss)):
        try:
            items = fn()
            print(f"Got {len(items)} videos from {name}")
            return items
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"{name} failed: {e}")
    raise RuntimeError("Could not list the channel's videos. " + " | ".join(errors))


def is_short(vid, info=None):
    """A /shorts/ URL returns 200 for a Short and redirects for a normal video."""
    try:
        r = requests.head(f"https://www.youtube.com/shorts/{vid}",
                          allow_redirects=False, timeout=20)
        if r.status_code == 200:
            return True
        if 300 <= r.status_code < 400:
            return False
    except requests.RequestException:
        pass
    # Fallback: vertical and 3 minutes or less
    if info:
        w, h = info.get("width") or 0, info.get("height") or 0
        return (info.get("duration") or 999) <= 180 and h > w
    return False


def ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "outtmpl": "video.%(ext)s",
    }
    if COOKIES:
        opts["cookiefile"] = COOKIES
    if extra:
        opts.update(extra)
    return opts


def download(url):
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=True)
    files = [f for f in glob.glob("video.*") if not f.endswith(".part")]
    if not files:
        raise RuntimeError("Download produced no file")
    return files[0], info


def compress(src, duration):
    """Re-encode to fit under MAX_BYTES. Tries a few times with lower bitrates."""
    duration = max(float(duration or 60), 1.0)
    audio_bps = 96_000
    factor = 0.92
    out = "small.mp4"
    for _ in range(4):
        vbit = int(MAX_BYTES * 8 * factor / duration - audio_bps)
        vbit = max(vbit, 120_000)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-i", src,
            "-vf", "scale=-2:'min(1280,ih)'",
            "-c:v", "libx264", "-preset", "medium",
            "-b:v", str(vbit), "-maxrate", str(vbit), "-bufsize", str(vbit * 2),
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", out,
        ], check=True)
        if os.path.getsize(out) <= MAX_BYTES:
            return out
        factor *= 0.8
    raise RuntimeError("Could not compress video under the size limit")


def post_to_discord(path, title):
    payload = {"content": f"**{title}**" if title else ""}
    with open(path, "rb") as f:
        r = requests.post(
            WEBHOOK,
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": ("short.mp4", f, "video/mp4")},
            timeout=180,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Discord returned {r.status_code}: {r.text[:300]}")


def cleanup():
    for f in glob.glob("video.*") + ["small.mp4"]:
        try:
            os.remove(f)
        except FileNotFoundError:
            pass


def main():
    entries = fetch_feed()
    state = load_state()

    if state is None:
        # First run: remember what's already there so we don't spam old videos.
        save_state({"seen": [vid for vid, _ in entries], "failures": {}})
        print(f"First run - marked {len(entries)} existing videos as seen. "
              "New Shorts will be posted from now on.")
        return 0

    seen = set(state.get("seen", []))
    failures = state.setdefault("failures", {})
    had_error = False

    for vid, title in entries:
        if vid in seen:
            continue
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            if not is_short(vid):
                print(f"Skipping (not a Short): {title}")
                state["seen"].append(vid)
                seen.add(vid)
                continue

            print(f"Posting Short: {title}")
            path, info = download(url)
            if os.path.getsize(path) > MAX_BYTES:
                path = compress(path, info.get("duration"))
            post_to_discord(path, title)
            state["seen"].append(vid)
            seen.add(vid)
            failures.pop(vid, None)
            print("  done")
        except Exception as e:
            had_error = True
            failures[vid] = failures.get(vid, 0) + 1
            print(f"  FAILED ({failures[vid]}/{MAX_ATTEMPTS}): {e}")
            if failures[vid] >= MAX_ATTEMPTS:
                print("  giving up on this video")
                state["seen"].append(vid)
                seen.add(vid)
                failures.pop(vid, None)
        finally:
            cleanup()

    save_state(state)
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
