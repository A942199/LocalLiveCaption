<!-- ai-orchestrator:checkpoint:v1:live-memory-diagnosis-20261024 -->
# Task report: live-memory-diagnosis-20261024

- Repository: `A942199/Qwen3-LiveCaption`
- Proposed branch: `agent/asr-ui-param-fix-20261023`
- Code HEAD before memory writes: `22491c9c486121788a62b0030f78c5dc0d648ac9`
- Outcome: `AWAITING_REVIEW`

## Observed task summary
Read-only live Windows diagnosis on 2026-10-24 using user-running app live-caption-ja-restore.pyw SHA256 afe3764de7d998d59b0c6d42d5d4b4b5e2be1a1798b120febb53e6cb0fb56182. Process PID 16396 llama-server.exe from 9629.6 to 11826.8 MiB private over 85s (+2197.2), while pythonw PID6036 stayed ~822 MiB; app processes disappeared and reappeared under PID 12356 server, which grew 5726.6 to 7901.7 MiB (+2175.1) over ~110sec; pythonw PID3360 820–823 MiB. All model/app processes disappeared again by ~12:38. No diagnostic command restarted/killed them, restart/exit cause unknown. Direct local llama-server.exe --version 0.5.0-dev build11368 1fb7ef3e3 and --help verified `--cache-ram` default 8192MiB (8GiB), 0 disables, env LLAMA_ARG_CACHE_RAM. User running cmd -c8192 -np2 with NO -cram override, backend /props returned 2 slots n_ctx4096 each, cache_prompt True in original ASR. Strong likelihood increasing host RAM is different audio inputs filling an 8GiB prompt cache, consistent with prior accelerated full audio server host memory rising from about 5.6GiB to plateau ~13.33GiB (delta ~7.73GiB). Causal link NOT confirmed by cache=0 A/B. Not Py/Tk unbounded queue issue in this sample. System RAM 31.76GiB, 14.53GiB available, VRAM 5334/24576MiB. No product changes or settings edits/restarts/GitHub source writes. Recommendation next restart controlled LLAMA_ARG_CACHE_RAM=0 before launch to test removing RAM cache, retain upstream recognition algorithm and compare similar workload. Local evidence ignored tests/results/asr_restore_20261023/live_memory_root_cause_20261024.md + live_memory_20261024_1233.jsonl + live_memory_20261024_dynamic_pids.jsonl. Full legacy tests previously not green; no tests executed for new intervention because no change.

## Durable knowledge
Live memory diagnosis 2026-10-24: user program running, two independent server PIDs sampled. 16396 9.63->11.83GB private over 85s while Python ~822MB; after app process replacement, server12356 5.73->7.90GB over110s while Python ~822MB, then app stopped; NO model interference. Actual llama-server 0.5.0-dev build11368 `-c8192 -np2`; /props two contexts 4096 each. Executable `--cache-ram` default 8192MiB, env LLAMA_ARG_CACHE_RAM=0 to disable RAM cache. Strong evidence host-memory creep from 8GiB prompt RAM cache, matching earlier ~5.6->13.33GiB plateau; not proven without A/B. Current code original ASR uses cache_prompt True, no --cache-ram override. No code or user config changed. Detailed report ignored path tests/results/asr_restore_20261023/live_memory_root_cause_20261024.md.

## Evidence links
- https://github.com/A942199/Qwen3-LiveCaption/issues/18
- https://github.com/A942199/Qwen3-LiveCaption/pull/19

## Release guard
Do not merge on this document alone; review and CI are not independently verified by memory.

