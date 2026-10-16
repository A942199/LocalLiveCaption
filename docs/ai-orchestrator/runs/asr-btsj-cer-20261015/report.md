<!-- ai-orchestrator:checkpoint:v1:asr-btsj-cer-20261015 -->
# Task report: asr-btsj-cer-20261015

- Repository: `A942199/LocalLiveCaption`
- Proposed branch: `agent/asr-btsj-cer-20261015`
- Code HEAD before memory writes: `8fa21c1058476b7d7949f987bebe6196ebb1dae7`
- Outcome: `BLOCKED`

## Objective and constraints
Run a true same-audio / same-gold-reference comparison of original BTSJ 331, 332, 333 Japanese audio before vs after local Qwen3-ASR Python fixes; examine strict/normalized CER, insertions, deletions, substitutions, per-segment losses, forced sentence breaks and final subtitle drops before changing overlap. Do not send source audio/text to external services. Preserve dirty checkout. No main merge.

## Sources checked and observed
- Windows checkout: `C:\Users\uu\Documents\webcodex\live-caption`; existing `main` checkout still dirty and uncommitted with prior repaired ASR logic. No production code was changed this run.
- `REALTIME_CAPTION_OPTIMIZATION_PLAN.md` previously reported BTSJ 331/332/333 total 3646.54 seconds, CER 39.660%, RTF 0.0257. These are HISTORICAL ONLY, not a new result.
- Original temp experiment workspace `C:\Users\uu\AppData\Local\Temp\btsj23-livecaption`: 138 directories, **0 files**, no audio, labels or old per-sample results.
- Targeted searches of Windows Documents, Downloads, managed worktrees, Desktop, Music, Videos, Temp, .cache and a file library did not produce the exact original BTSJ recordings or gold annotations.
- Saved integration report `tests/results/tts_file_1200s_20261010-161102/test-report.md`: 1200s of synthetic Japanese novel speech replay, 134 finals; 133 `maxlen`, 1 `shutdown`; VAD counted speech for 5999/6000 windows. Original novel MP3 path from archived `summary.json` is absent. This is an inference about segmentation prevalence only, NOT measured CER or data loss.
- No active inference/benchmark against original samples was performed because the exact fixed corpus is not accessible.

## Benchmark results
- Same-corpus strict CER before: **not measured now**
- Same-corpus strict CER after: **not measured**
- Delta CER: **unavailable**
- Deletion/insertion/substitution counts: **unavailable**
- Sentence-boundary error and subtitle omissions: **unavailable**
- This is a missing-input/data blocker, not evidence the previous code repair did or did not improve accuracy.

## Decision
Do **not** add segment-overlap/deduplication algorithm based solely on the old maxlen counts. First restore original BTSJ 331/332/333 audio and matching references, verify sample IDs and files/SHA, preserve inference/config and CER normalization parity, then test the old and repaired code on the same inputs. Recompare forced-cut boundaries with 0-overlap vs controlled candidates only after reliable labels. Prevent cross-corpus claims.

## GitHub evidence
- https://github.com/A942199/LocalLiveCaption/issues/3
- https://github.com/A942199/LocalLiveCaption/pull/2

## Release guard
No source-code commits, model changes, benchmark artifacts, GitHub CI, independent review or main-branch merge occurred in this run. Never report historical CER as post-fix performance.
