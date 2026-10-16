# Current project state: LocalLiveCaption

- Repository: `A942199/LocalLiveCaption`
- Initialized for run: `asr-accuracy-fixes-20261015`
- Original source HEAD: `75456b3c1027d68311a70a2967b06719e22f554d`
- Task status: `INITIALIZED` (not completed or merged)

## Task checkpoints
The active task fixes verified ASR config, language, VAD, padding and transcript-handling defects on the separate local Python checkout. Remote main is an unrelated WPF replacement. A checkpoint is not proof of tests, independent review, CI or merge.

<!-- ai-orchestrator:checkpoint:v1:asr-accuracy-fixes-20261015 -->
## Task status update: asr-accuracy-fixes-20261015
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `053073ac9d2498a89159118f8b3f321558dd158d`
- Summary: Local Python ASR fixes validated by 57/57 unit tests and real GGUF Q8_0 synthetic-audio HTTP 200 smoke test. Code is uncommitted and not pushed. Actual Japanese CER and forced max-segment overlap remain unverified; remote main is WPF.
- Evidence references: 2
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:asr-btsj-cer-20261015 -->
## Task status update: asr-btsj-cer-20261015
- Outcome: `BLOCKED`
- Code HEAD before memory writes: `8fa21c1058476b7d7949f987bebe6196ebb1dae7`
- Summary: Unable to run real pre/post Japanese CER: Temp BTSJ workspace had 138 directories and zero source files; the separately archived 1200s Japanese TTS original source MP3 is also missing. No current CER, deletion count, or subtitle-omission measure. No overlap modifications authorized without fixed corpus and human references.
- Evidence references: 2
This is a data blocker checkpoint, not a merged default-branch state.

