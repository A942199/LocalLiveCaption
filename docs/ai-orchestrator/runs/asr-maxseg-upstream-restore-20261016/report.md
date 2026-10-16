<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-upstream-restore-20261016 -->
# Original author max_seg processing restored

- Scope: `A942199/LocalLiveCaption` PR #11, existing isolated branch `agent/asr-maxseg-boundary-20261016`
- Tested code HEAD, before docs-only commits: `3e331eddb9181b8710877ccf1a207aeda1d57859`
- Baseline `main`: `667392f8db2bce5d54abfbd76a9dd19a06f5fc11`
- Outcome: `AWAITING_REVIEW`. Source-level behavior implemented and tested. **Not merged.**

## Upstream behavior implemented

Compared to `TerryKiddy/live-caption/live_caption.py`: on natural silence, a cached completed Partial is reused **only** if its audio coverage reaches the last effective speech sample. If no complete Partial is available, use fresh full-utterance Final ASR. On `maxlen`, always perform fresh full ASR, clear segment audio and restart without overlap. No separate Bridge recognition or cross-segment text deletion.

Changes replace prior PR #11 speculative "soft cut / 400ms overlap / bridge alignment" with original-author segmentation and Partial reuse. Retain modern LocalLiveCaption-specific stateful Silero VAD, adaptive punctuation/hesitation silence thresholds, Final retries on transient failures, bounded Final queues, stale-Partial filtering, buffer-draining shutdown and subtitle stabilization; this is **not** a literal copy of the older entire application.

- Updated `live-caption-ja.pyw`
- Updated `README_PYTHON.md` to reflect conditional Partial reuse, and no unconditional Final re-transcribe claim
- Deleted `tests/test_asr_maxseg_boundary.py` (tests obsolete experimental overlap behavior)
- Added `tests/test_asr_original_segmentation.py` (five regression tests including synthetic paced Partial reuse and hardcut isolated audio)

## Verification

**Mock/integration suite** — Windows `C:\Users\uu\Documents\webcodex\live-caption\.venv\Scripts\python.exe`; `python -m unittest discover -s tests -p test_*.py -q`: 110 tests PASS in 28.626s. `git diff --cached --check`: PASS before commit.

**Actual Japanese speech, actual ASR and VAD, no Partial** — cached FLEURS ja_jp dev, 48 different real human clips, 937.5s total (3548 normalized reference chars), same Windows RTX 3090 and Qwen3-ASR-1.7B-Q8_0.gguf / llama.cpp CUDA, actual Silero VAD. 192 runs: main and candidate exact same recognized transcript for every clip, no errors. At max_seg 16s both 6.6234% CER (235/3548), 69 Final inference calls each. At max_seg 6s both 10.3157% CER (366/3548), 143 Final inference calls each. This establishes output parity when Partial is disabled.

**Actual real-time paced Partial/Final** — one 29.52s FLEURS human Japanese clip delivered as 200ms audio chunks at playback speed; four trials at 16s/6s for main/candidate. All ended without exceptions; at 16s, main CER 3.738%, candidate 5.607%: candidate reused the natural-silence Partial and saved one inference, while maxlen itself was fresh full Final. At 6s, both CER 22.430%, all Finals maxlen, no reuse. This is an observed accuracy tradeoff of the author's reuse logic, not an improvement. No real Tkinter GUI, physical WASAPI or translation overlay verification.

**Release gates** — GitHub Actions `Build and Test` was `skipped` on tested code SHA, no independent review. Draft PR #11 remains unmerged; `main` unchanged. Underlying dirty original Windows project checkout untouched.

## Evidence

- https://github.com/A942199/LocalLiveCaption/pull/11
- https://github.com/A942199/LocalLiveCaption/pull/11#issuecomment-6215181627
- Local ignored workspace: `C:\Users\uu\.webcodex-worktrees\live-caption-8fb5ee5d\tests\results\maxseg_real_20261016_eval\`
- Raw tests: `results_author.json`, `results_author_paced.json`; FLEURS license CC BY 4.0.

## Gate

`AWAITING_REVIEW`. Do not auto-merge. The user chose matching original author semantics, but occasional natural-silence accuracy regression is proven. Separate real-world UI and subtitle translation tests and any additional accept/reject judgment from "code behaves as original" claim.
