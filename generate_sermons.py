#!/usr/bin/env python3
"""
generate_sermons.py

Polls the OBPC Lessons and Sermons RSS feeds, detects new episodes, and generates
JSON files matching the sermon-library schema.

Usage:
  # Process any new episodes (requires GROQ_API_KEY for transcription)
  python generate_sermons.py

  # Dry-run: show what would be created
  python generate_sermons.py --dry-run

  # Force reprocess a specific episode by title fragment
  python generate_sermons.py --force "Thomas Aquinas"

Environment variables:
  GROQ_API_KEY   - Required for transcription (https://console.groq.com)
  DATA_DIR       - Path to sermon-library/data/ (default: ./data)
  RSS_URL        - Override RSS feed URL
"""

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────
RSS_FEEDS = [
    # OBPC Lessons (Sunday school)
    "https://feeds.soundcloud.com/users/soundcloud:users:944857777/sounds.rss",
    # OBPC Sermons (worship services)
    "https://feeds.soundcloud.com/users/soundcloud:users:189943678/sounds.rss",
]
# Allow override via env for testing a single feed
if os.environ.get("RSS_URL"):
    RSS_FEEDS = [os.environ["RSS_URL"]]
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# LLM prompt for metadata extraction (works with Groq, OpenAI, etc.)
ANALYSIS_PROMPT = """You are processing a church sermon/Sunday school transcript.
Extract structured metadata and return ONLY valid JSON with this exact shape:

{
  "title": "string (clean title, no speaker in parentheses)",
  "speaker": "string (full name with title if present, e.g. 'Pastor Tito Lyro')",
  "date": "YYYY-MM-DD",
  "session": "AM" | "PM" | null,
  "series": "string or null",
  "series_part": number or null,
  "tags": [{"tag": "string", "source": "detected"}],
  "scripture_references": [
    {"reference": "Book Chapter:Verse", "type": "quoted"|"referenced", "source": "detected"}
  ],
  "outline": [
    {
      "title": "string",
      "subpoints": ["string"],
      "scripture": ["Book Chapter:Verse"]
    }
  ],
  "summary": "string (2-4 paragraphs, pastoral tone, no first person)"
}

Rules:
- tags: 4-8 concise topics. Prefer theological terms. Do not invent series names as tags.
- scripture_references: normalize to standard form (e.g. "1 Corinthians 15:12-28"). Mark primary text as "quoted".
- outline: 3-6 major points with short subpoints.
- summary: accurate, clear, suitable for a public library page.
- date: extract from transcript or description; if unknown use the RSS pubDate.

Transcript and description:
"""

# ── Helpers ────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80]


def fetch_rss(url: str) -> list[dict]:
    """Return list of episode dicts from RSS."""
    req = urllib.request.Request(url, headers={"User-Agent": "sermon-library-generator/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    episodes = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        pub = item.findtext("pubDate") or ""
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else ""
        desc = (item.findtext("description") or item.findtext("itunes:summary", namespaces=ns) or "").strip()
        link = (item.findtext("link") or "").strip()
        episodes.append({
            "title": title,
            "pubDate": pub,
            "audio_url": audio_url,
            "description": desc,
            "link": link,
            "guid": (item.findtext("guid") or title),
        })
    return episodes


def existing_ids(data_dir: Path) -> set[str]:
    if not data_dir.exists():
        return set()
    return {p.stem for p in data_dir.glob("*.json")}


def parse_date(pub_date: str) -> str:
    """Best-effort RFC 2822 → YYYY-MM-DD."""
    if not pub_date:
        return datetime.utcnow().strftime("%Y-%m-%d")
    # email.utils handles the common RSS date formats reliably
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_date)
        if dt:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(pub_date.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return datetime.utcnow().strftime("%Y-%m-%d")


def download_audio(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "sermon-library-generator/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 64)
            if not chunk:
                break
            f.write(chunk)


def transcribe_groq(audio_path: Path) -> str:
    """Transcribe with Groq Whisper (whisper-large-v3)."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    # Minimal multipart POST (no requests dependency)
    boundary = "----SermonBoundary"
    body = []
    body.append(f"--{boundary}".encode())
    body.append(b'Content-Disposition: form-data; name="model"')
    body.append(b"")
    body.append(b"whisper-large-v3")
    body.append(f"--{boundary}".encode())
    body.append(b'Content-Disposition: form-data; name="file"; filename="audio.mp3"')
    body.append(b"Content-Type: audio/mpeg")
    body.append(b"")
    body.append(audio_path.read_bytes())
    body.append(f"--{boundary}--".encode())
    body.append(b"")
    data = b"\r\n".join(body)

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=data,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    return result.get("text", "")


def analyze_with_groq(transcript: str, description: str, rss_date: str) -> dict:
    """Call Groq LLM for structured metadata. Falls back to simple extraction if no key."""
    if not GROQ_API_KEY:
        # Minimal fallback so the script still produces a usable stub
        return {
            "title": description.split("\n")[0][:80] if description else "Untitled",
            "speaker": "Unknown",
            "date": rss_date,
            "session": None,
            "series": None,
            "series_part": None,
            "tags": [{"tag": "sermon", "source": "detected"}],
            "scripture_references": [],
            "outline": [],
            "summary": description[:500] if description else "",
        }

    prompt = ANALYSIS_PROMPT + f"\n\nDescription:\n{description}\n\nTranscript:\n{transcript[:12000]}"
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    content = result["choices"][0]["message"]["content"]
    return json.loads(content)


def build_lesson(ep: dict, analysis: dict, transcript: str) -> dict:
    date = analysis.get("date") or parse_date(ep["pubDate"])
    title = analysis.get("title") or ep["title"]
    # Prefer clean title without speaker parenthetical for id
    clean_title = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
    lesson_id = f"{date}-{slugify(clean_title)}"

    lesson = {
        "id": lesson_id,
        "title": title,
        "speaker": analysis.get("speaker") or "Unknown",
        "date": date,
        "session": analysis.get("session"),
        "series": analysis.get("series"),
        "series_part": analysis.get("series_part"),
        "parts": 1,
        "source_files": [f"{lesson_id}.txt"],
        "tags": analysis.get("tags") or [],
        "scripture_references": analysis.get("scripture_references") or [],
        "outline": analysis.get("outline") or [],
        "summary": analysis.get("summary") or "",
        "transcript": transcript,
    }
    if ep.get("link"):
        lesson["soundcloud_url"] = ep["link"]
    return lesson


def process_episode(ep: dict, data_dir: Path, dry_run: bool = False) -> Path | None:
    date = parse_date(ep["pubDate"])
    clean_title = re.sub(r"\s*\([^)]*\)\s*$", "", ep["title"]).strip()
    lesson_id = f"{date}-{slugify(clean_title)}"
    out_path = data_dir / f"{lesson_id}.json"

    if out_path.exists() and not dry_run:
        return None  # already present

    print(f"Processing: {ep['title']}")

    if dry_run:
        print(f"  [dry-run] would write {out_path}")
        return out_path

    # 1. Transcribe
    with tempfile.TemporaryDirectory() as tmp:
        audio_path = Path(tmp) / "audio.mp3"
        if ep["audio_url"]:
            print("  downloading audio...")
            download_audio(ep["audio_url"], audio_path)
            print("  transcribing...")
            transcript = transcribe_groq(audio_path)
        else:
            transcript = ep.get("description", "")

    # 2. Analyze
    print("  analyzing metadata...")
    analysis = analyze_with_groq(transcript, ep["description"], date)

    # 3. Build & write
    lesson = build_lesson(ep, analysis, transcript)
    # Recompute path in case analysis changed the date/title
    final_path = data_dir / f"{lesson['id']}.json"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(json.dumps(lesson, indent=2, ensure_ascii=False) + "\n")
    print(f"  wrote {final_path}")
    return final_path


def main():
    parser = argparse.ArgumentParser(description="Generate sermon-library JSON from RSS")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created")
    parser.add_argument("--force", type=str, help="Force process episodes whose title contains this string")
    parser.add_argument("--limit", type=int, default=5, help="Max new episodes to process (default 5)")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_episodes = []
    for feed_url in RSS_FEEDS:
        print(f"Fetching RSS: {feed_url}")
        try:
            eps = fetch_rss(feed_url)
            print(f"  Found {len(eps)} episodes")
            all_episodes.extend(eps)
        except Exception as e:
            print(f"  ERROR fetching feed: {e}", file=sys.stderr)
    episodes = all_episodes
    print(f"Total episodes across feeds: {len(episodes)}")

    existing = existing_ids(DATA_DIR)
    to_process = []
    for ep in episodes:
        date = parse_date(ep["pubDate"])
        clean = re.sub(r"\s*\([^)]*\)\s*$", "", ep["title"]).strip()
        lid = f"{date}-{slugify(clean)}"
        if args.force and args.force.lower() in ep["title"].lower():
            to_process.append(ep)
        elif lid not in existing:
            to_process.append(ep)
        if len(to_process) >= args.limit:
            break

    if not to_process:
        print("No new episodes to process.")
        return

    print(f"Will process {len(to_process)} episode(s)")
    created = []
    for ep in to_process:
        try:
            path = process_episode(ep, DATA_DIR, dry_run=args.dry_run)
            if path:
                created.append(path)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)

    print(f"Done. Created/updated {len(created)} file(s).")


if __name__ == "__main__":
    main()
