# Python 翻译子系统

对照 [SakiRinn/LiveCaptions-Translator](https://github.com/SakiRinn/LiveCaptions-Translator) `master` 提交 `a6fee12757b15edbeef7c60f8895e0be694801e1` 实现翻译功能。识别、VAD、音频采集、llama.cpp 不在本包中。

**单文件运行说明（2026-10-15）：** 本目录的 6 个 Python 源模块已逐字内嵌在根目录的 `live-caption-ja.pyw` 文件末尾。运行时由 `.pyw` 自行加载，不需要携带 `translation/` 文件夹。本目录保留的是便于审查的源码备份；若日后修改这里的 Python 代码，必须同步更新 `.pyw` 中的内嵌副本并运行单文件一致性测试。

## 使用

在字幕窗口点 **⚙ → 翻译设置** 配置引擎和语言，**⚙ → 译文历史** 浏览或导出翻译历史。翻译设置保存后即时生效；默认 Google / zh-CN。支持各引擎多个配置档案。翻译会将原文发送到选定的翻译服务；联网/付费条件取决于所选引擎。

支持引擎：Google、Google2、OpenAI、OpenRouter、Ollama、LMStudio、DeepL、Youdao、MTranServer、Baidu、LibreTranslate。

也可在项目根目录创建 `translation-settings.json`（已被 `.gitignore` 排除）：

```json
{
  "engine": "Google",
  "target": "zh-CN",
  "context_aware": false,
  "num_contexts": 2,
  "display_sentences": 1,
  "timeout": 8.0,
  "log_only": false,
  "configs": {
    "OpenAI": [{
      "api_url": "https://YOUR_ENDPOINT/v1/chat/completions",
      "api_key": "YOUR_KEY",
      "model_name": "YOUR_MODEL",
      "temperature": 1.0
    }]
  },
  "config_indices": {"OpenAI": 0}
}
```

翻译历史位于 `translation-history.sqlite3`（不提交 Git），支持相似原文覆盖、上下文队列、历史全文搜索、分页、清空和完整 CSV 导出，并支持导入上游格式的历史库。**翻译服务密钥存储于本机 JSON 明文文件**，请勿分享该文件。

## 文件与上游对应关系

- `text.py`: `TextUtil.cs` / `RegexPatterns.cs` 原文预处理、拆句与相似度。
- `engine.py`: `Translator.cs` / `TranslationTaskQueue.cs` 触发、任务与结果显示。
- `api.py`: `TranslateAPI.cs` 的 11 种引擎、全部目标语言映射以及 OpenAI 400/422 八级回退。
- `history.py`: SQLite 翻译历史、相似度覆盖和上下文。
- `gui.py`: 翻译设置、历史浏览和 CSV 导出（Tkinter）。

## 限制与测试

离线测试：`.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v`。

Python 与上游 C# 是不同的运行时。`urllib` 无法像 .NET CancellationToken 那样可靠中断正在进行的 HTTP 请求，取消后改为丢弃返回结果；这属于已知实现差异。外部 API 真正联网、密钥授权、GPU 音频和与 C# 原版逐条差分仍需实测，模拟测试通过不等于接口上线验证。Windows Live Captions/WPF 的外壳功能不属于本地 Python 翻译模块。
