# Ten-beat audition server — usage and semantics

`examples/album_audition.py` is a **Python standard-library only** localhost
server that provides a human-guided listening and durable style-rating
workspace for the ten-track first-stage draft batch. It never executes media,
calls models, publishes anything, or rerenders a beat from a score.

## Starting the server

```sh
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python examples/album_audition.py --collection /path/to/collection_dir --port 8800
```

Then open <http://127.0.0.1:8800/>. Options:

- `--collection PATH` — directory containing `collection.json` (required)
- `--port INT` — TCP port (default `8800`)
- `--host HOST` — bind address (default `127.0.0.1`; do not expose broadly)

Programmatic use:

```python
from examples.album_audition import create_server
server = create_server("/path/to/collection_dir")  # ThreadingHTTPServer
server.serve_forever()
```

## Collection layout (`collection.json`)

The parent supplies `collection.json` in the collection directory:

```json
{
  "id": "collection-id",
  "title": "合集标题",
  "subtitle": "副标题 / 主题说明",
  "tracks": [
    {
      "id": "t01",
      "number": 1,
      "title": "曲目标题",
      "bpm": 90,
      "duration_seconds": 60,
      "direction": "改编方向说明",
      "audio": "audio/t01.mp3",
      "original_audio": "audio/t01.wav",
      "source_summary": [{"title": "来源唱片", "source_url": "https://..."}],
      "editable_path": "/absolute/local/stem/dir",
      "version": "v1",
      "mix_sha256": "64 hex chars",
      "change_point_seconds": 15
    }
  ]
}
```

- Normally **exactly 10** tracks; the server accepts **1..20** for partial batches and tests.
- `audio` / `original_audio` must be relative paths inside the collection
  directory (no absolute paths, no `..` segments).
- `mix_sha256` must be 64 hex characters; the server locks feedback to the
  exact `version` + `mix_sha256` declared for each track.

## Rating semantics (what every field means)

Per track the page offers:

| Key | 中文 | Meaning |
|---|---|---|
| `groove` | 律动 | Does the beat move / swing / pocket feel right? |
| `sample` | 采样记忆点 | Is the sampled motif memorable and well used? |
| `variation` | 变化 | Are section changes / contrast sufficient? |
| `clarity` | 清晰度 | Is the mix legible (elements distinguishable)? |
| `rap_space` | 留给人声的空间 | Is there usable room for a vocal on top? |

- Every dimension is **optional**: blank means `null`, not zero. Only
  integers **1..5** are accepted; booleans, floats, and out-of-range values
  are rejected.
- `keep` is one of `keep` / `revise` / `reject` / `undecided`
  （保留 / 待改 / 不要 / 未定）, default `undecided`.
- `notes` is a free-text field; `timestamp_notes` are
  `{seconds, text}` pairs captured from the playing audio ("记录当前播放
  位置"), each clickable to seek back. The "⚑ 跳到段落变化" button seeks to
  the manifest's `change_point_seconds`.
- Submissions are **stated human observations only**. The server never
  auto-generates opinions and never fills positive defaults.

## Persistence and version locking

- Feedback is written to `feedback.json` **inside the collection directory**,
  atomically (temp file + `os.replace`) under a process-wide threading lock.
- One record per track; each record carries `updated_at` in UTC and the
  submitted `version` / `mix_sha256`.
- POST bodies whose `version` / `mix_sha256` do not match the manifest are
  rejected `409 stale_version`. A track already saved under one
  version/hash cannot be overwritten by a different version/hash.
- An exact duplicate POST is idempotent: the same record is returned with no
  write, so no duplicate entries are created.
- Rejected/invalid requests (bad rating, oversize 64 KiB body, unknown
  track/field) never mutate stored feedback; a failed write leaves the
  previous `feedback.json` intact.
- `GET /api/feedback/download` returns the same JSON as
  `GET /api/feedback` with a `Content-Disposition: attachment` header.

## HTTP surface

| Method/route | Purpose |
|---|---|
| `GET /` | Simplified-Chinese listening page (warm light editorial theme) |
| `GET /api/collection` | The validated manifest |
| `GET /api/feedback` | `{collection_id, ratings: {track_id: record}}` |
| `POST /api/feedback` | Validate + persist one track's feedback |
| `GET /api/feedback/download` | Feedback JSON as attachment |
| `GET/HEAD /media/<relative>` | Declared media only; MIME, length, single byte `Range` (`206`/`416`) |

Media safety: only paths declared as a track's `audio` or `original_audio`
are served. `collection.json`, `feedback.json`, path traversal
(`..`, percent-encoded escapes), and symlinks resolving outside the
collection directory are all rejected. No CORS headers are emitted and the
server binds to localhost by default. All metadata/user text is escaped
server-side where embedded; the frontend renders dynamic data exclusively
through `textContent` / DOM APIs (never `innerHTML`).

## Explicit limitations

- **Not a blind review.** Track titles, BPM, direction, source summaries, and
  original-audio links are visible; ratings are informed opinions, not
  anonymized A/B results.
- **Not automated musical judgment.** Nothing here scores music automatically;
  all ratings and notes come from the human reviewer.
- **No Music Keep or commercial-clearance decision.** `keep` and the ratings
  are inputs for the next *human-reviewed* revision. They do not approve
  sale/release, do not clear samples, and do not trigger rerendering,
  publication, or any project write.
- **Local trust model only.** There is no authentication; bind to localhost.
