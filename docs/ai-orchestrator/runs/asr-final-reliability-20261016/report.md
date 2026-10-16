# ASR Final reliability optimization — agent/asr-final-reliability-20261016

Run ID: `asr-final-reliability-20261016`
State: **AWAITING_REVIEW** (not merged)

## Baselines and protection
- Remote source branch: `main` at `8b66779a1066a55909239fc84bd9c0388cf4a409`.
- User's Windows local `main` at `e4d342030544b891d73a5c13c1142df31d24c0e4`, dirty and 2 commits behind remote. It was **not** edited.
- Local isolated managed worktree: `C:\Users\uu\.webcodex-worktrees\live-caption-1f19397f`. Includes an exact file transplant from the current dirty local main (not from the remote branch).
- Files changed relative to that local transplant: `live-caption-ja.pyw`, new `tests/test_asr_final_reliability.py`.
- This GitHub documentation branch does **not** contain the local code patch. No code commit or push was done.

## Implemented behavior
1. Recognize llama-server `finish_reason=length`, retry once at doubled `max_tokens` up to 1024, raise explicit truncation error if unresolved; do not display an incomplete Final as success.
2. Retry Final inference once for transient exceptions, while preserving HTTP 4xx failures and explicit truncation errors as errors; report permanent failures and preserve diagnostic logging. Persistent failures can still cause a missing Final.
3. Preserve every nonempty VAD-confirmed active utterance at stop or queue-overflow, including speech under 0.5 seconds.
4. Prioritize Final over new Partial submissions, and discard already-obsolete Partial before inference, while preserving existing stable-prefix/UI translation behavior.

## Verification
- New tests: 4/4 failed on original candidate, 4/4 passed after patch.
- ASR suite: 48/48 passed (`test_asr_*.py`, Windows Python 3.11 virtual environment with stubbed models).
- Stable-prefix tests: 4/4 passed.
- Isolated translation upstream scheduler: 17/17 passed.
- Translation bundle parity: 1 of 2 tests failed due to embedded `engine.py` mismatch existing in source transplant.
- Full test discovery: **TIMEOUT** at 90s with Tkinter main-thread cleanup errors; not a full pass.
- Broad `git diff --check`: failed on previously existing trailing whitespace in transplanted dirty source; patch-only added lines had no trailing whitespace.
- Real RTX 3090 playback / Japanese CER / latency: **NOT TESTED**.

## Remaining risks
- `max_seg` boundary overlap / text dedup needs fixed-sample Japanese CER before introduction; no blind overlap to avoid subtitle duplication and flicker.
- Final attempts are bounded; prolonged unavailable backends still produce warning rather than guaranteed transcription.
- Separate GUI full-suite timeout and translation bundle parity mismatch must be resolved before release.

## Gate
Manual review, real audio regression, full-suite remediation, and integration of local changes are required. No auto-merge or deployment authorized.
