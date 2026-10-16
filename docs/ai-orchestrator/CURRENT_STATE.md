# Current project state: LocalLiveCaption

- Repository: `A942199/LocalLiveCaption`
- Initialized for run: `asr-final-reliability-20261016`
- Original source HEAD: `8b66779a1066a55909239fc84bd9c0388cf4a409`
- Task status: `INITIALIZED` (not a completed or merged task)

## Task checkpoints
A task checkpoint is not proof of tests, independent review, CI or merge.

## ASR reliability run `asr-final-reliability-20261016`

- Status: `AWAITING_REVIEW` — local candidate in isolated worktree only; no code commit, push or merge.
- Scoped tests: 48/48 ASR unittest tests passed; 4/4 new red-to-green tests passed. Stable-prefix 4/4 and isolated translation-scheduler 17/17 passed.
- Blockers: full-suite timeout (Tkinter cleanup); single-file translation-bundle parity mismatch; missing real Japanese audio CER and GPU timing.
- Next actions: review and merge the isolated changes only after safe integration of original dirty Python source, and benchmark sentence boundaries, Final loss, latency and CER.

## Merge request `asr-final-reliability-merge-20261016`

- Outcome: **BLOCKED**, not merged to `main`.
- Code branch: `agent/asr-final-reliability-code-20261016`; PR #9: https://github.com/A942199/LocalLiveCaption/pull/9
- Tested code SHA: `cd4d381c7ea4fd703d213122daac284e860de10c` (2 changed files; Python unittest 105/105, ASR 48/48 on Windows).
- Release gate blockers: GitHub CI Build and Test skipped, no independent PR approvals, GitHub branch protection read unavailable (403). Actual `main` remains `8b66779a1066a55909239fc84bd9c0388cf4a409` when checked.
- This PR #8 is documentation-only and must not be mistaken for the code patch. Original user dirty Windows working tree was not edited.

## Direct merge request `asr-final-reliability-direct-merge-20261016`

- Outcome: `BLOCKED`. PR #9 head `cd4d381c7ea4fd703d213122daac284e860de10c` remains OPEN, unmerged; remote `main` unchanged at `8b66779a1066a55909239fc84bd9c0388cf4a409` when verified.
- Prerequisites not satisfied: GitHub Build and Test skipped, no independent approval, branch protection details inaccessible (403). This was a recheck after user authorized direct merge.
- No automatic override, forced push, or conditional merge action performed. Two-file ASR patch remains on code PR #9.

<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-boundary-20261016 -->
## Task status update: asr-maxseg-boundary-20261016
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `e1b5fc51bba447f7c4380b44a4f3fefde2319faf`
- Summary: Implemented soft VAD pause splitting near max_seg, bounded 400ms previous-audio carry on forced cut, conservative Japanese text suffix/prefix de-duplication, optional 1–2 character seam bridge ASR, and single Final commit without rewriting older Final. Changed live-caption-ja.pyw, README_PYTHON.md and added 8 regression tests. Full Windows Python 3.11 mocked suite 113/113 PASSED after last code change; git diff --check passed. Code is on draft PR #11; main is unchanged. Japanese real-audio CER, RTX 3090 latency and overlay smoke not verified; CLI --help attempt timed out and was not proven; independent review/CI not established.
- Evidence references: 2 (Issue #10 and PR #11).
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-real-audio-20261016 -->
## Task status update: asr-maxseg-real-audio-20261016
- Task outcome: `COMPLETED` (real speech validation); **candidate PR #11 release decision: FAIL**, keep draft/unmerged.
- Code HEAD before memory writes: `e1b5fc51bba447f7c4380b44a4f3fefde2319faf`.
- Summary: 48 FLEURS Japanese long clips (937.5s, 3548 reference characters), 384 real-audio test cases on actual RTX 3090 Qwen GGUF + Silero VAD: main CER 6.623%/10.316% at max_seg 16s/6s; PR11 default overlap 7.018%/13.613%; disabling overlap 6.652%/10.231%. Additional four 29.52s real-time paced Partial/Final runs completed with similar negative accuracy trend. Actual Tkinter and physical WASAPI were not tested. GPU inference was real, WASAPI input was injected. PR11 remains unsuitable for default enablement.
- Evidence: https://github.com/A942199/LocalLiveCaption/pull/11 and `docs/ai-orchestrator/runs/asr-maxseg-real-audio-20261016/report.md`.
This checkpoint is not a merged/default-branch release approval.

<!-- ai-orchestrator:checkpoint:v1:asr-maxseg-upstream-restore-20261016 -->
## Task status: asr-maxseg-upstream-restore-20261016
- Outcome: `AWAITING_REVIEW`, PR #11 code updated; `main` remains unchanged. Code SHA `3e331eddb9181b8710877ccf1a207aeda1d57859` before docs-only commits.
- Exact requested original-author behavior implemented: silence ends current phrase and reuses completed full-speech Partial when available; otherwise re-ASR; max_seg always fresh ASR, next segment audio independent/zero-overlap; deleted old experimental Bridge/overlap. Preserves stable partial / Final worker reliability and modern adaptive endpoint.
- Tests: 110/110 full mock suite; 192 actual Japanese FLEURS human-audio GPU+Silero trials (48 clips at 16s/6s, main and candidate) with identical no-Partial CER of 6.623%/10.316%. Four paced live Partial+Final trials completed; silence Partial reuse confirmed but one 16s clip's CER was 3.738% baseline vs 5.607% candidate; this is a material release caveat.
- Blockers: code reviewer independent approval absent, CI skipped, real GUI + physical WASAPI + live translation flicker untested. See `docs/ai-orchestrator/runs/asr-maxseg-upstream-restore-20261016/report.md` and PR #11 comment.
This task checkpoint is not a release or merge approval.
