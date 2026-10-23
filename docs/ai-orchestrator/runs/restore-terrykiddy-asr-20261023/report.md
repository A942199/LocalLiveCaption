<!-- ai-orchestrator:checkpoint:v1:restore-terrykiddy-asr-20261023 -->
# Task report: restore-terrykiddy-asr-20261023

- Repository: `A942199/Qwen3-LiveCaption`
- Proposed branch: `agent/restore-terrykiddy-asr-20261023`
- Code HEAD before memory writes: `e4d342030544b891d73a5c13c1142df31d24c0e4`
- Outcome: `AWAITING_REVIEW`

## Observed task summary
Windows local dirty checkout only: replaced ASR capture/backend/VAD/scheduler in live-caption-ja.pyw with TerryKiddy upstream 794fea1; original UI+translation AST hashes unchanged. Upstream parity 3 tests, fake audio integration 1, overlay 9, translation port 11, translation cancel 9, translation resilience 3, model preset 2, syntax and isolated .pyw import all passed. Full legacy suite: 101 tests, 5 failed, 37 errored, mostly preexisting expectations of removed custom ASR; translation engine embedding mismatch independently confirmed in pre-edit backup. Real GPU/audio not tested. Local restored source remains uncommitted, no code push/merge. Independent reviewer and GitHub CI absent.

## Durable knowledge
TerryKiddy upstream fixed commit 794fea1fd50f34e55a6e1e1537aa1659af61fea1. Backup local .pyw under ignored tests/results/asr_restore_20261023/live-caption-ja.before-restore.pyw SHA256 78e1e182ec6ceab196ebbe7c00ba77c22ad3965842148b06b62014c5de4167af. Exactly restored upstream AST for 11 ASR entry points; preserved existing UI and translation sink AST hashes. UI ASR settings introduced in local custom build may no longer affect original ASR. Local HEAD e4d342... is base commit only; dirty workspace content has no commit SHA. Release blocked on full suite and real Windows audio/GPU check. GitHub memory/docs branch contains documentation only.

## Evidence links
- https://github.com/A942199/Qwen3-LiveCaption/issues/16

## Release guard
Do not merge on this document alone; review and CI are not independently verified by memory.

