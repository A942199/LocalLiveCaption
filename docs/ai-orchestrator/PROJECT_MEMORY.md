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
