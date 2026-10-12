# City Afterimages — feedback-driven v2 revision delivery

Local-only, bounded delivery of ten revised scores produced from the reviewer
feedback on the frozen v1 audition collection. Nothing is published,
downloaded, rated, or deleted; v1 artifacts stay byte-for-byte untouched.

## Collections

| Role | Collection id | Location |
| --- | --- | --- |
| Frozen parent (v1) | `city-afterimages-20260927` | `exports/city-afterimages-20260927/`, `beats/<id>-v1` |
| New comparison (v1+v2) | `city-afterimages-20261013-v2` | `exports/city-afterimages-20261013-v2/`, `beats/<id>-v2-20261013` |

The prepared payloads are read (never mutated) from
`library/instruments/city-afterimages-20260927/prepared-index.json`. Each
prepared record is deep-cloned in memory and only the clone's `run_id` changes:
suffix `-v1` becomes `-v2-20261013` (e.g. `city-afterimages-03-v1` →
`city-afterimages-03-v2-20261013`).

## Run

```
python -m examples.render_city_feedback --media-root /path/to/media-root
```

Runtime imports (render/export pipeline, `soundfile`, the v1 renderer helpers
`plan_for`/`loudness`, the `real_record_delivery` helpers, and the dedicated
`examples.city_feedback_scores.build_score` builder) happen inside `run()` so
the module imports cleanly in test environments. There is deliberately **no
fallback** to the v1 `ten_beat_scores` builder: if the revision builder is
missing the run fails with `ImportError`.

All ten parents are verified before any new render. Per v2 track the renderer:

1. verifies the v1 parent on disk before doing any work — real
   `full_mix.wav` and `score.json` bytes must match both the v1
   `collection.json` (`mix_sha256`) and `render-evidence.json`
   (`mix_sha256`, `score_sha256`), and `editable_path` must point at the
   existing package;
2. builds the revised score (same schema; Chinese `metadata.revision_summary`),
   attaches `instrument_sources` from the prepared clone exactly like v1, and
   drops eventless optional tracks;
3. renders to an independent `beats/<new-id>` directory and exports an
   independent `Projects/<new-id>` package containing ALS stems, source MIDI,
   chop rack + phrase MIDI, listening bundle, sources, source map and a
   delivery manifest — same artifact kinds and the same helper call sequence
   as v1. No Live open/sound/reopen claim is made; `keep` stays `null`.

## Resume and overwrite rules

- An existing v2 song resumes only when its stored `score.json` is **exactly
  equal** to the requested score; a different score raises.
- A package directory that exists without `delivery_manifest.json` is treated
  as incomplete and blocks the run; it is never overwritten or cleaned up.
- Resuming a completed package re-hashes and re-sizes **every recorded file**
  in the delivery manifest (plus parent binding and `keep: null`), not just
  manifest presence.
- Preview WAVs are encoded in memory first: an existing file with different
  bytes blocks the run instead of being overwritten. New MP3s are encoded,
  decoded and registered in `audio/preview-evidence.json`. Existing MP3s must
  match the stored SHA and decode to finite audio of the expected duration,
  channels and sample rate. Missing receipts or corrupt completed media block
  recovery; they are not silently regenerated.

## Comparison `collection.json` (20 tracks)

Tracks are adjacent pairs, same `number` 1..10, unique ids: v1 entry then its
v2 revision. The file passes `examples.album_audition.load_manifest` (server
accepts 1..20 tracks) and only declares safe in-collection relative media
paths.

- titles/directions carry explicit `v1 原版` / `v2 反馈修订版` labels; the v2
  direction quotes the builder's concise Chinese change summary;
- v1 `editable_path` points at the existing v1 package, v2 at the new package;
- `version` stays `v1`/`v2`; preview SHA (`matched_audio_sha256`) and master `mix_sha256` are
  recorded separately; zero-gain previews may legitimately have the same SHA;
- all 20 previews live only under the new collection (originals are never
  mutated) and are loudness-matched with one linear gain per track to
  `min(-20, min measured LUFS)`; resulting preview loudness must match the
  shared target within 0.15 LU; gains may only attenuate;
- `keep: null` everywhere — human Keep/ratings are never copied to revisions,
  feedback is never read, and no `feedback.json` is created here. New feedback
  simply lands in the new collection directory when the audition server runs;
- `purpose` is `original-vs-revision audition`; copy never claims the reviewer
  liked or approved v2.

`render-evidence.json` stores, per entry: actual QA, LUFS/true-peak, parent
binding (v2: parent run id/mix SHA/score SHA; v1: `parent: null`), own score
and mix SHAs, preview gain/loudness/SHA and the per-file artifact hashes.

## Safety / non-goals

- No network, credentials, installs, database writes, git, schedulers,
  agents or server launch. Real media is read locally by the parent operator;
  only public code and synthetic fixtures enter the Coding Plan workspace.
- Parent feedback data stays in its old directory; it is operator-only input
  and is never bundled.
- No commercial claims, no subjective/quality guarantees.
