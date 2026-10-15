# LiveCaptions-Translator → live-caption-ja.pyw 一比一移植审计

> 审计日期：2026-10-15。只读业务审计；仅新增本报告，未修改 ASR、翻译业务代码、设置或数据库。未运行真实第三方翻译 API。

## 1. 基准与范围

- 上游：`SakiRinn/LiveCaptions-Translator` 的 `master`，`git ls-remote origin refs/heads/master` 实测 SHA **a6fee12757b15edbeef7c60f8895e0be694801e1**；本地克隆路径 `third_party/LiveCaptions-Translator` 处于相同 SHA。
- 本地：`live-caption-ja.pyw`（翻译模块已嵌入为 6 段 Python 代码）。`translation/` 是完全相同的源码副本，方便逐行比对。
- 目标：重点判断上游**翻译子系统**的逻辑与功能是否 1:1，而不是把 Windows LiveCaptions/WPF/系统识别也移植到 Python（用户要求保留现有 Qwen3-ASR）。
- 项目 Skill：`code-review-and-quality`、`realtime-caption-stability`、`unittest-skill`、`systematic-debugging`；均已在本次 WebCodex Workflow Session 加载。

## 2. 结论

**不满足一比一全量移植。** 当前版本属于有用的 Python 功能性适配/部分迁移，不是上游翻译系统的严格等价实现。

已经匹配：11 个翻译引擎名称；Google/Google2 主要路径；部分传统翻译接口的签名与响应解析；原文基础正则、主要阈值与 25/40/40/720 毫秒默认调度时间；上游默认 LLM Prompt。

未匹配或不完整：LLM 请求体与自动 fallback、部分目标语言代码、上下文生命周期、SQLite 兼容与日志查询/清理、UI 展示分路、翻译配置多 Profile 与即时生效、部分取消/并发/异常规则、上游 Windows LiveCaptions 全文输入语义。

## 3. 翻译接口覆盖矩阵

| API | 名称/基本路径 | 实际一比一？ | 证据与差异 |
| --- | --- | --- | --- |
| Google | 已实现 | **接近，非严格** | endpoint/参数/`[0][0]` 解析一致；Python urllib 与 .NET HttpClient 取消、超时行为不同 |
| Google2 | 已实现 | **接近，非严格** | extension endpoint、客户端 key、x-referer、响应字段匹配；异常和取消差异 |
| Ollama | 已实现 | **否** | 原版 `OllamaRequestData` 包含 `temperature`,`max_tokens=128`,`keep_alive`,`think=false`；Python 改用 `options.temperature`，漏 `max_tokens/think`，GUI 可能传字符串 `keep_alive` |
| OpenAI | 已实现 | **否** | 原版 HTTP 400/422 会依次尝试 8 种请求体（`LLMRequestDataFactory`）；Python 一次请求后直接报错；缺 `max_tokens`、`keep_alive`、reasoning 限制 |
| LMStudio | 已实现 | **部分** | `/api/v1/chat`、prompt/input/output 基本路径匹配；GUI 保存 `temperature` 为字符串，缺字段校验 |
| DeepL | 已实现 | **否** | JSON 和认证基本路径对应，但上游 13 种目标语言表只在 Python 明确映射了 4 种。例：fr-FR 上游 `FR`，Python `fr-FR`；es-ES 上游 `ES`，Python `es-ES` |
| OpenRouter | 已实现 | **否** | 基本 chat/completions 对应，但漏原版 `reasoning.exclude=true`、`reasoning.enabled=false`、`max_tokens=128`、`keep_alive` 等字段 |
| Youdao | 已实现 | **基本相近** | MD5、salt、表单、主要语言代码和结果字段相近；GUI/API 配置类型、异常语义仍不同 |
| MTranServer | 已实现 | **基本相近** | `Bearer`、from/to/text、result 对应；请求参数边界和取消逻辑未严格等价 |
| Baidu | 已实现 | **基本相近** | MD5、salt、form、主要语言码和 dst 对应；取消与错误细节未严格等价 |
| LibreTranslate | 已实现 | **基本相近** | POST JSON 和 `translatedText` 对应；取消与错误处理不同 |

## 4. 关键发现（按影响程度）

### [高] F01 上下文不是原版的独立队列

- C# `src/Translator.cs:88-89, 323-343` 与 `src/models/Caption.cs:22-31, 95-137`：内存队列 `Contexts`（上限 10）专门管理可用的**前文**；新窗口/无 EOS 时清空；只有新句非覆盖时才向队列压入**上条**历史。
- Python `translation/history.py:66-83`、`translation/engine.py:149-161`：直接从持久 SQLite 最近几条读取，没有等价的内存队列和清空逻辑；重启后依旧把旧会话字幕当成上下文；失败/覆盖会导致条数与选取时间不同。
- 实测：新建 `HistoryStore`，写入一条，再实例化一次，`contexts()` 依旧返回 1 条；C# 新 `Caption` 队列初始为空。使用上下文翻译时可串入不相关旧话题。

### [高] F02 OpenAI 兼容服务 fallback 被遗漏

- C# `src/apis/TranslateAPI.cs:85-107` 与 `src/apis/LLMRequestDataFactory.cs:9-21`：400/422 循环尝试 `integrated/Aliyun/Anthropic/Ollama/OpenRouter/OpenAI/XAI/base` 八种数据体。
- Python `translation/api.py:190-218`：固定一个数据体，没有状态机或多种请求格式。
- 实测：注入模拟 HTTP 400，Python 调用次数 1 且返回 `[ERROR]`；C# 有显式重试分支。兼容服务器尤其受影响。

### [高] F03 LLM 请求字段不一致（推理模式、tokens、数据类型）

- C# `src/models/RequestData.cs:5-84`：基础字段含 `max_tokens=128`、`stream=false`、`keep_alive=600`，各平台另附 `think=false`、`reasoning.exclude=true`、`reasoning.enabled=false` 等。
- Python `translation/api.py:190-218`：OpenAI/OpenRouter 没有上述字段；Ollama 请求改成 `options.temperature`，没有原版 `think=false` / `max_tokens`。
- Python `translation/gui.py:34, 37-39` 保存全部 API 字段为 `tk.StringVar` 字符串，包括 `temperature`、`keep_alive`；`translation/api.py:190-211` 不做数值转换。运行 mock：`temperature='0.5'` 为 `str`、Ollama `keep_alive='600'` 为 `str`。可能导致 API 422/400 或忽略参数。

### [高] F04 DeepL 语言代码不全

- C# `src/models/TranslateAPIConfig.cs:199-217`：13 种明确映射。
- Python `translation/api.py:29-36,114-118`：只有 zh-CN、zh-TW、ja-JP、ko-KR。
- 实测：fr-FR、es-ES、pt-BR 三种 Python 返回原 UI 语言标识而非上游要求代码；若用户切换这些目标语言，翻译请求可能失败。

### [高] F05 翻译输入序列语义不完全相同

- C# `src/Translator.cs:47-132` 从 Windows Live Captions 连续读取**全文**，截取当前句及显示历史，按 EOS 自动纠正。
- 当前 `.pyw` ASR 部分通过 `sink.partial(display)` 与 `sink.final(text, ...)` 提交**当前语音段文本**；Python `translation/engine.py:77-111` 仅存一个 `observed_text`；缺少完全等价的全文缓冲/会话边界。
- 即使 25/40/40ms 等常数一致，真实字幕断句、上下文续接、旧句覆盖的输入行为也并非原版 1:1。为保护用户现有 ASR，可只在翻译输入适配层补齐。

### [中] F06 原版 SQLite 历史库不兼容

- C# `src/utils/HistoryLogger.cs:13,27-35,68-84`：`translation_history.db`，表 `TranslationHistory`，列名 PascalCase，Unix UTC 秒时间戳。
- Python `translation/history.py:20-57`：`translation-history.sqlite3`，表 `translation_history`，列名 snake_case，ISO 本地时间戳。
- 实测：用原版 C# DDL 写 1 行，再用 Python `HistoryStore` 打开同一个文件，`recent()` 返回 **0**；它创建了不同表而非直接读取原记录。

### [中] F07 历史管理功能缺失

- C# `src/utils/HistoryLogger.cs:86-155,215-246,249-275`：搜索、分页/总页数、清空记录、全部 CSV 导出、旧时间戳迁移。
- Python `translation/history.py`：只有 recent、log、last、contexts；`translation/gui.py:98-142`：显示最近 1000 条，CSV 只导出**当前加载的最近 1000 条**；没有搜索、分页、清空、迁移。
- 实测源码方法 `HistoryStore.clear` / `HistoryStore.search` 不存在；超 1000 条时 CSV 不全。

### [中] F08 显示/设置/状态功能不等价

- C# `src/Translator.cs:107-124,193-231`：独立维护 `OverlayOriginalCaption`、`DisplayOriginalCaption`、`TranslatedCaption`、`OverlayNoticePrefix`、`OverlayCurrentTranslation`、`DisplayTranslatedCaption`；对主窗口显示做 220 UTF-8 字节缩短，包含延迟前缀等。
- Python `translation/engine.py:188-208`：直接向现有 Tkinter overlay 发送 source/translated，没有严格相同的通知前缀、双窗口截断及历史翻译合成流程；`display_sentences` 在同步和展示时未使用（设置项实际上不控制这部分）。
- C# 运行中可切换 `LogOnlyFlag`；Python GUI 的 `log_only`、API 参数与上下文等设置需要**重启**；运行时切换函数存在，但没有等价的主界面开关。
- C# `src/models/Setting.cs:139-162,193-220`：每个 API 有 `Configs` 数组与 `ConfigIndices`，可选不同 Profile；Python 一个引擎仅一份配置 dict。

### [中] F09 异步取消及竞争规则不一致

- C# `TranslationTaskQueue.cs:17-49` 使用 Task/CancellationToken，处理完成后的 output/log/context 行为；Python `translation/engine.py:135-186` 使用 daemon Thread + 布尔标记取消结果，无法真正停止 HTTP 请求。
- C# 被先前取消的任务仍可能进入完成延续（参见 IndexOf=-1 时的处理）；Python 会直接丢弃。后者更安全，但不是逐条同义。
- 翻译仍可能占用网络连接/线程直到 timeout，UI 展示吞吐和分段的顺序也不同。

### [低/边界] F10 文本辅助函数无法宣称逐 Unicode 等价

- C# 使用 .NET UTF-16 字符串的 Length/IndexOf、按 UTF-8 字节长度断句；Python 使用 Unicode code points。非 BMP 字符（如 emoji）、某些 Unicode 正则处理和空段拆句边界可能产生差异，需专门差分测试。

## 5. 实际测试证据

| 测试 | 实测结果 | 能证明什么 |
| --- | --- | --- |
| `.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v` | **72/72 通过**（约 19.4 秒） | 当前 Python 自有 ASR/翻译回归测试没有失败；**不能证明跨语言 1:1** |
| 比较上游 11 个引擎标识、4 个 LLM 名称、6 个默认时间/阈值、提示词开头、Google/Google2 端点 | **6/6 通过** | 名称与关键默认常数、核心路径一致 |
| Python 通过 `runpy.run_path('live-caption-ja.pyw',...)` 实际加载模块后的专项差异注入 | **14 个刻意选定的差异场景，14 个不匹配** | 各场景的具体缺陷有直接复现，不表示总功能通过率 0% |
| 单文件源码相等、无 `translation/` 隔离运行（此前会话） | **2/2 通过** | 内嵌代码与本地 Python 备份一致，且 `.pyw` 不依赖外部 Python 源码文件夹；**未证明它等价于 C#** |
| `git ls-remote origin refs/heads/master` | **a6fee127…** | 本地上游比较基准与远端目前的 master 一致 |

**测试限制**：本地尚无 .NET SDK，未启动编译后的 C# 原版来做自动化同输入同输出的动态差分；未进行真实 Google/DeepL/OpenAI 等联网请求、GPU 音频端到端演练，也未执行 Windows Live Captions/WPF 交互验收。上文运行差异用的是读取到的 C# 源码分支加 Python 运行时可重复的 Mock/SQLite 检查。

## 6. 建议的修复顺序（本次未修改）

1. **P0：先建立跨语言契约样例**：给 11 种引擎补齐请求体、响应、400/422 fallback、超时、错误文本；保证默认 Google 不回退。
2. **P1：修复配置类型和 DeepL 语言表**：数值型参数校验/转换，完整映射 13 种语言；补多 Profile/ConfigIndices 才可宣称配置全量。
3. **P1：重建独立字幕 `Contexts` 队列**：与数据库日志分开管理，按原版 AddContexts/ClearContexts 的时机维护，结合 Python ASR 的每段输入设计翻译侧全文累计适配器。
4. **P1：历史库结构与迁移**：兼容 `TranslationHistory` / Unix 时间戳，实现搜索、分页、清空和真正全量 CSV。
5. **P2：译文显示与运行中开关**：按照字幕原文、译文、提示前缀、历史和截断条件逐项验收；`display_sentences`、`log_only` 即时状态生效。
6. **P2：取消和日志顺序**：明确是否保留原版存在的竞态副作用，默认应以“不允许旧结果覆盖新结果”为安全不变式。
7. **P2：引入 C# 编译/运行参照与相同 fixture 差分**，尤其测试 Unicode、缩写、换行、句末标点、上下文覆盖与多个并行任务的边界。

## 7. Git/文件变更

本次仅新建这一份 Markdown 审计报告；业务代码未改、未执行 Git commit/push/reset/clean，历史用户工作区未提交修改均保留。
