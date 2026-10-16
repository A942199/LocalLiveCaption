# ASR Final reliability — request to merge to main
Run: `asr-final-reliability-merge-20261016`
Outcome: **BLOCKED / NOT MERGED**

## Implementation
- Verified repository: `A942199/LocalLiveCaption`; remote main baseline: `8b66779a1066a55909239fc84bd9c0388cf4a409`.
- Original Windows dirty `main` working tree was preserved.
- A clean isolated Windows worktree based on remote main received only the ASR changes from the previous isolated candidate. No cleanup PR #7 content was merged.
- Code commit `cd4d381c7ea4fd703d213122daac284e860de10c` changed exactly two files: `live-caption-ja.pyw` and `tests/test_asr_final_reliability.py`.
- Code branch `agent/asr-final-reliability-code-20261016` was pushed; code PR: https://github.com/A942199/LocalLiveCaption/pull/9.
- Documentation-only PR #8 remains separate.

## Verification
- On isolated Windows Python 3.11 worktree based on remote main: `unittest discover -s tests -p "test_asr_*.py" -q`: **48/48 pass**.
- Same candidate: `unittest discover -s tests -p "test_*.py" -q`: **105/105 pass**.
- Staged `git diff --check`: no reported errors.
- No current live RTX 3090 Japanese CER/latency test.

## Live merge gates on PR #9
- PR head: `cd4d381c7ea4fd703d213122daac284e860de10c`; GitHub mergeability: clean.
- GitHub Actions workflow `CI/CD Pipeline` concluded `skipped`; checks `Build and Test` and `Create GitHub Release` both `skipped`, **not successful CI verification**.
- PR reviews: none. No verified independent approval.
- GitHub branch protection inspection: connector returned `403 Resource not accessible by integration`; unable to validate required rules.
- No conditional merge action executed; remote `main` unchanged at time of check.

## Follow-up gate
Obtain real passing applicable CI, independent review and verifiable branch policy; only then reconsider a conditional merge on exact live PR head. Do not force merge, reset dirty checkout or claim skipped checks passed.
