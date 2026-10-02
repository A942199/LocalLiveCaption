# LocalLiveCaption

Windows 本地实时字幕工具。本项目在开源项目基础上进行修改，主要用于 **日语实时语音识别 + 中文翻译字幕**。

## 代码出处

本项目主要基于以下开源项目实现：

- [TerryKiddy/live-caption](https://github.com/TerryKiddy/live-caption)  
  本项目的音频采集、WASAPI loopback、Silero VAD、Qwen3-ASR / llama.cpp 推理、实时 partial/final 字幕处理和悬浮字幕窗口等核心 ASR 代码均基于该项目修改。

- [SakiRinn/LiveCaptions-Translator](https://github.com/SakiRinn/LiveCaptions-Translator)  
  Google 翻译调用方式，以及翻译触发时机、未完成句翻译、完整句立即翻译、翻译任务新旧结果处理等逻辑参考并适配自该项目。

同时使用：

- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) — 语音识别模型
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — GGUF 模型 GPU 推理
- [Silero VAD](https://github.com/snakers4/silero-vad) — 语音活动检测
- [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) — Windows WASAPI loopback 音频采集

感谢以上项目及其作者。

## 本项目实现的功能

- 直接捕获 Windows 当前播放的系统音频，无需虚拟声卡。
- 使用 **Qwen3-ASR-1.7B + llama.cpp + NVIDIA GPU** 在本地实时识别日语。
- 说话过程中实时刷新日语临时字幕，并在一句话结束后输出最终字幕。
- 使用 Google Translate 将识别到的日语实时翻译为简体中文。
- 翻译时机参考 LiveCaptions-Translator：
  - 完整句出现句末标点时立即翻译；
  - 未完成句在字幕连续变化达到阈值时提前翻译；
  - 字幕停止变化约 1.25 秒时也会触发翻译；
  - 较新的翻译结果不会被较旧任务覆盖。
- 悬浮字幕窗口同时显示日语和中文翻译，并保留上一句日语作为上下文显示。
- 字幕窗口支持拖动、鼠标滚轮调整字号、右上角 **×** 或 **Esc** 关闭。
- 日语识别在本地完成；**中文翻译需要联网，并会将识别后的文本发送给 Google Translate**。

主程序：

`live-caption-ja.pyw`
