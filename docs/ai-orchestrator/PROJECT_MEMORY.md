# Project memory: LocalLiveCaption

- Repository: `A942199/LocalLiveCaption`
- Source HEAD at initialization: `75456b3c1027d68311a70a2967b06719e22f554d`
- Initialized for run: `asr-accuracy-fixes-20261015`

## Verified project purpose
The remote main branch contains the WPF translator replacement; a Windows local checkout at `e4d342030544b891d73a5c13c1142df31d24c0e4` retains a Python Qwen3-ASR GGUF live-caption implementation with user-local uncommitted work. This repair task targets the local Python implementation and must not overwrite the divergent remote main branch.

## Durable decisions and lessons
Preserve existing worktree changes. No code push or merge without explicit user authorization and branch reconciliation.
