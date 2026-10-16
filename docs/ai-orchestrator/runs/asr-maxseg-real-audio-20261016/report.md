<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-real-audio-20261016 -->
# Real Japanese audio validation: PR #11 max_seg

- Repository: `A942199/LocalLiveCaption`
- Target PR: https://github.com/A942199/LocalLiveCaption/pull/11
- Tested code SHA (before prior memory commits): `e1b5fc51bba447f7c4380b44a4f3fefde2319faf`
- Baseline main SHA: `667392f8db2bce5d54abfbd76a9dd19a06f5fc11`
- Validation task outcome: `COMPLETED`
- **Release decision: FAIL — do not merge the PR #11 default overlap implementation.**

## Environment and source data

- Windows Windows-native WebCodex runner, NVIDIA RTX 3090 24 GB, llama.cpp CUDA; actual Qwen3-ASR-1.7B-Q8_0.gguf and mmproj model, actual ONNX Silero VAD on CPU. GPU memory during evaluation ~5153 MiB, returning ~1418 MiB when server closed.
- FLEURS Japanese `ja_jp` dev set, Google FLEURS: https://huggingface.co/datasets/google/fleurs (CC BY 4.0; credit dataset). Cached in the user's local repository under `tests/results/corpora/fleurs_ja_jp_dev`.
- A/B uses 48 unique real human Japanese clips of duration >=16s, 937.5s total (15m37.5s), 3548 normalized reference characters. Each version processed the same recordings under max_seg=16s and 6s.
- Runtime settings: lang=ja, Q8_0, no ASR previous-text context, max_tokens=256, temperature=0, 2 slots, ctx_size=4096, Silero VAD threshold=.1, 200ms pad, 200ms VAD window.
- CER: NFKC+lowercase, strip Unicode punctuation, whitespace and control code points on both text and reference; Levenshtein characters / reference characters. Not equivalent to verbatim punctuation-sensitive CER.

## Actual real-audio A/B: 384 completed cases

| Configuration | max_seg=16s CER | max_seg=6s CER |
|---|---:|---:|
| `main` (baseline) | 6.623% (235/3548) | 10.316% (366/3548) |
| `PR11` default: soft split + 400ms overlap | 7.018% (249/3548) | 13.613% (483/3548) |
| `PR11` `maxseg_overlap_ms=0`: soft only | 6.652% (236/3548) | 10.231% (363/3548) |
| `PR11` `maxseg_soft_window=0`: overlap only | 6.990% (248/3548) | 14.205% (504/3548) |

Each cell above contains 48 matching audio files. No per-case Python exceptions; evaluation script completed with exit code 0. **A passing execution is NOT passing accuracy acceptance.**

Default overlap versus baseline: +14 character edits at 16s; +117 at 6s. Isolating overlap shows the error largely disappears when disabled. Overlap changes model interpretation of the following utterance and can duplicate already recognized material even when suffix/prefix text de-duplication cannot align it. Soft-only was near neutral, not an established cross-domain accuracy improvement.

Performance (batch-fed WAV, not real-time total caption latency):
- 16s baseline vs default: 69 vs 69 ASR calls, P95 inference-call duration ~360ms vs 375ms.
- 6s baseline vs default: 143 vs 152 ASR calls, P95 inference-call duration ~172ms vs 188ms.

Detailed exemplar: FLEURS clip `1062963029061341657`, 16s: baseline CER 3.738%, PR11 default 11.215%, PR11 no-overlap 3.738%. PR11 produced an altered/repeated phrase at the 16s boundary. At 6s, `14997571596385525160` had 21 errors on baseline and 36 on default PR, while no-overlap returned to 21. Other candidate regressions: `13205813217473836129`, `10282008873026289596`.

## Actual real-time paced ingestion (not instantaneous WAV batch)

- Additional four full-length input trials using genuine 29.52s FLEURS human Japanese WAV `1062963029061341657` at 16kHz, delivered as 200ms audio chunks at actual audio pace; *Partial enabled* with actual Qwen3-ASR inference and VAD.
- max_seg 16s: main CER 3.738%, 2 Final, 42 Partial events, 45 ASR calls, 30.094s wall; PR11 default CER 5.607%, 2 Final, 42 Partial events, 46 calls, 29.953s wall.
- max_seg 6s: main CER 22.430%, 4 Final, 40 Partial events, 44 calls, 29.937s wall; PR11 default CER 28.972%, 5 Final, 40 Partial events, 47 calls, 29.938s wall.
- All four paced trials ended normally with no reported exception; cannot infer absence of GUI visual flicker from Partial event counts.

## Test scope / caveats

- **Real:** human Japanese speech samples with fixed reference, actual GGUF Qwen3-ASR via Windows CUDA backend, actual ONNX CPU Silero VAD, actual application Final/Partial worker functions, max-seg boundary paths, test evidence persisted locally.
- **Injected:** WASAPI capture callback/audio queue and output sink, to guarantee reproducible identical PCM samples and measure ASR not audio-device routing. No physical speaker playback / WASAPI microphone loopback check; no actual Tkinter GUI or online translation API was invoked.
- **Not claimed:** real-world animation/drama CER, overlapping-speaker separation, production speech-end-to-translation delay, visible translated subtitle no-flicker, unbiased wide-domain threshold tuning. 48 selected *long* clips, not the full 266-clip FLEURS dev set.
- Prior mocked 113/113 tests on PR11 remain valid for their limited contract, but did not detect this accuracy regression.

## Locally persisted artifacts (ignored by Git)

`C:\Users\uu\.webcodex-worktrees\live-caption-8fb5ee5d\tests\results\maxseg_real_20261016_eval\`

- `run_real.py`, `results.json`: 4-clip preliminary A/B.
- `run_real_long.py`, `results_long.json`: 384-case long-audio A/B, transcripts, Final reason and inference timing.
- `run_real_realtime.py`, `results_realtime.json`: 4-case 29.52s paced test, Partial/Final count, GPU timings.
- All tests read model/audio from existing original project folders; they did not alter `main`, model binaries, or the user's dirty source checkout. Temporary baseline launcher was removed; RTX 3090 memory returned to pretest usage.

## Recommendation / gate

**Keep PR #11 draft and unmerged.** Do not enable its default 400ms audio overlap on the evidence above. A next iteration should disable overlap by default; retain/improve soft cut only when separately validated, and redesign boundary handling using deferred uncommitted text with stronger cross-seam alignment before reconsidering audio overlap. Re-test on fixed real Japanese audio, particularly forced cuts, number/name-heavy speech and genuine repeated words. Do not call this change an accuracy optimization based only on mocked tests.

This report reflects test evidence on code SHA `e1b5fc51bba447f7c4380b44a4f3fefde2319faf`. Documentation commits can advance the PR HEAD; rerun fresh CI/review gates for any later merge.
