<!-- ai-orchestrator:checkpoint:v1:asr-12core-upstream-parity-20261023 -->
# Task report: asr-12core-upstream-parity-20261023

- Repository: `A942199/Qwen3-LiveCaption`
- Proposed branch: `agent/asr-ui-param-fix-20261023`
- Code HEAD before memory writes: `3a9384279831ade84bf99adee3709dbe062d96bb`
- Outcome: `AWAITING_REVIEW`

## Observed task summary
Windows local user's ASR requested exact source parity with TerryKiddy/live-caption pinned commit 794fea1fd50f34e55a6e1e1537aa1659af61fea1; verified GitHub file live_caption.py blob SHA1 2247eed1a91218b358e767db1a87b019e2004bdf and raw UTF8LF SHA256 37e925c5c9aef56ad4d171c1f2af3d1a100166fac0c2f26b653b78cb0958812b. Actual GitHub authoritative source (not merely local fixture) AST matches 12/12 ASR core top-level definitions including LlamaBackend and run_asr; 5 application-level differences intentional fmt_ts, ConsoleSink, OverlaySink, MultiSink, main preserve SRT/Tk GUI/translation. Moved last UI ctx_size core code alteration outside upstream to isolated _LCContextBackend(LlamaBackend) launch adapter; default 8192 delegates upstream init, saved GUI ctx4096 launches dedicated llama-server -c4096 -np2; run_asr original AST no extra arg. Original model/VAD/capture/scheduling/partial/final algorithm definitions unmodified. Both main local launchers live-caption-ja.pyw and live-caption-ja-restore.pyw synced SHA256 afe3764de7d998d59b0c6d42d5d4b4b5e2be1a1798b120febb53e6cb0fb56182; safe backup in ignored tests/results/asr_restore_20261023. Tests: 16/16 targeted upstream SHA AST + UI/ctx backend tests GREEN. 31:01.851 Japanese podcast complete accelerated 10x real RTX3090 llama.cpp ASR + actual Google2 API + real Tk, 367 Final, 1151 Partial, 1659 successful model calls, 1237 translation replies, 1453 Chinese Tk renders, all samples, no fatal errors, normal exit and zero server leftovers. Fresh physical Windows Haruka Japanese TTS->SoundCore2 WASAPI loopback->RTX3090 GGUF->real Google2->Tk 36-sec E2E exit0, all 3 utterances covered with 4 Final, 20 Partial, 15 translation responses, 35 Chinese UI frames, clean close. Original 31-minute audio tested via virtual PCM accelerated playback, not 31min physical WASAPI; no ground-truth CER, external independent review, or fresh GitHub CI. Full legacy suite still RED: 107 tests 25 failures +37 errors; a majority target removed pre-upstream custom ASR/old preset. Google2 had 3 response-format error replies and 131 expected obsolete translation cancellations; Chinese duplicate translation quality issue remains unresolved. No code pushed/committed/merged; only GitHub docs checkpoint.

## Durable knowledge
TerryKiddy/live-caption original pinned commit 794fea1fd50f34e55a6e1e1537aa1659af61fea1 genuine GitHub file live_caption.py sha1 git blob 2247eed1a91218b358e767db1a87b019e2004bdf SHA256 37e925c5c9aef56ad4d171c1f2af3d1a100166fac0c2f26b653b78cb0958812b. Previous local fixture only prepended 2 comment lines, AST same. Current Windows local main+restore .pyw SHA256 afe3764de7d998d59b0c6d42d5d4b4b5e2be1a1798b120febb53e6cb0fb56182. 12/12 original ASR core AST nodes identical, 5 UI/SRT/translation/startup divergence intentionally preserved. _LCContextBackend adapter implements configured ctx4096 externally while original LlamaBackend and run_asr source definitions stay AST-identical. Tested RTX3090 real Google2 31m accelerated full input+real 36sec WASAPI, 16/16 targeted guard green; existing suite 107 with 25fails37errors remains not green, Google2 occasional error and duplicate Chinese semantics remain. No source pushed; local ignored tests/results/asr_restore_20261023/exact_terrykiddy_asr_parity_report_20261023.md.

## Evidence links
- https://github.com/A942199/Qwen3-LiveCaption/issues/18
- https://github.com/A942199/Qwen3-LiveCaption/pull/19

## Release guard
Do not merge on this document alone; review and CI are not independently verified by memory.

