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

