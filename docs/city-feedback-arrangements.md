# City Afterimages — feedback revision map (v2, 2026-10-13)

Collection: `city-afterimages-20261013-v2`. Each v2 score is produced from the
frozen v1 score by `examples/city_feedback_scores.py`: BPM, bars, sections,
slices, source records, sample files, tail/fade and license are unchanged. The
v1 builder, its globals and the prepared payload are never mutated. Drum-layer
samples are the prepared kit only; no samples are synthesised.

| # | Phrases / samples | Drum revision | Bass / keys / gain |
|---|---|---|---|
| 01 | Lead motif placements frozen | Kit rebuilt as 92 BPM half-time drill pocket: backbeat snare on bar-local beat 2, kick strong/soft transient contrast (1.0 vs ~0.5), sparse offbeat hats, one 16th hat roll in verse bar 4 and the final return bar; drop thinned to kick+snare+single hat | unchanged |
| 02 | Middle rephrased as repeated `lead_b`/`answer_b` call+answer (two identical calls, one full-bar rest, closing long answer); piano/jazz timbre retained | Sparser rim-based jazz pocket throughout; middle is kick-only with a one-bar stop; snare never used | Added key bed removed (sampling focus, less masking) |
| 03 | All phrase placements frozen; primary kick/snare timing frozen | Rim colour dropped in the final turn bar (gap); soft hat double added in the last return bar | All added drums −4 dB, bass −2 dB |
| 04 | Source motif placements frozen; no material replaced | Stomp/clap wall removed (clap track gone), busy 8th hats replaced by sparse single hats; rim backbeat support instead of claps | All added drums −6 dB |
| 05 | Phrases frozen; middle is the intentional quiet turn | New half-time anchor (snare beat 2), alternating two-bar kick pockets, sparse hats, one roll per 4-bar group; middle: kick-only bars, two fully silent bars, restrained rim pickup; return re-anchors | Bass reduced to downbeat roots; added keys reduced to root notes only |
| 06 | Ten-slice alternation replaced by a repeatable `lead_b`/`answer_b` motif with explicit rest bars; `turn_b` kept once as the long colour in the drop; intro/outro statements kept | Sparse kick/rim verse, kick-only drop, minimal half-backbeat return | All added drums −6 dB; distracting added keys removed |
| 07 | Jazz cuts preserved (subset used); funk/return organised as strict two-bar question (`lead_c`)/answer (`answer_c`) with one `lead_a` colour; one `turn_a` long phrase in the switch | Clap doubling removed; 16th-hat wall replaced by two hats/bar over a beat-2 snare anchor; single end-of-section roll | unchanged; key events overlapping new phrases removed |
| 08 | All phrase placements, files, gains, processing frozen; bass and key tracks byte-identical to v1 | Only drum patterns/gains change: restrained bounce (backbeat snare, two hats), roll in the last bounce bar and return bar 4; middle is groove bar / kick-only bar / full stop / rim pickup | Added drums −2 dB |
| 09 | Every track event and sample setting frozen; arrangement preserved | Patterns frozen | Only gains: added drums −2 dB, bass −1 dB |
| 10 | Fewer source switches: recurring `lead_a`/`answer_a` theme, long `turn_a`/`turn_b` phrases with rest bars in the middle and a long turn in the reprise | Warm backbeat without open hats; hats cut to two per bar; middle drums only on the long-phrase bars | Added keys reduced to root notes |

## Validation properties (covered by `tests/test_city_feedback_scores.py`)

- Ten deterministic scores; building twice is identical; prepared input and
  `ten_beat_scores` globals are unchanged after builds.
- `sample_flip.placements` mirror the phrase-track events exactly (6 dp), are
  sorted, non-overlapping, within `target_beats`, and inside their section and
  the song.
- Bass stays monophonic and only uses active-slice `bass_midi` notes; no added
  key event overlaps a phrase window; all velocities/gains are finite.
- Frozen-direction checks: 03 phrases and kick/snare timing unchanged with
  −4/−2 dB moves; 08 non-drum tracks deep-equal v1; 09 only the specified gain
  fields change.
- Density reductions (04/05/06/07/10), clap removal (04/07), half-time anchors
  (01/05), call/answer grids with rests (02/06/07) and long-theme returns (10)
  are asserted per direction. No clip/level guarantee is made pre-render; the
  local renderer sets the master and verifies audio.
