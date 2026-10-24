<!-- ai-orchestrator:checkpoint:v1:hotwords-ab-test-20261024 -->
# Task report: hotwords-ab-test-20261024

- Repository: `A942199/Qwen3-LiveCaption`
- Proposed branch: `agent/hotwords-ab-test-20261024`
- Code HEAD before memory writes: `3ce44dce50ed82c96deef759afe1363bcb9c6aa8`
- Outcome: `COMPLETED`

## Observed task summary
Local Windows RTX3090 ASR hotword A/B completed: four independent Q8_0 GGUF 31m offline replay passes of 233 paired 8s clips each, 0 model errors, plus four runs of eight labeled Japanese TTS utterances. Prompt-echo bug confirmed for lists of 3/12/50 (4/2/2 affected clips), not observed with 0. Source/settings/hotwords unchanged; local private report saved.

## Durable knowledge
2026-10-24 real local-GPU test controlled lang ja, ctx8192 total, slots2, previous context off, --cache-ram0. 31:01.851s user-provided Japanese podcast as fixed eight-second clips: no hotwords 56.61s inference, RTF0.03041, echo0; 3 words 55.73s, echo4; 12 words56.92s, echo2; 50 words64.39s, echo2. All 233/233 clips each, no model errors. Llama service private RAM ~5.09-5.15 GiB with disabled cache RAM. Full-podcast ground-truth transcript unavailable; no full-audio CER claim. 8 Microsoft Haruka Desktop synthetic Japanese sentences 33.4s total per mode with reference texts: strict CER 0=24.4%, 3=23.44%, 12=14.35%, 50=77.99%. 12-word prompt improves synthetic CER but echoes entire list twice in long real podcast at 688s and1104s. 3 words echo4 clips; 50 words echo two and one TTS severe contamination. Recommendation no hotwords on general-purpose mixed videos pending safe prompt strategy and retest; current hotwords.txt 50 unchanged. Local evidence tests/results/asr_restore_20261023/hotwords_ab_20261024/report.md. Local app SHA256 25dac0471f71ed5d7196b4c90b7b6afe1166224979e8cb9d294baf5f575a01b0. No audio/transcripts/upload of personal source; no code pushes. This GitHub branch only receives documentation.

## Evidence links
- None recorded; verify logs via connected tools.

## Release guard
Do not merge on this document alone; review and CI are not independently verified by memory.

