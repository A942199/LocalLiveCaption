# Direct merge to main — ASR Final reliability

Run ID: `asr-final-reliability-direct-merge-20261016`
Outcome: **BLOCKED (not merged)**

- Repository: `A942199/LocalLiveCaption`.
- PR: https://github.com/A942199/LocalLiveCaption/pull/9.
- Verified PR head: `cd4d381c7ea4fd703d213122daac284e860de10c` (two-file ASR patch).
- Verified main at gate: `8b66779a1066a55909239fc84bd9c0388cf4a409`.
- PR mergeability from GitHub: clean, no conflicts; user explicitly authorized a direct merge.
- Previous Windows local tests: 105/105 full suite passed, 48/48 ASR suite passed on current code SHA.
- Live GitHub Actions for PR head: `CI/CD Pipeline` completed `skipped`, `Build and Test` conclusion `skipped`, `Create GitHub Release` conclusion `skipped`. A skipped CI check is not a passing check.
- GitHub PR reviews: none; no independent review or approval.
- Branch protection endpoint: HTTP 403 `Resource not accessible by integration`; unable to verify current applicable protection/rules.
- The orchestrator's mandatory release gate cannot be satisfied. No merge or forced branch update was executed; user's local dirty checkout remained untouched.
- This branch/PR #8 is a documentary checkpoint. PR #9 is the actual code patch. Next step: run verifiable CI, obtain independent review, inspect branch rules, then recheck exact head before conditional merge.
