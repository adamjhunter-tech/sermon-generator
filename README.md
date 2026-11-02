# Sermon Library Generator

Generates JSON files for the [sermon-library](https://github.com/adamjhunter-tech/sermon-library) site from the OBPC Lessons RSS feed.

## What it does

1. Fetches the public SoundCloud RSS feed.
2. Skips episodes that already have a matching JSON file in `data/`.
3. Downloads the MP3 and transcribes it with Groq Whisper.
4. Calls a Groq LLM to extract title, speaker, series, tags, scripture references, outline, and summary.
5. Writes a JSON file that matches the existing site schema.

## Requirements

- Python 3.9+
- `GROQ_API_KEY` environment variable (free tier at https://console.groq.com)


## Rate limits

The free-tier Groq analysis model (`qwen/qwen3.8-27b`) has a low output-token limit.  
**Keep manual runs at 9 episodes or fewer** to avoid 429 rate-limit errors.  
The Monday scheduled run uses `--limit 4` and stays well under the limit.

If a run hits a 429, the failed episode is simply skipped and will be retried on the next run.

## Usage

```bash
# Process up to 5 new episodes
GROQ_API_KEY=gsk_... python generate_sermons.py

# See what would be created
python generate_sermons.py --dry-run

# Re-process a specific episode
python generate_sermons.py --force "Thomas Aquinas"

# Point at the real data directory
DATA_DIR=/path/to/sermon-library/data python generate_sermons.py
```

## GitHub Actions

Add `.github/workflows/generate-sermons.yml` to the sermon-library repo:

```yaml
name: Generate new sermons
on:
  schedule:
    - cron: "0 14 * * 1"   # Monday 14:00 UTC
  workflow_dispatch:

jobs:
  generate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Run generator
        env:
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
          DATA_DIR: data
        run: python generate_sermons.py --limit 4
      - name: Commit and push
        run: |
          git config user.name "sermon-bot"
          git config user.email "bot@example.com"
          git add data/
          git diff --staged --quiet || git commit -m "Add new sermon transcripts"
          git push
```

Store `GROQ_API_KEY` as a repository secret.

## Notes

- Audio is downloaded only temporarily and is not stored.
- The script is intentionally dependency-free (stdlib only).
- If the LLM call fails, a minimal stub is still written so the episode is not lost.
- Topic lists should be reviewed occasionally; the prompt asks for concise tags, but periodic cleanup may still be useful.
