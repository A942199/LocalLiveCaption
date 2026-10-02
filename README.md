# live-caption — 本地显卡实时字幕 / Local-GPU Realtime Captions

**把电脑正在播放的任何声音，实时变成屏幕底部的字幕。全程本地显卡运行，不联网、不花钱。**
直播、会议、视频、网课、游戏解说……只要电脑在出声，就能出字。中文识别质量为主要目标，英文及中英混说亦可。

![overlay](docs/overlay.png)

- **延迟约半秒**：从一句话说完到字幕上屏，实测平均 0.54s（RTX 3090）
- **中文准确率**：Qwen3-ASR-1.7B，WenetSpeech 字错率 4.97%（whisper-large-v3 为 9.86%）；口语、快语速、游戏解说明显好于 whisper
- **不用虚拟声卡**：WASAPI loopback 直接抓系统输出，不需要 VB-Cable / 立体声混音
- **像视频字幕**：屏幕底部悬浮条，最新一句大字，上一句缩小变灰，没人说话自动渐隐；可拖动、滚轮调字号
- **热词**：把人名、队名、术语写进 `hotwords.txt`，专有名词识别率大幅提升
- **显存约 3GB**，显卡占用约 13%，可以一边打游戏 / 跑别的一边用

> English summary at the bottom.

## 怎么跑（Windows）

需要：NVIDIA 显卡（6GB+ 显存）、Python 3.10+。

1. 下载本仓库，双击 `setup.bat`：创建虚拟环境、装依赖、下载模型（约 2.8GB，国内可先 `set HF_ENDPOINT=https://hf-mirror.com`）。
2. 下载 [llama.cpp 的 Windows CUDA 构建](https://github.com/ggml-org/llama.cpp/releases)（`llama-bXXXX-bin-win-cuda-13.x-x64.zip` 和同名 `cudart-...zip`），两个都解压到本仓库的 `llama\` 目录里，使 `llama\llama-server.exe` 存在。
   **必须是 2026-04-12 之后的版本**（Qwen3-ASR 支持在 [PR #19441](https://github.com/ggml-org/llama.cpp/pull/19441) 合并），b10941 实测可用。
3. 双击 `start-zh.bat`（中文）或 `start.bat`（自动检测语言）。字幕条出现在屏幕底部，终端同时打印，`transcript.txt` 追加保存。

字幕窗：鼠标拖动移动；滚轮调字号；右键或 Esc 退出。

## 常用参数

```
start.bat --lang zh                 中文（推荐；判成别的语言的段落如音乐会被丢掉）
start.bat --lang en                 英文
start.bat                           自动检测（中英混说）
start.bat --list                    列出输出设备；--device N 抓指定设备（默认跟随系统默认输出）
start.bat --console                 不开悬浮窗，只在终端显示
start.bat --srt out.srt             同时写 srt
start.bat --font-size 40 --bottom 120 --alpha 0.7 --width 0.7
start.bat --hotwords "A,B,C"        临时热词（也可写在 hotwords.txt）
start.bat --debug debug_dir         留存每段 wav + log.jsonl（时长/推理耗时/延迟/文本），调参用
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--silence` | 0.5 | 静音多少秒算一句结束。这是延迟下限，小了断句更碎 |
| `--max-seg` | 6 | 一句最长几秒强制切（决定一行字数，约 20~35 字） |
| `--min-seg` | 1.0 | 短于此秒数先不切，并入下一段 |
| `--partial-every` | 0.4 | 说话中多久刷新一次临时结果 |
| `--vad-win` | 0.2 | VAD 判定窗口 |
| `--hold` | 6 | 没新话几秒后渐隐 |
| `--model` / `--mmproj` | models/…1.7B… | 换 [Qwen3-ASR-0.6B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-0.6B-GGUF) 更省显存 |

## 它是怎么做到半秒的

```
系统音频 (WASAPI loopback) ─► 自动增益 ─► Silero VAD 切句（主线程）
        │                                          │
        │                            ┌─────────────┴──────────────┐
        │                     线程 A：最终句，按序排队        线程 B：临时句，只留最新一份
        │                            └─────────────┬──────────────┘
        │                                   llama-server（2 槽位并行）
        │                                   Qwen3-ASR-1.7B Q8_0 GGUF
        └──────────────────────────────────────────┴──► 悬浮字幕条 / 终端 / txt / srt
```

- 识别引擎是 **llama.cpp**：同一模型用 HF transformers 每秒音频要 0.22s，llama.cpp 只要 0.03s（6 秒音频 0.2s 出字），快 10 倍。
- 说话过程中每 0.4s 就对当前句做一次临时识别；句尾静音判定成立时，如果之后没有新语音，**直接把最后一次临时结果当最终句**，不再推理（实测 90% 命中）。
- 因此延迟 ≈ `--silence` + 一个 VAD 窗口 ≈ 0.6s；GPU 实际只花 0.15s，剩下都在等"这句话说完了没"。
- 主线程只做 VAD 和切句，永远不被推理卡住；临时结果做不过来就丢旧的，不堆积。
- 热词和上一句通过 Qwen3-ASR 的 context 传入，system prompt 前缀命中 KV cache，不增加延迟。
- llama-server 由脚本拉起，并用 Windows Job Object 绑定：程序退出或崩溃，server 一起结束；端口上已有同模型 server 就复用，被占了自动换端口。

## 提高准确率

1. **热词最有效**。`hotwords.txt` 里放你常看内容的人名、队名、产品名、术语，逗号或换行分隔，改完重启。
2. 中文内容加 `--lang zh`；不加时偶尔会输出繁体或把中文判成别的语言。
3. 音源本身混着音乐、音效、多人抢话时错字会增多，这是 ASR 的普遍限制。

## 备用后端

`--backend hf` 用 transformers 直接跑 `Qwen/Qwen3-ASR-1.7B`（需要 `pip install torch qwen-asr`），不依赖 llama.cpp，但慢 10 倍，仅作对照。

## 致谢

- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) — 模型
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — 推理，GGUF 来自 [ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF)
- [Silero VAD](https://github.com/snakers4/silero-vad)（经 faster-whisper 打包）、[PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch)

---

## English

**Realtime captions for whatever your PC is playing, rendered as a subtitle bar at the bottom of the screen. Runs entirely on your local NVIDIA GPU — no cloud, no cost.**
Built for Chinese first (Qwen3-ASR beats whisper-large-v3 by a wide margin on colloquial Mandarin); English and mixed zh/en work too.

- **~0.5 s latency** from end of utterance to on-screen text (measured on an RTX 3090)
- **No virtual audio cable**: captures the system output via WASAPI loopback
- **Subtitle-style overlay**: newest line large and white, previous line small and gray, fades out when nobody is speaking; drag to move, scroll to resize
- **Hotwords** (`hotwords.txt`) for names, teams and jargon
- ~3 GB VRAM, ~13 % GPU load

**Setup (Windows):** run `setup.bat` (venv + deps + ~2.8 GB model download), unzip a llama.cpp Windows CUDA release **newer than 2026-04-12** (Qwen3-ASR support, PR #19441) into `llama\`, then run `start.bat` (auto language) or `start.bat --lang en`.

**How it hits half a second:** llama.cpp decodes 6 s of audio in 0.2 s; a partial transcription is refreshed every 0.4 s while speech is ongoing, and when the end-of-sentence silence is detected the last partial is reused as the final result if no new speech arrived (90 % hit rate). Latency is therefore ≈ `--silence` (0.5 s) + one VAD window. VAD and segmentation run on the main thread; finals and partials run on two worker threads against a 2-slot llama-server.

Options: `--lang zh|en`, `--list` / `--device N`, `--console`, `--srt file`, `--font-size`, `--bottom`, `--alpha`, `--silence`, `--max-seg`, `--debug dir`. See the tables above (parameter names are the same).

MIT License.