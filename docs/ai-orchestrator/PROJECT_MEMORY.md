# Project memory: Qwen3-LiveCaption

- Repository: `A942199/Qwen3-LiveCaption`
- Source HEAD at initialization: `54727d89e29d2e7fb333a638519417a0c16f0d39`
- Initialized for run: `restore-terrykiddy-asr-20261023`

## Verified project purpose
Windows local Japanese speech recognition with Qwen3 ASR, unchanged Tkinter UI and Chinese translation; restore upstream TerryKiddy ASR only.

## Durable decisions and lessons
Add only verified project decisions and outcomes; do not include secrets.

<!-- ai-orchestrator:checkpoint:v1:restore-terrykiddy-asr-20261023 -->
## Durable checkpoint: restore-terrykiddy-asr-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `e4d342030544b891d73a5c13c1142df31d24c0e4`
- Verified knowledge to retain: TerryKiddy upstream fixed commit 794fea1fd50f34e55a6e1e1537aa1659af61fea1. Backup local .pyw under ignored tests/results/asr_restore_20261023/live-caption-ja.before-restore.pyw SHA256 78e1e182ec6ceab196ebbe7c00ba77c22ad3965842148b06b62014c5de4167af. Exactly restored upstream AST for 11 ASR entry points; preserved existing UI and translation sink AST hashes. UI ASR settings introduced in local custom build may no longer affect original ASR. Local HEAD e4d342... is base commit only; dirty workspace content has no commit SHA. Release blocked on full suite and real Windows audio/GPU check. GitHub memory/docs branch contains documentation only.
- Source task: `docs/ai-orchestrator/runs/restore-terrykiddy-asr-20261023/report.md`

<!-- ai-orchestrator:checkpoint:v1:asr-translation-e2e-20261023 -->
## Durable checkpoint: asr-translation-e2e-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `6bd233474e5d78111fd3feab591011070d6bcaf4`
- Verified knowledge to retain: True E2E proven twice. Source local artifacts under ignored tests/results/asr_restore_20261023/e2e_real_20261023_163032 and e2e_real_20261023_163348. Partial translation quality issue: UI last Chinese '非常感谢。下次再见。再找机会出来碰面吧。' duplicates concept for Japanese 'ありがとうございました。またお会いしましょう。'. This is a pending quality bug, not a total translation verification pass.
- Source task: `docs/ai-orchestrator/runs/asr-translation-e2e-20261023/report.md`

<!-- ai-orchestrator:checkpoint:v1:long31m-e2e-20261023 -->
## Durable checkpoint: long31m-e2e-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `5869f15c80f0dd2fb8c79d415e8d7a883bf3543c`
- Verified knowledge to retain: Full 31m Japanese test audio in %TEMP%/yisheng-benchmark-20261020/yurugengo_479_31m.mp3 sha256 1494b4f6af1761d59f969c2554f0beb8f8522f7af104971ca6f4550df00ad737. Isolated ignored Windows test artifacts tests/results/asr_restore_20261023/long_31m_20261023_165616/{summary.json,events.jsonl} and long_31m_e2e_report_20261023.md. ASR+Google2+Tk dataflow pass over all 1861.851 seconds input with 10x accelerated virtual capture; hardware WASAPI 31-minute sustained stream not asserted. Windows GBK stdout encoding risk may crash final worker on katakana middle dot; rerun via UTF8 stdout avoids it without modifying code. One Google2 API response format error, 160 expected stale partial task cancellations, translation semantic duplicate bug remains. Quality/CI not green. Local source uncommitted unchanged.
- Source task: `docs/ai-orchestrator/runs/long31m-e2e-20261023/report.md`

<!-- ai-orchestrator:checkpoint:v1:asr-ui-param-fix-20261023 -->
## Durable checkpoint: asr-ui-param-fix-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `ed553085d4ff961bc5aa7abd3819ccb5317565a0`
- Verified knowledge to retain: Source local only sha256 f6d48f2db5e7e3203861bcc692466e7515b127ba791435560dd799b9e73e452c. Source backup under ignored tests/results/asr_restore_20261023. Fix ctx_size => actual server -c, dedicated server avoids stale model-config reuse; main and restore .pyw synchronized; hide unsupported upstream-fixed UI options, high accuracy preset uses original defaults, Japanese remains language. Actual real GPU 31m accelerated podcast and twice real Windows speaker WASAPI Google2 Tk E2Es passed pipeline, old full suite 27fail+36error/107 not green, Chinese duplicate output remains.
- Source task: `docs/ai-orchestrator/runs/asr-ui-param-fix-20261023/report.md`

<!-- ai-orchestrator:checkpoint:v1:asr-12core-upstream-parity-20261023 -->
## Durable checkpoint: asr-12core-upstream-parity-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `3a9384279831ade84bf99adee3709dbe062d96bb`
- Verified knowledge to retain: TerryKiddy/live-caption original pinned commit 794fea1fd50f34e55a6e1e1537aa1659af61fea1 genuine GitHub file live_caption.py sha1 git blob 2247eed1a91218b358e767db1a87b019e2004bdf SHA256 37e925c5c9aef56ad4d171c1f2af3d1a100166fac0c2f26b653b78cb0958812b. Previous local fixture only prepended 2 comment lines, AST same. Current Windows local main+restore .pyw SHA256 afe3764de7d998d59b0c6d42d5d4b4b5e2be1a1798b120febb53e6cb0fb56182. 12/12 original ASR core AST nodes identical, 5 UI/SRT/translation/startup divergence intentionally preserved. _LCContextBackend adapter implements configured ctx4096 externally while original LlamaBackend and run_asr source definitions stay AST-identical. Tested RTX3090 real Google2 31m accelerated full input+real 36sec WASAPI, 16/16 targeted guard green; existing suite 107 with 25fails37errors remains not green, Google2 occasional error and duplicate Chinese semantics remain. No source pushed; local ignored tests/results/asr_restore_20261023/exact_terrykiddy_asr_parity_report_20261023.md.
- Source task: `docs/ai-orchestrator/runs/asr-12core-upstream-parity-20261023/report.md`

<!-- ai-orchestrator:checkpoint:v1:publish-asr-source-20261023 -->
## Durable checkpoint: publish-asr-source-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `cf6de6b4bf272a6867a8d3620f3c82b3a9fe32ea`
- Verified knowledge to retain: GitHub push now COMPLETE for source: PR #19 branch agent/asr-ui-param-fix-20261023 added actual live-caption-ja.pyw commit cf6de6b4bf272a6867a8d3620f3c82b3a9fe32ea, blob 5fddbf844442956568dcd4b33181cdaaeec26941, corresponding local CRLF sha256 afe3764de7d998d59b0c6d42d5d4b4b5e2be1a1798b120febb53e6cb0fb56182. PR remains draft, not merged, main unchanged. Published only one program file plus preexisting docs branch, excluded all user settings/logs/tests/untracked. 16/16 targeted parity/ctx UI tests green; full legacy suite red 25 failures 37 errors /107; long GPU replay + short real WASAPI passing prior evidence. Docs checkpoint will add further commits and change PR head but not source code.
- Source task: `docs/ai-orchestrator/runs/publish-asr-source-20261023/report.md`

<!-- ai-orchestrator:checkpoint:v1:live-memory-diagnosis-20261024 -->
## Durable checkpoint: live-memory-diagnosis-20261024
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `22491c9c486121788a62b0030f78c5dc0d648ac9`
- Verified knowledge to retain: Live memory diagnosis 2026-10-24: user program running, two independent server PIDs sampled. 16396 9.63->11.83GB private over 85s while Python ~822MB; after app process replacement, server12356 5.73->7.90GB over110s while Python ~822MB, then app stopped; NO model interference. Actual llama-server 0.5.0-dev build11368 `-c8192 -np2`; /props two contexts 4096 each. Executable `--cache-ram` default 8192MiB, env LLAMA_ARG_CACHE_RAM=0 to disable RAM cache. Strong evidence host-memory creep from 8GiB prompt RAM cache, matching earlier ~5.6->13.33GiB plateau; not proven without A/B. Current code original ASR uses cache_prompt True, no --cache-ram override. No code or user config changed. Detailed report ignored path tests/results/asr_restore_20261023/live_memory_root_cause_20261024.md.
- Source task: `docs/ai-orchestrator/runs/live-memory-diagnosis-20261024/report.md`

<!-- ai-orchestrator:checkpoint:v1:hotwords-ab-test-20261024 -->
## Durable checkpoint: hotwords-ab-test-20261024
- Outcome: `COMPLETED`
- Code HEAD before memory writes: `3ce44dce50ed82c96deef759afe1363bcb9c6aa8`
- Verified knowledge to retain: 2026-10-24 real local-GPU test controlled lang ja, ctx8192 total, slots2, previous context off, --cache-ram0. 31:01.851s user-provided Japanese podcast as fixed eight-second clips: no hotwords 56.61s inference, RTF0.03041, echo0; 3 words 55.73s, echo4; 12 words56.92s, echo2; 50 words64.39s, echo2. All 233/233 clips each, no model errors. Llama service private RAM ~5.09-5.15 GiB with disabled cache RAM. Full-podcast ground-truth transcript unavailable; no full-audio CER claim. 8 Microsoft Haruka Desktop synthetic Japanese sentences 33.4s total per mode with reference texts: strict CER 0=24.4%, 3=23.44%, 12=14.35%, 50=77.99%. 12-word prompt improves synthetic CER but echoes entire list twice in long real podcast at 688s and1104s. 3 words echo4 clips; 50 words echo two and one TTS severe contamination. Recommendation no hotwords on general-purpose mixed videos pending safe prompt strategy and retest; current hotwords.txt 50 unchanged. Local evidence tests/results/asr_restore_20261023/hotwords_ab_20261024/report.md. Local app SHA256 25dac0471f71ed5d7196b4c90b7b6afe1166224979e8cb9d294baf5f575a01b0. No audio/transcripts/upload of personal source; no code pushes. This GitHub branch only receives documentation.
- Source task: `docs/ai-orchestrator/runs/hotwords-ab-test-20261024/report.md`

<!-- ai-orchestrator:checkpoint:v1:asr-parameter-pipeline-20261024 -->
## Durable checkpoint: asr-parameter-pipeline-20261024
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `36183f2048f2a2ef577adf833ad047e1a9afc60c`
- Verified knowledge to retain: 2026-10-24 user request to repair ALL available Windows Qwen3-ASR UI/CLI/settings parameter propagation on local dirty user checkout. Local both pyw entrypoints identical final SHA256 dfa993fdbc996fded0634c030bd7b68842080860920e4483eaa0c1621f216fda, original before SHA 25dac0471f71ed5d7196b4c90b7b6afe1166224979e8cb9d294baf5f575a01b0. Original TerryKiddy upstream QwenBackend/LlamaBackend/run_asr and original UI/translation/main AST preserved; adapter fixes HTTP max_tokens/temperature, HF max_new_tokens, final history/char context, configurable VAD threshold,min_speech,min_silence,padding via isolated AST clone. ctx explicit8192 no longer mis-sentinel to saved4096; local --cache-ram0 preserved. All19 numeric CLI setting bounds validated incl disabled max_seg=0. User hotwords still disabled by renamed file, original user settings unchanged. Unsupported upstream placeholders stable_count/punct_silence/hesitation_silence warn if nondefault, not secretly implemented; external llama-url warns local flags don't apply. 14/14 fresh focused tests and 16/16 pinned upstream regressions pass; real Q8_0 RTX3090 8s HTTP smoke and 36s actual Silero VAD+Final/Partial virtual PCM replay on final exact SHA pass: audio36/36s, 5 Final/81 Partial, no errors, runtime VAD .25/100/100/200, max_tokens512. 107 historical legacy tests fail25/error36 exactly matching pre-change baseline by test name; full suite NOT green. HF dependencies torch/transformers/qwen_asr absent, mocked only; actual physical WASAPI not re-tested. Local ignored report tests/results/asr_restore_20261023/asr_parameter_bridge_report_20261024.md, result JSON and 107 legacy log; no source, transcript or user audio uploaded; GitHub documentation-only checkpoint, no code commit/push or merge.
- Source task: `docs/ai-orchestrator/runs/asr-parameter-pipeline-20261024/report.md`

