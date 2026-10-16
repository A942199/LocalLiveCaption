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
- 说话过程中以完整当前 utterance 做 partial 识别；RTX 3090 实测性能足够，避免非 stateful ASR 使用 rolling window 时产生错拼。
- 使用 **Recoverable Stable Prefix**：连续多次一致的日语前缀才锁定；单次冲突先抑制，若连续 3 次都冲突则自动解锁并纠正，既减少字幕抖动也避免早期错字被永久锁死。
- **沿用原作者的切段 / Final 复用策略**：自然静音满足结束条件时，若最新 Partial 已覆盖全部有效语音，可直接复用其原始完整识别结果作为 Final；否则重新识别。达到 `max_seg` 时必须完整重新识别当前段，随后清空音频缓冲，下一段不带重叠音频；不执行跨段去重或 Bridge 猜测修复。原有 Final 失败重试、任务队列和字幕稳定机制保留。
- 使用 **Smart Endpointing**：普通静音、句末标点、日语犹豫/连接词使用不同的 speech-final 等待时间，并保留 max-seg 强制切分。
- 默认使用 Google Translate 日译中；翻译设置支持 **11 种上游接口**、API 多配置档案、上下文、历史搜索/清空/CSV 全量导出和运行中热更新。
- 翻译时机参考 LiveCaptions-Translator：
  - 完整句出现句末标点时立即翻译；
  - 未完成句在字幕连续变化达到阈值时提前翻译；
  - 字幕停止变化约 1.25 秒时也会触发翻译；
  - 较新的翻译结果不会被较旧任务覆盖。
- 悬浮字幕窗口同时显示日语和中文翻译；历史日语字幕数量可在 **⚙ 设置**中设为 `0～5` 句，默认 `1` 句。
- 字幕窗口支持拖动、鼠标滚轮调整字号、右上角 **⚙ 设置**、**×** 或 **Esc** 关闭。
- 设置面板可调整并保存：ASR 主模型（Q8_0 / BF16）、字体、字号、窗口透明度、窗口宽度、距屏幕底部距离、字幕停留时间、历史字幕数量（0～5）、日语/中文/历史字幕/背景颜色、窗口置顶、是否显示历史字幕、当前字幕粗体。
- **识别参数**子窗口可调整识别语言（auto 自动检测 / ja 固定日语）、上下文、llama context、slots、max tokens、temperature、VAD、partial 刷新、Stable、静音与句段等参数。“高精度推荐”使用 2026-10-15 日语音频复测候选：Q8_0、固定日语、关闭跨句上下文、4096 ctx、2 slots、256 tokens、温度 0、0.4 秒 partial、VAD 阈值 0.1、200ms 语音 padding、16 秒最长句段等；应用后仍可微调。
- 字幕外观即时生效；ASR 模型、语言和识别参数在下次启动生效。配置保存在本地 `live-caption-settings.json`，不会提交到 Git；显式命令行参数（包括 `--lang`）仍然优先。旧配置不含 `asr_language` 时保持自动语言检测。
- 模型选择默认使用 Q8_0；选择 BF16 后会加载 `models/Qwen3-ASR-1.7B-bf16.gguf`。若 BF16 文件缺失，会自动回退到 Q8_0。显式传入命令行 `--model` 时，以命令行指定模型为准。
- 日语识别在本地完成；中文翻译将原文发送至**用户选择的翻译服务**（Google/Google2 等在线接口或自行部署的本地 Ollama、LM Studio、MTranServer），是否需要联网取决于服务部署方式。

主程序：

`live-caption-ja.pyw`

## Python 翻译子系统

**所有主要业务代码现已整合进 `live-caption-ja.pyw` 单文件**：ASR、VAD、音频、翻译任务队列、11 种翻译接口、上下文、SQLite 历史和界面均无需额外 `.py` 模块。`translation/` 目录仍作为 Python 翻译源码备份保留，但**运行时不依赖此目录**。字幕设置提供“翻译设置”和“译文历史”入口，详细说明见 [translation/README.md](translation/README.md)。识别算法、VAD 和音频采集逻辑保持不变。

## 新电脑安装

目标环境：**Windows 10/11 x64 + NVIDIA GPU**。请先安装较新的 NVIDIA 显卡驱动，并保持网络连接。

1. 下载或克隆本仓库。
2. 双击 `setup.bat`。
3. 等待安装完成后，双击 `live-caption-ja.pyw`。

`setup.bat` 会自动完成：

- 检查 Python 3.11+；如果未安装且系统有 `winget`，会自动安装 Python 3.11。
- 创建项目自己的 `.venv` 并安装 Python 依赖。
- 从 Hugging Face 下载 Qwen3-ASR-1.7B 的两个 GGUF 模型文件（约 2.8 GB）。
- 从 llama.cpp 最近的 GitHub Releases 自动选择兼容的 **Windows x64 CUDA** 构建：优先 CUDA 12.x，如当前版本只提供 CUDA 13.x 则自动回退，并下载匹配的 CUDA Runtime DLL，然后解压到 `llama/`。
- 检查模型、Python 依赖和 `llama-server.exe` 是否可用。

安装生成的 `.venv/`、`models/`、`llama/`、日志和字幕文件都已加入 `.gitignore`，不会提交到仓库。

> 日语语音识别完全在本机运行；中文翻译需要联网。若新电脑没有 NVIDIA 驱动，或驱动版本过旧，请先更新驱动。
