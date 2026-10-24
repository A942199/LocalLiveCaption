<!-- ai-orchestrator:checkpoint:v1:asr-ui-param-fix-20261023 -->
# Task report: asr-ui-param-fix-20261023

- Repository: `A942199/Qwen3-LiveCaption`
- Proposed branch: `agent/asr-ui-param-fix-20261023`
- Code HEAD before memory writes: `ed553085d4ff961bc5aa7abd3819ccb5317565a0`
- Outcome: `AWAITING_REVIEW`

## Observed task summary
Fixed local Windows live-caption-ja.pyw and live-caption-ja-restore.pyw (identical SHA256 f6d48f2db5e7e3203861bcc692466e7515b127ba791435560dd799b9e73e452c): GUI/CLI ctx_size now forwarded to LlamaBackend startup -c; actual RTX3090 llama-server.exe verified -c 4096 -np 2, no accidental reuse of same-model server with different flags; injected launcher default --lang no longer overrides UI language. Hidden false UI knobs that restored TerryKiddy upstream never uses: max tokens/temperature fixed 200/0; history Final count & char limit fixed 1/80; VAD threshold/min speech/min silence/padding fixed 0.45/100/200/100; stable/punctuation/hesitation unsupported. High precision preset sets upstream defaults: ctx 8192, 2 slots, context enabled, VAD threshold 0.45, silence 0.5s, minseg1 maxseg6, 200 tokens, Japanese app language JA preserved. Tests 4/4 new regression and 8/8 runtime/Tk/backend contract green, full 31:01.851s Japanese podcast accelerated virtual audio GPU ASR+real Google2+Tk completed, 367 Final,1002 partial,1093 translation replies,1344 Chinese UI renders; full input consumed, clean server/thread shutdown. Two real 36s Windows SAPI speaker->SoundCore2 WASAPI->RTX3090->real Google2->Tk E2Es returned 0: first missed initial sentence; second covered all 3 utterances (4 Final,12 replies,34 UI frames). Legacy full suite NOT GREEN: 107 tests, 27 failed,36 errored, 19 obsolete high-preset expected values, 2 strict AST parity assertions for deliberately changed constructor call; other old ASR and translation bundling failures. Known duplicate Chinese semantic output unresolved. No source commit or push, no independent reviewer/CI pass or merge. Local test report tests/results/asr_restore_20261023/asr_ctx_ui_fix_report_20261023.md.

## Durable knowledge
Source local only sha256 f6d48f2db5e7e3203861bcc692466e7515b127ba791435560dd799b9e73e452c. Source backup under ignored tests/results/asr_restore_20261023. Fix ctx_size => actual server -c, dedicated server avoids stale model-config reuse; main and restore .pyw synchronized; hide unsupported upstream-fixed UI options, high accuracy preset uses original defaults, Japanese remains language. Actual real GPU 31m accelerated podcast and twice real Windows speaker WASAPI Google2 Tk E2Es passed pipeline, old full suite 27fail+36error/107 not green, Chinese duplicate output remains.

## Evidence links
- https://github.com/A942199/Qwen3-LiveCaption/issues/18

## Release guard
Do not merge on this document alone; review and CI are not independently verified by memory.

