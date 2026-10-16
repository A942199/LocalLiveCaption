<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-boundary-20261016 -->
# Task report: asr-maxseg-boundary-20261016

- Repository: `A942199/LocalLiveCaption`
- Proposed branch: `agent/asr-maxseg-boundary-20261016`
- Code HEAD before memory writes: `e1b5fc51bba447f7c4380b44a4f3fefde2319faf`
- Outcome: `AWAITING_REVIEW`

## Observed task summary

- Baseline: main `667392f8db2bce5d54abfbd76a9dd19a06f5fc11`, clean isolated Windows WebCodex managed worktree. Existing original Windows checkout is dirty and was not overwritten.
- Modified `live-caption-ja.pyw`: prefer natural short pauses near maximum segment time; carry up to 400ms overlapping tail into the next segment if forced at maxlen; conservative Japanese text suffix-prefix removal; optional short bridge recognition for plausible 1-2 character boundary mismatch; preserve ordered Final, once-only sink delivery and no revision of previously submitted Final. Retained Silero VAD and Qwen3-ASR GGUF + llama.cpp backend.
- Added `tests/test_asr_maxseg_boundary.py` with 8 mocked regression tests. Updated `README_PYTHON.md` with configuration flags `--maxseg-soft-window`, `--maxseg-soft-silence`, and `--maxseg-overlap-ms`.
- Windows Python 3.11 targeted boundary tests: 8/8 passed; ASR-focused tests: 56/56 passed.
- After final code change, Windows Python 3.11 full test suite: `python -m unittest discover -s tests -p "test_*.py" -q` => 113/113 PASS in 27.431s. `git diff --cached --check` passed. Git commit `e1b5fc51bba447f7c4380b44a4f3fefde2319faf` was pushed to branch; Draft PR #11 opened.
- The attempted `python live-caption-ja.pyw --help` runtime command timed out, so CLI help smoke remains unverified.
- No real Japanese recording CER/WER, RTX 3090 GPU delay profiling, production translation/overlay integration smoke, independent human review or applicable fresh successful GitHub CI check was obtained. Mock tests do not establish real accuracy gains. Changes are not merged to main.

## Durable knowledge

Use a conservative seam policy: preserve text on ambiguous alignment rather than deleting genuine spoken repeats; let old Final stay immutable, send repaired new Final at most once, preserve queue/backpressure behaviors; defer release until real Japanese audio regression and GPU latency measurements verify behavior.

## Evidence links

- https://github.com/A942199/LocalLiveCaption/issues/10
- https://github.com/A942199/LocalLiveCaption/pull/11

## Release guard

Do not merge on this document alone; independent review, real-world ASR benchmarks and CI/ruleset requirements are not verified. Documentation commits advance PR HEAD and invalidate any assumption that earlier code-commit checks apply to the final head.
