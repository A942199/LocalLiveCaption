# Current project state: Qwen3-LiveCaption

- Repository: `A942199/Qwen3-LiveCaption`
- Initialized for run: `restore-terrykiddy-asr-20261023`
- Original source HEAD: `54727d89e29d2e7fb333a638519417a0c16f0d39`
- Task status: `INITIALIZED` (not a completed or merged task)

## Task checkpoints
A task checkpoint is not proof of tests, independent review, CI or merge.

<!-- ai-orchestrator:checkpoint:v1:restore-terrykiddy-asr-20261023 -->
## Task status update: restore-terrykiddy-asr-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `e4d342030544b891d73a5c13c1142df31d24c0e4`
- Summary: Windows local dirty checkout only: replaced ASR capture/backend/VAD/scheduler in live-caption-ja.pyw with TerryKiddy upstream 794fea1; original UI+translation AST hashes unchanged. Upstream parity 3 tests, fake audio integration 1, overlay 9, translation port 11, translation cancel 9, translation resilience 3, model preset 2, syntax and isolated .pyw import all passed. Full legacy suite: 101 tests, 5 failed, 37 errored, mostly preexisting expectations of removed custom ASR; translation engine embedding mismatch independently confirmed in pre-edit backup. Real GPU/audio not tested. Local restored source remains uncommitted, no code push/merge. Independent reviewer and GitHub CI absent.
- Evidence references: 1
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:asr-translation-e2e-20261023 -->
## Task status update: asr-translation-e2e-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `6bd233474e5d78111fd3feab591011070d6bcaf4`
- Summary: Two real Windows 36-second end-to-end trials passed: Japanese WAV output->SoundCore2 WASAPI loopback->RTX3090 Qwen3-ASR GGUF llama.cpp->real user-selected Google2 translation->actual Tk label. Session1 5 final+7 partial, 12 translation responses, 23 Chinese render events; session2 uses exact user saved ASR settings, 4 final+16 partial, 15 translation responses, 30 Chinese render events. Both exit 0, no leftover llama server or translator threads. User source/settings/history untouched, tested local source SHA256 4eb8e80a935395bc2d5298babe8512ce059715d3a4af59b588893fa46b9aed6f. Broader legacy tests still fail 5 and error 37 of 107; overlapping partial translations can display duplicated Chinese meaning. No code push, review or merge.
- Evidence references: 2
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:long31m-e2e-20261023 -->
## Task status update: long31m-e2e-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `5869f15c80f0dd2fb8c79d415e8d7a883bf3543c`
- Summary: Full 31:01.851 Japanese podcast input (MP3 sha256 1494b4f6af1761d59f969c2554f0beb8f8522f7af104971ca6f4550df00ad737) decoded all 29,789,623 16k mono PCM samples, accelerated virtual audio input 10x through unchanged original TerryKiddy run_asr code object, RTX 3090 GGUF llama.cpp, live configured Google2 API, actual Tk subtitle labels hidden for usability. Local Windows successful second full replay elapsed 197.5s, 232 ASR Final, 1006 partials, 1305 model transcribe responses, 1083 translation requests, 923 API responses, 160 obsolete cancellation, 1 real Google2 Unexpected API response format, 1287 Chinese Tk render updates, zero remaining translation worker threads / llama-server processes. Local .pyw sha256 4eb8e80a935395bc2d5298babe8512ce059715d3a4af59b588893fa46b9aed6f unchanged. First full replay detected Windows GBK Japanese middle-dot UnicodeEncodeError in console output which crashed final worker; UTF8 redirected logging rerun passed; source code was not changed. Old legacy unittest suite 107 tests with 5 failures 37 errors, not resolved. This was accelerated simulated audio transport rather than physical 31min WASAPI continuous playback; separate previous 36sec 1x hardware captures passed. No code commit/push/merge.
- Evidence references: 2
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:asr-ui-param-fix-20261023 -->
## Task status update: asr-ui-param-fix-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `ed553085d4ff961bc5aa7abd3819ccb5317565a0`
- Summary: Fixed local Windows live-caption-ja.pyw and live-caption-ja-restore.pyw (identical SHA256 f6d48f2db5e7e3203861bcc692466e7515b127ba791435560dd799b9e73e452c): GUI/CLI ctx_size now forwarded to LlamaBackend startup -c; actual RTX3090 llama-server.exe verified -c 4096 -np 2, no accidental reuse of same-model server with different flags; injected launcher default --lang no longer overrides UI language. Hidden false UI knobs that restored TerryKiddy upstream never uses: max tokens/temperature fixed 200/0; history Final count & char limit fixed 1/80; VAD threshold/min speech/min silence/padding fixed 0.45/100/200/100; stable/punctuation/hesitation unsupported. High precision preset sets upstream defaults: ctx 8192, 2 slots, context enabled, VAD threshold 0.45, silence 0.5s, minseg1 maxseg6, 200 tokens, Japanese app language JA preserved. Tests 4/4 new regression and 8/8 runtime/Tk/backend contract green, full 31:01.851s Japanese podcast accelerated virtual audio GPU ASR+real Google2+Tk completed, 367 Final,1002 partial,1093 translation replies,1344 Chinese UI renders; full input consumed, clean server/thread shutdown. Two real 36s Windows SAPI speaker->SoundCore2 WASAPI->RTX3090->real Google2->Tk E2Es returned 0: first missed initial sentence; second covered all 3 utterances (4 Final,12 replies,34 UI frames). Legacy full suite NOT GREEN: 107 tests, 27 failed,36 errored, 19 obsolete high-preset expected values, 2 strict AST parity assertions for deliberately changed constructor call; other old ASR and translation bundling failures. Known duplicate Chinese semantic output unresolved. No source commit or push, no independent reviewer/CI pass or merge. Local test report tests/results/asr_restore_20261023/asr_ctx_ui_fix_report_20261023.md.
- Evidence references: 1
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:asr-12core-upstream-parity-20261023 -->
## Task status update: asr-12core-upstream-parity-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `3a9384279831ade84bf99adee3709dbe062d96bb`
- Summary: Windows local user's ASR requested exact source parity with TerryKiddy/live-caption pinned commit 794fea1fd50f34e55a6e1e1537aa1659af61fea1; verified GitHub file live_caption.py blob SHA1 2247eed1a91218b358e767db1a87b019e2004bdf and raw UTF8LF SHA256 37e925c5c9aef56ad4d171c1f2af3d1a100166fac0c2f26b653b78cb0958812b. Actual GitHub authoritative source (not merely local fixture) AST matches 12/12 ASR core top-level definitions including LlamaBackend and run_asr; 5 application-level differences intentional fmt_ts, ConsoleSink, OverlaySink, MultiSink, main preserve SRT/Tk GUI/translation. Moved last UI ctx_size core code alteration outside upstream to isolated _LCContextBackend(LlamaBackend) launch adapter; default 8192 delegates upstream init, saved GUI ctx4096 launches dedicated llama-server -c4096 -np2; run_asr original AST no extra arg. Original model/VAD/capture/scheduling/partial/final algorithm definitions unmodified. Both main local launchers live-caption-ja.pyw and live-caption-ja-restore.pyw synced SHA256 afe3764de7d998d59b0c6d42d5d4b4b5e2be1a1798b120febb53e6cb0fb56182; safe backup in ignored tests/results/asr_restore_20261023. Tests: 16/16 targeted upstream SHA AST + UI/ctx backend tests GREEN. 31:01.851 Japanese podcast complete accelerated 10x real RTX3090 llama.cpp ASR + actual Google2 API + real Tk, 367 Final, 1151 Partial, 1659 successful model calls, 1237 translation replies, 1453 Chinese Tk renders, all samples, no fatal errors, normal exit and zero server leftovers. Fresh physical Windows Haruka Japanese TTS->SoundCore2 WASAPI loopback->RTX3090 GGUF->real Google2->Tk 36-sec E2E exit0, all 3 utterances covered with 4 Final, 20 Partial, 15 translation responses, 35 Chinese UI frames, clean close. Original 31-minute audio tested via virtual PCM accelerated playback, not 31min physical WASAPI; no ground-truth CER, external independent review, or fresh GitHub CI. Full legacy suite still RED: 107 tests 25 failures +37 errors; a majority target removed pre-upstream custom ASR/old preset. Google2 had 3 response-format error replies and 131 expected obsolete translation cancellations; Chinese duplicate translation quality issue remains unresolved. No code pushed/committed/merged; only GitHub docs checkpoint.
- Evidence references: 2
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:publish-asr-source-20261023 -->
## Task status update: publish-asr-source-20261023
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `cf6de6b4bf272a6867a8d3620f3c82b3a9fe32ea`
- Summary: User explicitly authorized submission of the previously validated Windows local main application code to GitHub. Source file live-caption-ja.pyw local sha256 afe3764de7d998d59b0c6d42d5d4b4b5e2be1a1798b120febb53e6cb0fb56182 (CRLF), 3410 lines, normalized LF Git blob SHA1 5fddbf844442956568dcd4b33181cdaaeec26941. Published through connected GitHub update_file to existing dedicated draft PR19 branch agent/asr-ui-param-fix-20261023 at commit cf6de6b4bf272a6867a8d3620f3c82b3a9fe32ea. Validated branch HEAD after write, blob SHA exact, remote file content verified, PR19 now includes live-caption-ja.pyw plus pre-existing four docs; updated PR title/body from outdated docs-only to source+tests draft. Did not change GitHub main or merge, and did not upload any unrelated dirty Windows worktree changes (.gitignore, deleted README, added tests, translation/ sources), ignored user's JSON configs, logs, GPU models. Two hard-coded GOOGLE2_KEY defaults compared and confirmed unchanged vs public main, preventing new credential disclosure. 16/16 targeted current-source unittest tests passed fresh. Previously full Japanese 31:01.851 accelerated PCM real GPU llama.cpp+Google2+Tk and 36s real WASAPI E2E passed, but legacy suite 107 total 25 failures 37 errors and Google2 response errors/duplicate translation persist; no current CI or independently verified review. Draft PR release blocked. Exact commit https://github.com/A942199/Qwen3-LiveCaption/commit/cf6de6b4bf272a6867a8d3620f3c82b3a9fe32ea, PR https://github.com/A942199/Qwen3-LiveCaption/pull/19.
- Evidence references: 3
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:live-memory-diagnosis-20261024 -->
## Task status update: live-memory-diagnosis-20261024
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `22491c9c486121788a62b0030f78c5dc0d648ac9`
- Summary: Read-only live Windows diagnosis on 2026-10-24 using user-running app live-caption-ja-restore.pyw SHA256 afe3764de7d998d59b0c6d42d5d4b4b5e2be1a1798b120febb53e6cb0fb56182. Process PID 16396 llama-server.exe from 9629.6 to 11826.8 MiB private over 85s (+2197.2), while pythonw PID6036 stayed ~822 MiB; app processes disappeared and reappeared under PID 12356 server, which grew 5726.6 to 7901.7 MiB (+2175.1) over ~110sec; pythonw PID3360 820–823 MiB. All model/app processes disappeared again by ~12:38. No diagnostic command restarted/killed them, restart/exit cause unknown. Direct local llama-server.exe --version 0.5.0-dev build11368 1fb7ef3e3 and --help verified `--cache-ram` default 8192MiB (8GiB), 0 disables, env LLAMA_ARG_CACHE_RAM. User running cmd -c8192 -np2 with NO -cram override, backend /props returned 2 slots n_ctx4096 each, cache_prompt True in original ASR. Strong likelihood increasing host RAM is different audio inputs filling an 8GiB prompt cache, consistent with prior accelerated full audio server host memory rising from about 5.6GiB to plateau ~13.33GiB (delta ~7.73GiB). Causal link NOT confirmed by cache=0 A/B. Not Py/Tk unbounded queue issue in this sample. System RAM 31.76GiB, 14.53GiB available, VRAM 5334/24576MiB. No product changes or settings edits/restarts/GitHub source writes. Recommendation next restart controlled LLAMA_ARG_CACHE_RAM=0 before launch to test removing RAM cache, retain upstream recognition algorithm and compare similar workload. Local evidence ignored tests/results/asr_restore_20261023/live_memory_root_cause_20261024.md + live_memory_20261024_1233.jsonl + live_memory_20261024_dynamic_pids.jsonl. Full legacy tests previously not green; no tests executed for new intervention because no change.
- Evidence references: 2
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:hotwords-ab-test-20261024 -->
## Task status update: hotwords-ab-test-20261024
- Outcome: `COMPLETED`
- Code HEAD before memory writes: `3ce44dce50ed82c96deef759afe1363bcb9c6aa8`
- Summary: Local Windows RTX3090 ASR hotword A/B completed: four independent Q8_0 GGUF 31m offline replay passes of 233 paired 8s clips each, 0 model errors, plus four runs of eight labeled Japanese TTS utterances. Prompt-echo bug confirmed for lists of 3/12/50 (4/2/2 affected clips), not observed with 0. Source/settings/hotwords unchanged; local private report saved.
- Evidence references: 0
This is a task checkpoint, not a merged default-branch state.

<!-- ai-orchestrator:checkpoint:v1:asr-parameter-pipeline-20261024 -->
## Task status update: asr-parameter-pipeline-20261024
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `36183f2048f2a2ef577adf833ad047e1a9afc60c`
- Summary: Local Windows Qwen3-ASR parameter pipeline repaired and validated with real GPU. HTTP max_tokens/temperature, dynamic VAD four knobs, context history/chars, HF max tokens, explicit ctx8192 corrected; all19 numeric bounds. 14 new focused tests and16 pinned upstream regressions pass. Real RTX3090 36s Q8_0+Silero+ASR worker test 36/36 sec with5 Final81 Partial and zero errors, user VAD setting .25/100/100/200 + tokens512 applied. Pre-existing full legacy 107 fail25/error36 unchanged. Upstream pinned source/UI/translation, user settings, disabled hotwords unchanged. Local source SHA dfa993fdbc996fded0634c030bd7b68842080860920e4483eaa0c1621f216fda; no code push.
- Evidence references: 0
This is a task checkpoint, not a merged default-branch state.

