# 翻译系统真实联网及稳定性复验（2026-10-15）

本报告续接 `PARITY_IMPLEMENTATION_2026-10-15.md`，聚焦上游翻译功能移植之后的**真实翻译网络、集成管道、异常/热更新稳健性**，不变更原有 ASR、VAD 或音频路径。

## 验证环境

- Windows WebCodex 项目：`C:\Users\uu\Documents\webcodex\live-caption`
- 目标入口：`live-caption-ja.pyw`，运行时翻译模块内嵌在同一文件；`translation/*.py` 为同步源码备份。
- 上游对照基准：`SakiRinn/LiveCaptions-Translator`，commit `a6fee12757b15edbeef7c60f8895e0be694801e1`。
- 本轮实际加载项目 Skill：`realtime-caption-stability`、`code-review-and-quality`、`unittest-skill`、`systematic-debugging`。

## 真实联网结果（仅测试公开合成文本，不读取用户的实时字幕）

| 服务 | 输入 | 返回 | 单次实测请求耗时 |
|---|---|---|---|
| Google | 今日はいい天気ですね。 | 今天天气很好。 | 275ms |
| Google2 | 今日はいい天気ですね。 | 今天天气不错。 | 291ms |

- 含上下文标记的公开合成文本 `昨日は雨でした。 🔤 今日は晴れです。 🔤`，Google 与 Google2 均返回可提取的 `🔤…🔤` 当前句译文。
- **端到端测试**：实际使用 `live-caption-ja.pyw` 内嵌模块，不启动真实 ASR/麦克风：调用 `partial/final` 注入公开合成日语；Google 在线返回中文，字幕 Overlay Mock 收到正确结果，并在隔离的临时 SQLite 中记录源文、译文。分别验证两段连续字幕；最终版本另外验证 `日本語を勉強しています → 我正在学日语。`，数据库 1 条与字幕事件一致。
- **真实 Tkinter 热更新 GUI**：用实际设置窗口选择 Google2 并点击保存，确认 `translation-settings.json` 与当前运行的翻译对象均立即切换 Google2；未重启或修改 ASR。

## 故障复现和本次修复

| 情况 | 旧行为（实际重现） | 修复后 |
|---|---|---|
| 开启非 LLM 上下文时 API 失败 | `[ERROR] Translation Failed: HTTP Error - 503` 会被 `🔤…🔤` 提取规则吞掉，变为空白译文且记录空白历史 | 错误原文直接通过翻译调度层发送，并按错误记录日志 |
| GUI 热切换翻译服务，旧请求未完成 | 旧任务仍可完成并把旧译文覆盖成新界面显示结果 | 设置更新取消未完成翻译、清理翻译侧等待队列，并丢弃过期网络请求返回；不改 ASR |
| 最小翻译对象没有 Overlay | 设置更新引发 `AttributeError`（完整回归发现） | Overlay 可选，避免空窗口/测试对象崩溃 |

为上述故障新增 `tests/test_translation_live_resilience.py` 的 3 个可复现/回归用例。

## 最终验证命令和结果

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v
```

- **92/92 全部通过**；其中包括原 ASR 稳定性回归、上游翻译契约、无 `translation/` 单文件隔离加载，以及 3 个新的故障注入回归。
- 原 `.pyw` 文件中翻译源码之前的主程序前缀 SHA256 一直为：`7e0667b255cf824e07acfd2da164b4841518c1df42b96bd0ce69d90d632fa5a7`。
- 单文件实现最终与六个 `translation/*.py` 源文件同步；不要求程序运行时携带 `translation/` 目录。

## 仍未完成的真实性证明

- **未采集用户麦克风、系统音频或私有字幕**：未进行真机 Qwen3-ASR + 连续真实音频端到端测试。为保护用户音频隐私，此次使用公开合成日语注入。
- **其他 9 家翻译接口未执行凭据授权下的实际网络联调**，已通过离线 Mock 与上游代码契约测试，但不能宣称其外部系统响应在真实环境已全面验收。
- **未启动 .NET/WPF 上游应用进行同步播放逐帧比对**；静态与契约一致不等于所有系统/视觉行为精确相同。
- **阻塞式 urllib 不能做到与 .NET CancellationToken 相同的在途 HTTP 强取消**；旧任务响应可被阻止发布，但请求可能运行到网络超时。属于明确保留的运行时差异。
- 未执行真实 GUI 中长时间运行、网络波动或服务器限流的持久稳定性压力测试。

## Git 和用户文件

本轮不执行 git reset/clean、commit、push；保留已有未提交和未跟踪文件。修改范围：`translation/engine.py`、`live-caption-ja.pyw` 内嵌翻译区、新的 `tests/test_translation_live_resilience.py`，及本报告。
