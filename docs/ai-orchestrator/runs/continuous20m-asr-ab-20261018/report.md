# Continuous 20-minute Japanese ASR A/B test — 2026-10-18

Run: `continuous20m-asr-ab-20261018` · Issue: https://github.com/A942199/LocalLiveCaption/issues/14

**Result: COMPLETE (test-only, no production code change).** On Windows RTX 3090, using the real upstream and local `run_asr` pipelines with synthetic 3× PCM loopback and Qwen3-ASR-1.7B Q8_0, both processed the same exact 1,200 seconds built from 89 Google FLEURS ja_jp/dev samples. This is **NOT** the original October 10 AivisSpeech novel TTS recording (MP3 no longer located).

| Metric | Upstream TerryKiddy/live-caption | Local dirty LocalLiveCaption |
|---|---:|---:|
| Strict CER | 17.1124% | 14.8912% |
| Normalized CER | 14.4724% | 12.1975% |
| Final outputs | 171 | 150 |
| Partial outputs | 879 | 877 |
| Zero-Final-interval-overlap source clips | 1 | 5 |
| Runtime exceptions | 0 | 0 |

The Final-interval overlap figure is **not** a verified total omission count: several neighboring FLEURS recordings repeat the same reference text. Adjacent identical Finals in both variants were verified as legitimate separately recorded sentences. At least one local missing-interval case merits further review against raw audio.

Upstream source commit: `794fea1fd50f34e55a6e1e1537aa1659af61fea1`. Local checkout HEAD: `e4d342030544b891d73a5c13c1142df31d24c0e4` (dirty; source SHA256 `78E1E182EC6CEAB196EBBE7C00BA77C22AD3965842148B06B62014C5DE4167AF`). Test source WAV SHA256 `9CDC0CBE9CC545BFE59D8FBC884B573B5321A540C5117C67737DE097B4645A85`.

Full local report and reproducible results in ignored `tests/results/upstream_accuracy_20261017/REPORT_CONTINUOUS_20MIN_20261018.md`, `replay_continuous20m_upstream.json`, `replay_continuous20m_local.json`, `stream_replay_long20m.py`. This branch is documentation only. No `main` merge; no real WASAPI, GUI, translation, or original novel MP3 retest. Prior October 10 logs reported 134 Final, 665 Partial and 401/401 successful Google translation requests without a CER score.
