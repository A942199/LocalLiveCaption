# LocalLiveCaption — Python 日语实时字幕

Windows 10/11 上的本地 **日语语音识别 + 中文翻译** 悬浮字幕程序。
当前项目只保留 Python 主程序；原 C#/WPF 独立程序不参与运行或构建。

## 快速开始

**环境：** Windows 10/11 x64、NVIDIA GPU 与驱动、Python 3.11+（安装脚本可协助安装）。

1. 下载项目，在目录中运行 setup.bat。
2. 安装脚本创建 .venv/，安装依赖，并下载本地 Qwen3-ASR GGUF 模型及兼容的 llama.cpp CUDA 服务端。
3. 安装完成后运行 live-caption-ja.pyw，在窗口 ⚙ 中调整识别和翻译设置。

首次安装需要网络连接，模型与 CUDA 运行库体积较大。日语 ASR 在本地运行；中文翻译根据所选服务可能向外部 API 发送日语文本。

## 现有 Windows 电脑快速启动（独立 .venv）

项目首次设置独立的 Python 虚拟环境：

    py -3.11 -m venv .venv
    $env:PYTHONUTF8 = '1'
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt

复制 .env.example 为 .env，在 .env 中填写已下载的三个模型 / llama.cpp 文件路径（当前开发机已配置好），随后双击 start.cmd。
启动器 start.ps1 会为子进程设置 VIRTUAL_ENV、PATH 和 PYTHONUTF8，而不会永久修改系统环境变量：

    .\start.cmd -Check     # 核查 .venv、包、模型和启动入口
    .\start.cmd -Test      # 执行全部 Python 回归测试
    .\start.cmd            # 启动真实字幕 GUI

如需强制在当前 PowerShell 会话中激活自己的环境，可以执行：

    .\.venv\Scripts\Activate.ps1

注意：setup.bat 是针对新电脑全新安装的脚本；它会下载模型和 CUDA 推理服务。已有模型时请先使用 .env + start.cmd，避免重复下载约 2.8 GB 模型文件。

## 主要功能

- WASAPI loopback 捕获 Windows 系统音频，Silero VAD 检测语音，Qwen3-ASR + llama.cpp GPU 识别日语。
- 快速 Partial、准确 Final、稳定前缀和智能断句，降低字幕抖动。
- 稳定 Partial 译文预览、跨 VAD Final 合句、避免过期译文覆盖。
- 11 种翻译接口，包括 Google、Google2、DeepL、OpenAI、Ollama 等。
- Tkinter 日中双语悬浮字幕，可调整颜色、字号、背景、自动隐藏和字幕历史。
- 翻译设置热更新、SQLite 历史、搜索与 CSV 导出。

## 项目结构

| 路径 | 作用 |
| --- | --- |
| live-caption-ja.pyw | **最终启动入口**，包含 ASR、VAD、字幕 UI 和内嵌翻译子系统 |
| translation/ | **可维护翻译源码**和技术说明；修改这里后须同步 .pyw 内嵌副本 |
| tests/ | Python 回归、字幕稳定性、翻译逻辑和行为契约测试 |
| setup.bat、requirements.txt | Windows 安装与依赖 |
| .github/workflows/python-tests.yml | Windows Python 自动测试 |
| docs/ai-orchestrator/ | 开发与验收记录 |

**不可因翻译代码也内嵌于 .pyw 而删除 translation/：** 模块源码用于维护、差分审计和回归测试；运行时则不需要单独安装这些模块。

详细使用和安装说明：[README_PYTHON.md](README_PYTHON.md)。
翻译配置：[translation/README.md](translation/README.md)。

## 测试

在项目根目录安装依赖后运行：

    .venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py" -q

真实窗口测试需在交互式 Windows 桌面运行；GitHub CI 对其中两项仅在无桌面 Runner 中跳过。

## 源代码与许可证

ASR 主程序改编自 [TerryKiddy/live-caption](https://github.com/TerryKiddy/live-caption)。
翻译子系统参考并移植自 [SakiRinn/LiveCaptions-Translator](https://github.com/SakiRinn/LiveCaptions-Translator)。
保留根目录 [LICENSE](LICENSE) 和 [translation/LICENSE-UPSTREAM](translation/LICENSE-UPSTREAM)。
旧 C#/WPF 源文件、截图与 .NET 构建流程已从当前 Python 工作树移除，但可从清理前 Git 提交 7483b349 恢复。
