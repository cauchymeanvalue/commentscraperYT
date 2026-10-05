"""
YouTube comment scraper: the core logic.

Used by:
  - app.py                               (Streamlit website)
  - commentscraper_user_friendly_ui.py   (desktop window)
  - this file directly                   (terminal: python commentscraper_actual_job_script.py)

The API key is read from the environment variable YT_API_KEY:
  - locally:  from a .env file next to this script (never commit it)
  - on Render: from the service's Environment settings
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass  # fine if the key is set as a normal environment variable

OUTPUT_DIR = Path(__file__).with_name("exports")
FIELDS = ["comment_id", "parent_id", "is_reply", "author", "text",
          "likes", "published_at", "updated_at", "reply_count"]


# ---------- link parsing ----------

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(link: str) -> str | None:
    """Accepts watch links, youtu.be, shorts, live, embed, or a bare ID."""
    link = link.strip()
    if VIDEO_ID_RE.match(link):
        return link
    if "://" not in link:
        link = "https://" + link
    url = urlparse(link)
    host = url.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        candidate = url.path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        if url.path == "/watch":
            candidate = parse_qs(url.query).get("v", [""])[0]
        else:
            parts = url.path.strip("/").split("/")
            is_known_path = len(parts) > 1 and parts[0] in ("shorts", "live", "embed", "v")
            candidate = parts[1] if is_known_path else ""
    else:
        return None
    return candidate if VIDEO_ID_RE.match(candidate) else None


# ---------- API calls with retries ----------

class FatalApiError(Exception):
    """Errors that retrying won't fix (quota, disabled comments, bad key)."""


FATAL_REASONS = {
    "quotaExceeded": "Daily API quota used up. It resets at midnight Pacific time.",
    "commentsDisabled": "Comments are turned off for this video.",
    "videoNotFound": "Video not found (private, deleted, or wrong link).",
    "forbidden": "Access denied for this video.",
    "keyInvalid": "The API key is invalid.",
}


def execute(request, tries: int = 5):
    """Run an API request, retrying temporary errors with growing pauses."""
    for attempt in range(1, tries + 1):
        try:
            return request.execute()
        except HttpError as e:
            reason = ""
            try:
                reason = e.error_details[0].get("reason", "")
            except (AttributeError, IndexError, TypeError):
                pass
            if reason in FATAL_REASONS:
                raise FatalApiError(FATAL_REASONS[reason]) from e
            if e.resp.status < 500 and e.resp.status != 429:
                raise FatalApiError(f"API error {e.resp.status}: {reason or e}") from e
        except (OSError, TimeoutError):
            pass  # network hiccup: retry below
        if attempt == tries:
            raise FatalApiError("Gave up after repeated network/server errors.")
        wait = 2 ** attempt
        print(f"  temporary error, retrying in {wait}s...")
        time.sleep(wait)


def get_video_title(youtube, video_id: str) -> str:
    resp = execute(youtube.videos().list(part="snippet", id=video_id))
    if not resp.get("items"):
        raise FatalApiError(FATAL_REASONS["videoNotFound"])
    return resp["items"][0]["snippet"]["title"]


def row(snippet: dict, comment_id: str, parent_id: str = "", reply_count: int = 0) -> dict:
    """Turn one API comment into a flat dictionary (one row in the export)."""
    text = snippet.get("textOriginal", snippet.get("textDisplay", ""))
    return {
        "comment_id": comment_id,
        "parent_id": parent_id,
        "is_reply": bool(parent_id),
        "author": snippet.get("authorDisplayName", ""),
        "text": text.replace("\r", "").replace("\n", " ").strip(),
        "likes": snippet.get("likeCount", 0),
        "published_at": snippet.get("publishedAt", ""),
        "updated_at": snippet.get("updatedAt", ""),
        "reply_count": reply_count,
    }


def get_all_replies(youtube, parent_id: str) -> list[dict]:
    """commentThreads only includes ~5 replies per thread; this fetches all of them."""
    replies = []
    request = youtube.comments().list(part="snippet", parentId=parent_id,
                                      maxResults=100, textFormat="plainText")
    while request:
        resp = execute(request)
        for c in resp.get("items", []):
            replies.append(row(c["snippet"], c["id"], parent_id))
        request = youtube.comments().list_next(request, resp)
    return replies


def get_all_comments(youtube, video_id: str, progress=None) -> list[dict]:
    """All comments and replies of a video, replies directly after their parent.

    progress: optional function called with the running count
              (used by the website and the desktop window).
    """
    comments = []
    request = youtube.commentThreads().list(
        part="snippet,replies", videoId=video_id, maxResults=100,
        textFormat="plainText", order="time")
    while request:
        resp = execute(request)
        for item in resp.get("items", []):
            top = item["snippet"]["topLevelComment"]
            total = item["snippet"].get("totalReplyCount", 0)
            comments.append(row(top["snippet"], top["id"], reply_count=total))

            included = item.get("replies", {}).get("comments", [])
            if total > len(included):
                comments.extend(get_all_replies(youtube, top["id"]))
            else:
                comments.extend(row(r["snippet"], r["id"], top["id"]) for r in included)
        if progress:
            progress(len(comments))
        else:
            print(f"  {len(comments)} comments so far...", end="\r")
        request = youtube.commentThreads().list_next(request, resp)
    if not progress:
        print()
    return comments


# ---------- building the file contents ----------

def safe_filename(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "", title).strip()
    return re.sub(r"\s+", "_", cleaned)[:60] or "video"


def csv_text(comments: list[dict]) -> str:
    """The CSV content as a string (the website sends this as a download)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(comments)
    return buf.getvalue()


def txt_text(comments: list[dict], title: str, video_id: str) -> str:
    """A readable TXT version, replies indented under their comment."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"{title}\nhttps://youtu.be/{video_id}\n",
        f"{len(comments)} comments, exported {stamp}\n",
        "=" * 60 + "\n\n",
    ]
    for c in comments:
        date = c["published_at"][:10]  # "2026-09-24T08:15:00Z" -> "2026-09-24"
        if c["is_reply"]:
            lines.append(f"    ↳ {c['author']} · {date} · 👍 {c['likes']}\n")
            lines.append(f"      {c['text']}\n\n")
        else:
            lines.append(f"{c['author']} · {date} · 👍 {c['likes']}\n")
            lines.append(f"{c['text']}\n\n")
    return "".join(lines)


# ---------- saving to disk (terminal + desktop versions) ----------

def _export_path(title: str, video_id: str, ext: str, output_dir) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return output_dir / f"{safe_filename(title)}_{video_id}_{stamp}.{ext}"


def save_csv(comments: list[dict], title: str, video_id: str,
             output_dir: Path = OUTPUT_DIR) -> Path:
    path = _export_path(title, video_id, "csv", output_dir)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # -sig: Excel reads emoji correctly
        f.write(csv_text(comments))
    return path


def save_txt(comments: list[dict], title: str, video_id: str,
             output_dir: Path = OUTPUT_DIR) -> Path:
    path = _export_path(title, video_id, "txt", output_dir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt_text(comments, title, video_id))
    return path


# ---------- terminal version ----------

def main():
    api_key = os.environ.get("YT_API_KEY")
    if not api_key:
        sys.exit("No API key found. Create a .env file next to this script "
                 "with the line:  YT_API_KEY=your_key_here")

    youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    print("YouTube comment exporter. Paste a video link (or press Enter to quit).")

    while True:
        link = input("\nVideo link: ").strip()
        if not link:
            break
        video_id = extract_video_id(link)
        if not video_id:
            print("  That doesn't look like a YouTube video link, try again.")
            continue
        try:
            title = get_video_title(youtube, video_id)
            print(f"  Fetching comments for: {title}")
            comments = get_all_comments(youtube, video_id)
            path = save_txt(comments, title, video_id)
            top = sum(not c["is_reply"] for c in comments)
            print(f"  Done: {top} comments + {len(comments) - top} replies")
            print(f"  Saved to {path}")
        except FatalApiError as e:
            print(f"  Stopped: {e}")
        except KeyboardInterrupt:
            print("\n  Cancelled this video.")

    print("thank you for using me!")


if __name__ == "__main__":
    main()
