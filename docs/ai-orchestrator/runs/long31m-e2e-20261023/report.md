<!-- ai-orchestrator:checkpoint:v1:long31m-e2e-20261023 -->
# Task report: long31m-e2e-20261023

- Repository: `A942199/Qwen3-LiveCaption`
- Proposed branch: `agent/restore-terrykiddy-asr-20261023`
- Code HEAD before memory writes: `5869f15c80f0dd2fb8c79d415e8d7a883bf3543c`
- Outcome: `AWAITING_REVIEW`

## Observed task summary
Full 31:01.851 Japanese podcast input (MP3 sha256 1494b4f6af1761d59f969c2554f0beb8f8522f7af104971ca6f4550df00ad737) decoded all 29,789,623 16k mono PCM samples, accelerated virtual audio input 10x through unchanged original TerryKiddy run_asr code object, RTX 3090 GGUF llama.cpp, live configured Google2 API, actual Tk subtitle labels hidden for usability. Local Windows successful second full replay elapsed 197.5s, 232 ASR Final, 1006 partials, 1305 model transcribe responses, 1083 translation requests, 923 API responses, 160 obsolete cancellation, 1 real Google2 Unexpected API response format, 1287 Chinese Tk render updates, zero remaining translation worker threads / llama-server processes. Local .pyw sha256 4eb8e80a935395bc2d5298babe8512ce059715d3a4af59b588893fa46b9aed6f unchanged. First full replay detected Windows GBK Japanese middle-dot UnicodeEncodeError in console output which crashed final worker; UTF8 redirected logging rerun passed; source code was not changed. Old legacy unittest suite 107 tests with 5 failures 37 errors, not resolved. This was accelerated simulated audio transport rather than physical 31min WASAPI continuous playback; separate previous 36sec 1x hardware captures passed. No code commit/push/merge.

## Durable knowledge
Full 31m Japanese test audio in %TEMP%/yisheng-benchmark-20261020/yurugengo_479_31m.mp3 sha256 1494b4f6af1761d59f969c2554f0beb8f8522f7af104971ca6f4550df00ad737. Isolated ignored Windows test artifacts tests/results/asr_restore_20261023/long_31m_20261023_165616/{summary.json,events.jsonl} and long_31m_e2e_report_20261023.md. ASR+Google2+Tk dataflow pass over all 1861.851 seconds input with 10x accelerated virtual capture; hardware WASAPI 31-minute sustained stream not asserted. Windows GBK stdout encoding risk may crash final worker on katakana middle dot; rerun via UTF8 stdout avoids it without modifying code. One Google2 API response format error, 160 expected stale partial task cancellations, translation semantic duplicate bug remains. Quality/CI not green. Local source uncommitted unchanged.

## Evidence links
- https://github.com/A942199/Qwen3-LiveCaption/issues/16
- https://github.com/A942199/Qwen3-LiveCaption/pull/17

## Release guard
Do not merge on this document alone; review and CI are not independently verified by memory.

