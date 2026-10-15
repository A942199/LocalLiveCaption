<!-- ai-orchestrator:checkpoint:v1:asr-accuracy-fixes-20261015 -->
# Task report: asr-accuracy-fixes-20261015

- Repository: `A942199/LocalLiveCaption`
- Proposed branch: `agent/asr-accuracy-fixes-20261015`
- Code HEAD before memory writes: `053073ac9d2498a89159118f8b3f321558dd158d`
- Outcome: `AWAITING_REVIEW`

## Observed task summary

Windows local checkout `C:\Users\uu\Documents\webcodex\live-caption`, local Git HEAD `e4d342030544b891d73a5c13c1142df31d24c0e4` (dirty worktree), was patched in place. Main file: `live-caption-ja.pyw`; new regression suite: `tests/test_asr_accuracy_fixes.py`.

Repairs:
- Force Japanese/selected language via assistant prefill in llama.cpp chat messages.
- Avoid near-zero VAD exit threshold and add bounded hysteresis timeout.
- Use sample-precise speech preroll for the configured milliseconds.
- Preserve repeated Japanese utterances in transcript cleanup.
- Share persisted ASR parameters between console and GUI while respecting explicit command-line options.
- Require matching slots, context and audio capability before reusing a detected llama-server.

## Verified tests

- `.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_*.py -v`: 57 tests passed, 0 failed.
- Python AST parse: passed.
- `git diff --check`: passed (line-ending warning on a pre-existing test file).
- Real Windows RTX 3090 GGUF Q8_0 + BF16 mmproj: llama-server started on ephemeral port, /health ready, synthetic WAV chat completion with Japanese assistant prefill returned HTTP 200. Owned server was terminated. This proves protocol viability, not Japanese transcription accuracy or hallucination suppression.

## Unresolved / release guard

- Hard `max_seg` cuts still have no proven overlap/deduplication. Changing this without aligned speech benchmarks could introduce duplicated Japanese subtitles; deferred pending audio ground truth.
- GGUF model/source precision and real Japanese CER/RTF comparison not executed.
- The server-probe improvement cannot verify mmproj file identity from /props.
- Remote GitHub `main` is WPF replacement at `75456b3c1027d68311a70a2967b06719e22f554d`; local Python repairs have NOT been committed, pushed, or merged. This draft PR tracks memory only.

## Evidence links

- https://github.com/A942199/LocalLiveCaption/issues/1
- https://github.com/A942199/LocalLiveCaption/pull/2

## Release guard

Do not merge on this document alone; independent code review, GitHub CI and user validation have not occurred.
