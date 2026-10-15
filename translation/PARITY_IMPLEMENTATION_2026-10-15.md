# 翻译子系统 C# → Python 行为对齐实施报告

日期：2026-10-15
原版：`third_party/LiveCaptions-Translator`，`SakiRinn/LiveCaptions-Translator` commit `a6fee12757b15edbeef7c60f8895e0be694801e1`
目标：`live-caption-ja.pyw` 内嵌翻译模块；`translation/{api,history,engine,gui,text,__init__}.py` 为一致的可读源码。

> 本报告是对 `PARITY_AUDIT_2026-10-15.md` 的修复状态更新。原审计报告保留作前后对照，不代表当前实现仍有全部旧缺口。

## 已修复 / 对齐

| 原审计问题 | 完成情况 | 证据 |
|---|---|---|
| F01 独立字幕上下文队列 | 已在翻译模块建立最大 10 条的内存队列；记录当前译文前先将旧记录纳入前文；按无句末标点条件清理；不从磁盘历史恢复旧会话的对话上下文 | `translation/engine.py`, `history.py`，上游 `Caption.cs` / `Translator.cs` |
| F02 OpenAI 400/422 兼容回退 | 已实现 8 种请求体（integrated/Aliyun/Anthropic/Ollama/OpenRouter/OpenAI/XAI/base），仅在 400/422 继续尝试，并记住上次成功兼容索引 | `translation/api.py`，专项 fake HTTP 错误契约测试 |
| F03 LLM 请求体 / 类型 | 已对齐 base `model/messages/temperature/max_tokens/stream/keep_alive`，按上游模型增加禁用 thinking / reasoning 字段；GUI 将 temperature、keep_alive 保存为数值类型 | `translation/api.py`, `gui.py` |
| F04 DeepL 语言映射 | DeepL、Youdao、Baidu、MTranServer、LibreTranslate 的五组目标语言，共 65 个条目与 C# `SupportedLanguages` 字面一致 | C# 动态源码表差分测试 |
| F05 ASR 片段输入差异 | **仅在翻译输入适配层**累计 Qwen3-ASR 最终片段，并为无标点的 final 补结束符；partial 与 final 现具有可用于原版 `SyncLoop` 的连续全文 | `translation/engine.py`；未改 ASR/VAD/音频识别代码 |
| F06 SQLite 上游数据库不兼容 | 可在给定上游数据库路径时，将 C# `TranslationHistory` 旧记录保留原表并一次性导入 Python 结构；处理 Unix 时间戳；不会重复导入 | `translation/history.py`、迁移幂等测试 |
| F07 历史查询/管理缺失 | 已提供关键词搜索、分页与总页数、清空、**不受 GUI 当前页限制的 CSV 全量导出** | `translation/history.py`, `gui.py`；1010 行全量导出测试 |
| F08 设置/Profile/显示 | GUI 每引擎多配置档案和索引、历史 UI，保存后热更新在运行中的翻译对象；`display_sentences` 已用于最近译文叠加展示；保留现有 Tkinter 悬浮窗 | `translation/gui.py`, `engine.py` |
| F10 Unicode 文本长度 | 相似度与覆盖检测按 C# `string.Length` 的 UTF-16 code units 计算，不再把 emoji 按一个字符误计 | `translation/text.py`；emoji 契约测试 |

另：按照原版 `TextUtil.NormalizeUrl` 规则处理配置 URL；超时处理会给出接近上游的错误文本。

## 测试 / 运行验证

- **89/89 全回归通过**：`.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v`，2026-10-15，约 23.8 秒。
- 上游契约测试：`tests/test_translation_upstream_contract.py`，覆盖引擎名称、请求 schema、八级 fallback 与状态持久、所有非 LLM API 目标语言表、模拟超时、UTF-16、上下文队列、SQLite 导入/幂等/全量导出和运行中设置切换。
- 单文件测试：`tests/test_single_pyw_bundle.py`，校验六段内嵌模块与 `translation/` 完全一致，并将 `.pyw` 复制到**没有 translation 文件夹**的独立路径测试加载与命令行帮助。
- GUI smoke：用 Windows Tkinter 创建、刷新并关闭翻译设置窗口与历史窗口，返回 `GUI_SETTINGS_AND_HISTORY_OPEN_OK`。
- ASR 前缀在多次翻译模块重新打包后 SHA256 保持 `7e0667b255cf824e07acfd2da164b4841518c1df42b96bd0ce69d90d632fa5a7`；翻译模块内嵌后程序约 164 KB。

## 严格 1:1 的残余边界（必须诚实说明）

1. **上游依赖 Windows Live Captions 和 WPF 窗口；本项目依赖原有 Qwen3-ASR 与 Tkinter。** 尽管翻译输入、任务调度和常见显示结果已对齐，这两套外壳/控件的生命周期、字段更新与视觉行为不能在“不修改 ASR 代码”的约束下逐条等价，也不应把原版的系统听写替换进来。
2. **HTTP 真取消的运行时差异。** C# `CancellationToken` 可尝试中断 `HttpClient` 请求。Python `urllib` 正在进行中的阻塞请求无法被立即安全中断；程序只能标记取消并丢弃陈旧响应，最长约受 8 秒请求超时控制。**不宣称此项 1:1。**
3. **尚未进行真实外部 API 的 live 网络端到端测试。** 11 个供应商的离线契约和模拟响应通过，并不证明当前所有服务凭据、配额、网络条件或其未来协议都正确。
4. **没有将 C# 原版编译并做两进程同时重放真实音频/字幕的全量动态差分。** 多个跨语言分支通过源码表/协议夹具对照，但未达到形式化证明的级别。
5. 字幕合并的用户体验经过适配，尤其对没有标点的 Qwen3-ASR 最终结果主动增加句末句点，这是为了在 ASR 输入模式不同的情况下保持上游“完整句立即翻译”的意图，而**非逐字符相同输入**。

因此准确结论为：**已修复前次报告的主要 Python 翻译业务缺口，离线回归通过，但不能声称与原 C# 完成绝对 100% 的运行时行为等价。**

## 工作区约束

- 原有 ASR/VAD/llama.cpp 代码未编辑；仅翻译模块文件、主 `.pyw` 的翻译内嵌区、翻译相关测试及说明发生变更。
- 其他已有未提交文件均保留。没有 Git reset/clean/commit/push。
