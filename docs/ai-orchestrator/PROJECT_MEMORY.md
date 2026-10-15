# Project memory: LocalLiveCaption

- Repository: `A942199/LocalLiveCaption`
- Source HEAD at initialization: `8b66779a1066a55909239fc84bd9c0388cf4a409`
- Initialized for run: `translation-semantic-20261015`

## Verified project purpose
Real-time Japanese ASR subtitles and Chinese translation; preserve ASR and improve translation semantic scheduling.

## Durable decisions and lessons
Add only verified project decisions and outcomes; do not include secrets.

<!-- ai-orchestrator:checkpoint:v1:translation-semantic-20261015 -->
## Durable checkpoint: translation-semantic-20261015
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `6d39af34ca0af7ae88084bc5c53de90591506cc5`
- Verified knowledge to retain: Qwen3-ASR Final is an audio-segment boundary, not a guaranteed Japanese sentence ending; use stable Partial for preview and bounded joined Final as authoritative translation input. Suppress stale previews and keep visible subtitles until replacement. Standalone pyw embeds translation/engine.py verbatim; always sync both.
- Source task: `docs/ai-orchestrator/runs/translation-semantic-20261015/report.md`

<!-- ai-orchestrator:checkpoint:v1:translation-flicker-acceptance-20261015 -->
## Durable checkpoint: translation-flicker-acceptance-20261015
- Outcome: `AWAITING_REVIEW`
- Code HEAD before memory writes: `3a56840ff41d7a59c34ba197a82f36bddc5e83cf`
- Verified knowledge to retain: Caption blanking is not caused by normal Partial/Final in tested cases; distinguish translation publication (nonempty), Tk root hidden auto-hide after 6s and ASR status kind=s clearing label. Frequent Partial triggers repeated text configure (19 for 24 Partial) and may be worth future UI redraw optimization. Keep explicit auto-hide semantics and disable the setting if users need persistent overlay.
- Source task: `docs/ai-orchestrator/runs/translation-flicker-acceptance-20261015/report.md`

