# Project memory: LocalLiveCaption

- Repository: `A942199/LocalLiveCaption`
- Source HEAD at initialization: `8b66779a1066a55909239fc84bd9c0388cf4a409`
- Initialized for run: `asr-final-reliability-20261016`

## Verified project purpose
Windows local Japanese ASR captioning, stable Final and Partial, optional translated overlay

## Durable decisions and lessons
Add only verified project decisions and outcomes; do not include secrets.

## Run `asr-final-reliability-20261016`

- Local source baseline: Windows live-caption dirty `main`, commit `e4d342030544b891d73a5c13c1142df31d24c0e4`, uncommitted user edits preserved.
- Implementation is on an isolated local WebCodex worktree, **not pushed or committed**; this GitHub branch is documentation only.
- Scoped fixes: `finish_reason=length` handling plus one larger token-budget retry, bounded Final retry on transient failures, preservation of short active speech tails, Final-first admission of new Partial work and early stale-Partial filtering.
- Evidence: four new Final reliability regressions failed before patch and passed after; all 48 ASR-specific unittests passed after patch. Translation scheduling and stable-prefix focused suites passed separately. Full GUI suite timed out with Tkinter finalizer exceptions; standalone single-file bundle parity had a pre-existing mismatch.
- No real Japanese speech CER/GPU benchmark. Do not assert improved accuracy; no automatic main merge.

## Run `asr-final-reliability-merge-20261016`

- 2026-10-16: ASR-only code fixes were ported from the isolated local candidate to verified remote `main` baseline `8b66779a1066a55909239fc84bd9c0388cf4a409`, committed as `cd4d381c7ea4fd703d213122daac284e860de10c` and pushed to code PR #9 (`agent/asr-final-reliability-code-20261016`).
- Candidate only changes `live-caption-ja.pyw` and `tests/test_asr_final_reliability.py`; full 105/105 and ASR 48/48 Windows mocked unittest suite passed.
- PR #7's larger Python-only rewrite was not merged. This branch / PR #8 contains documentation only.
- Requested `main` merge remains **blocked**: on PR #9 GitHub Build and Test was **skipped**, no independent PR review exists, and the branch protection endpoint returned 403. Actual remote `main` was not changed.
- No live Japanese CER/GPU ASR benchmark was performed; manual release gate review is outstanding.

## Run `asr-final-reliability-direct-merge-20261016`

- User directly requested merging code PR #9 to `main` again. Real-time GitHub check verified code SHA `cd4d381c7ea4fd703d213122daac284e860de10c`, no merge conflicts, but `Build and Test` and `Create GitHub Release` are **skipped**, no independent PR reviews exist, and branch protection details returned HTTP 403. `main` was `8b66779a1066a55909239fc84bd9c0388cf4a409`.
- Gate outcome: **BLOCKED**, **no merge executed**, even with explicit user request, due to mandatory fresh independent review, successful CI, and verified policy requirements. Original dirty local checkout untouched.

<!-- ai-orchestrator:checkpoint:v1:continuous20m-asr-ab-20261018 -->
## Durable checkpoint: continuous20m-asr-ab-20261018
- Outcome: `COMPLETED` (local test only; no code change).
- Code HEAD before documentation: `667392f8db2bce5d54abfbd76a9dd19a06f5fc11` (GitHub main); tested local dirty checkout `e4d342030544b891d73a5c13c1142df31d24c0e4`, source SHA256 `78E1E182EC6CEAB196EBBE7C00BA77C22AD3965842148B06B62014C5DE4167AF`.
- Verified lesson: 20-minute FLEURS synthetic loopback, 3×, strict CER upstream 17.1124%, local 14.8912%; Final interval overlap misses upstream 1/local 5 require careful reference alignment. Not the historical missing AivisSpeech original MP3.
- Source task: `docs/ai-orchestrator/runs/continuous20m-asr-ab-20261018/report.md`.
