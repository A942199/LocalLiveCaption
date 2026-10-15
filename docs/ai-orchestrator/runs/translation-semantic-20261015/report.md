<!-- ai-orchestrator:checkpoint:v1:translation-semantic-20261015 -->
# Task report: translation-semantic-20261015

- Repository: `A942199/LocalLiveCaption`
- Proposed branch: `agent/translation-stable-partial-20261015`
- Code HEAD before memory writes: `6d39af34ca0af7ae88084bc5c53de90591506cc5`
- Outcome: `AWAITING_REVIEW`

## Observed task summary
Implemented stable Partial translation previews, Final joining without artificial VAD punctuation, 0.9s idle fallback, 360-byte cutoff, request dedup/cache, stale result suppression, no blank overlay update, and synced standalone pyw engine. Real Windows Python unittest suite: 110 tests OK; no live Japanese audio or GUI visual acceptance evidence. No WPF/ASR/VAD/audio code changes; main unchanged.

## Durable knowledge
Qwen3-ASR Final is an audio-segment boundary, not a guaranteed Japanese sentence ending; use stable Partial for preview and bounded joined Final as authoritative translation input. Suppress stale previews and keep visible subtitles until replacement. Standalone pyw embeds translation/engine.py verbatim; always sync both.

## Evidence links
- https://github.com/A942199/LocalLiveCaption/issues/5
- https://github.com/A942199/LocalLiveCaption/commit/6d39af34ca0af7ae88084bc5c53de90591506cc5

## Release guard
Do not merge on this document alone; review and CI are not independently verified by memory.

