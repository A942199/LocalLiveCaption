# Project memory: LocalLiveCaption

- Repository: `A942199/LocalLiveCaption`
- Source HEAD at initialization: `75456b3c1027d68311a70a2967b06719e22f554d`
- Initialized for run: `asr-accuracy-fixes-20261015`

## Verified project purpose
The remote main branch contains the WPF translator replacement; a Windows local checkout at `e4d342030544b891d73a5c13c1142df31d24c0e4` retains a Python Qwen3-ASR GGUF live-caption implementation with user-local uncommitted work. This repair task targets the local Python implementation and must not overwrite the divergent remote main branch.

## Durable decisions and lessons
Preserve existing worktree changes. No code push or merge without explicit user authorization and branch reconciliation.

<!-- ai-orchestrator:checkpoint:v1:asr-accuracy-fixes-20261015 -->
## Durable checkpoint: asr-accuracy-fixes-20261015
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `053073ac9d2498a89159118f8b3f321558dd158d`
- Verified knowledge to retain: Never overwrite the local uncommitted Python branch with divergent remote WPF main. Share saved ASR settings between GUI and console. Qwen3-ASR forced language via trailing assistant prefix. Bounded VAD hysteresis and exact preroll samples. Preserve Japanese repetitions. Check reused server slots, context and audio capability. Real Japanese CER still not measured.
- Source task: `docs/ai-orchestrator/runs/asr-accuracy-fixes-20261015/report.md`

