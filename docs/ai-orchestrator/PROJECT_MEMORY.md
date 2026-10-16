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

<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-boundary-20261016 -->
## Durable checkpoint: asr-maxseg-boundary-20261016
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `e1b5fc51bba447f7c4380b44a4f3fefde2319faf`
- Verified knowledge to retain: Near max_seg choose soft VAD pauses; for unavoidable maxlen carry at most 400ms audio into next utterance; conservatively de-duplicate direct Final overlaps, optionally run a short bridge ASR for verified 1–2 character seams, and never rewrite prior Final. Preserved original dirty Windows checkout.
- Source task: `docs/ai-orchestrator/runs/asr-maxseg-boundary-20261016/report.md`

<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-real-audio-20261016 -->
## Durable checkpoint: asr-maxseg-real-audio-20261016
- Outcome: `COMPLETED` (real-audio testing only); PR #11 RELEASE DECISION: `FAIL / DO NOT MERGE`.
- Tested code HEAD: `e1b5fc51bba447f7c4380b44a4f3fefde2319faf` vs baseline `667392f8db2bce5d54abfbd76a9dd19a06f5fc11`.
- Verified knowledge to retain: Real FLEURS ja_jp human speech on local RTX 3090/Qwen GGUF and CPU Silero, 48 long clips/937.5s/384 audio trials, found PR11 default 400ms overlap worsens CER (16s: 6.623% main vs 7.018% PR; 6s: 10.316% vs 13.613%); disabling overlap restores near-baseline CER (6.652%, 10.231%). Four additional paced Partial/Final trials confirm same direction. Prior 113/113 mocked pass was insufficient. Maintain draft PR and avoid default overlap until redesign; no source-code mutations in validation.
- Source task: `docs/ai-orchestrator/runs/asr-maxseg-real-audio-20261016/report.md`

<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-upstream-restore-20261016 -->
## Durable checkpoint: asr-maxseg-upstream-restore-20261016
- Outcome: `AWAITING_REVIEW`, draft PR #11 remains unmerged.
- Tested code HEAD before memory commits: `3e331eddb9181b8710877ccf1a207aeda1d57859` against main `667392f8db2bce5d54abfbd76a9dd19a06f5fc11`.
- Verified facts: restored original author's natural-silence complete-Partial reuse and maxlen always fresh full-ASR logic; removed previous PR11 soft/overlap/bridge code, retained present-day LocalLiveCaption queue/retry/dynamic endpointing. Full 110/110 Windows Python tests passed; 192 genuine FLEURS long-audio Qwen+Silero A/B tests without Partial yielded exactly matching texts and CER (16s 6.623%, 6s 10.316%). Four real-time-paced Partial tests showed reuse on normal silence saves one inference but can worsen CER on one tested clip (3.738% to 5.607% at 16s). No independent review, GitHub CI skipped; no automatic merge.
- Source task: `docs/ai-orchestrator/runs/asr-maxseg-upstream-restore-20261016/report.md`

<!-- ai-orchestrator:checkpoint:v1:upstream-code-parity-audit-20261016 -->
## Audit checkpoint: upstream-code-parity-audit-20261016
- Outcome: `COMPLETED` (source audit, **no production edits**). Compared original TerryKiddy/live-caption `794fea1fd50f34e55a6e1e1537aa1659af61fea1`, fork main `667392f8db2bce5d54abfbd76a9dd19a06f5fc11`, and PR #11 code `3e331eddb9181b8710877ccf1a207aeda1d57859`. PR #11 still draft/unmerged, GitHub CI skipped.
- Verified high-priority findings: app startup appends `--lang ja` and `--out transcript.txt` behind `--lang=auto` / `--out=custom.txt` (runtime repro) and mistakes its injected `--lang` for user explicit configuration, defeating saved GUI auto selection; translation final() unconditionally inserts period even for ASR `reason=maxlen` because that reason never reaches translation, breaking semantic sentence boundaries; original-author silence Partial reuse regressed one real FLEURS Japanese clip vs always-fresh Final; current CI only builds/tests .NET, no Python gate.
- Secondary risks: bounded audio queue drops old chunks on overload, no in-flight HTTP ASR cancellation on shutdown, translation UI accepts potentially stale queued results, VAD relies on private ONNX session interface, unbounded Tkinter event queue. These are mostly source-backed risk hypotheses, not all live-reproduced defects.
- Durable report: `docs/ai-orchestrator/runs/upstream-code-parity-audit-20261016/report.md`; tracking issue #12. No auto-merge or source rewrite.
