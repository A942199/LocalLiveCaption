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

