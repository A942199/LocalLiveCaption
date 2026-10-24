<!-- ai-orchestrator:checkpoint:v1:asr-parameter-pipeline-20261024 -->
# Task report: asr-parameter-pipeline-20261024

- Repository: `A942199/Qwen3-LiveCaption`
- Proposed branch: `agent/asr-parameter-pipeline-20261024`
- Code HEAD before memory writes: `36183f2048f2a2ef577adf833ad047e1a9afc60c`
- Outcome: `AWAITING_REVIEW`

## Observed task summary
Local Windows Qwen3-ASR parameter pipeline repaired and validated with real GPU. HTTP max_tokens/temperature, dynamic VAD four knobs, context history/chars, HF max tokens, explicit ctx8192 corrected; all19 numeric bounds. 14 new focused tests and16 pinned upstream regressions pass. Real RTX3090 36s Q8_0+Silero+ASR worker test 36/36 sec with5 Final81 Partial and zero errors, user VAD setting .25/100/100/200 + tokens512 applied. Pre-existing full legacy 107 fail25/error36 unchanged. Upstream pinned source/UI/translation, user settings, disabled hotwords unchanged. Local source SHA dfa993fdbc996fded0634c030bd7b68842080860920e4483eaa0c1621f216fda; no code push.

## Durable knowledge
2026-10-24 user request to repair ALL available Windows Qwen3-ASR UI/CLI/settings parameter propagation on local dirty user checkout. Local both pyw entrypoints identical final SHA256 dfa993fdbc996fded0634c030bd7b68842080860920e4483eaa0c1621f216fda, original before SHA 25dac0471f71ed5d7196b4c90b7b6afe1166224979e8cb9d294baf5f575a01b0. Original TerryKiddy upstream QwenBackend/LlamaBackend/run_asr and original UI/translation/main AST preserved; adapter fixes HTTP max_tokens/temperature, HF max_new_tokens, final history/char context, configurable VAD threshold,min_speech,min_silence,padding via isolated AST clone. ctx explicit8192 no longer mis-sentinel to saved4096; local --cache-ram0 preserved. All19 numeric CLI setting bounds validated incl disabled max_seg=0. User hotwords still disabled by renamed file, original user settings unchanged. Unsupported upstream placeholders stable_count/punct_silence/hesitation_silence warn if nondefault, not secretly implemented; external llama-url warns local flags don't apply. 14/14 fresh focused tests and 16/16 pinned upstream regressions pass; real Q8_0 RTX3090 8s HTTP smoke and 36s actual Silero VAD+Final/Partial virtual PCM replay on final exact SHA pass: audio36/36s, 5 Final/81 Partial, no errors, runtime VAD .25/100/100/200, max_tokens512. 107 historical legacy tests fail25/error36 exactly matching pre-change baseline by test name; full suite NOT green. HF dependencies torch/transformers/qwen_asr absent, mocked only; actual physical WASAPI not re-tested. Local ignored report tests/results/asr_restore_20261023/asr_parameter_bridge_report_20261024.md, result JSON and 107 legacy log; no source, transcript or user audio uploaded; GitHub documentation-only checkpoint, no code commit/push or merge.

## Evidence links
- None recorded; verify logs via connected tools.

## Release guard
Do not merge on this document alone; review and CI are not independently verified by memory.

