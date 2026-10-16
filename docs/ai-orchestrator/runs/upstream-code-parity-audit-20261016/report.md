<!-- ai-orchestrator:checkpoint:v1:upstream-code-parity-audit-20261016 -->
# 原作者代码与 LocalLiveCaption 的剩余差异审计（2026-10-16）

**任务类型：只读审计；不修改生产代码，不合并任何分支。**

- 原作者：[`TerryKiddy/live-caption/live_caption.py`](https://github.com/TerryKiddy/live-caption/blob/794fea1fd50f34e55a6e1e1537aa1659af61fea1/live_caption.py)，固定 SHA `794fea1fd50f34e55a6e1e1537aa1659af61fea1`，约 847 行
- 用户仓库 `main`：`667392f8db2bce5d54abfbd76a9dd19a06f5fc11`
- 用户仓库未合并 PR #11 的代码：`3e331eddb9181b8710877ccf1a207aeda1d57859`；审计时 PR Head（含文档提交）`ddc7554cc0c919239524590a0a2302756aa5ae98`
- [目标 PR #11](https://github.com/A942199/LocalLiveCaption/pull/11)：Draft、尚未合并；本报告只评估源码，未授权实施修复或合并。
- 范围：ASR 采集/VAD、Qwen/llama、Partial/Final/切段、翻译衔接、悬浮窗、CLI 设置、CI 与测试完整性。
- 方法：分别读取原作者单文件和本仓库 Python、翻译子模块、工作流及既有测试；Windows 隔离工作树上使用 Git clone 读取上游 SHA，Python AST 列举函数方法，执行无写入的 CLI 解析复现；核对历史已执行的 FLEURS 真实语音测试报告。Windows 执行连接在审计后段断开，故未补跑整套 live GUI/ASR，新测试证据不得捏造。

## 高优先级：确定缺陷 / 已测准确率回退

### H1 启动器的默认参数误覆盖用户的 `--lang=...` / `--out=...`（确定，实际运行复现）

- 源码：[`live-caption-ja.pyw` 顶部第 46–52 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L46-L52) 用 `"--lang" not in sys.argv` 检查 **完整列表项**，遗漏 `--lang=auto` 语法；在原参数之后再追加 `["--lang","ja"]`，`argparse` 同名选项最后一次生效。`--out=custom.txt` 同样被默认 `--out transcript.txt` 覆盖。
- 实测：`--lang=auto` 变为 `['--lang=auto','--lang','ja',...]`，解析结果 `lang=ja`；`--out=custom.txt` 的最终输出路径变为项目默认 `transcript.txt`。
- 次生缺陷：[`main()` CLI 显式参数识别](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L2352-L2355) 会把 **自动追加的** `--lang` 当成用户显式指定；[`_apply_asr_config()`](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L2278-L2281) 因而跳过 GUI 的 `asr_language=auto` 保存设置。对日语识别配置真实性有直接影响。
- 方案：在解析层提供真正的 `default='ja'` 或保留 `user_cli_argv` 单独检测显式输入；不能把内部注入项当 CLI 用户意图。对 `--flag value`、`--flag=value`、保存 GUI 配置、console/gui 两模式补覆盖测试。

### H2 翻译层把所有 ASR Final 视为句末，`maxlen` 被误加句号（确定代码缺陷；效果量待实测）

- 原作者无独立中文翻译链路，Final 意味一个语音段，不一定是完整语义句。
- 本项目 [`translation/engine.py` 95–105 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/translation/engine.py#L95-L105)：对每次 `final(text)` 无标点就强行补 `。`，再追加到累计已完成文本；而 [`_run_asr_impl()` 2160–2169 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L2160-L2169) `reason='maxlen'` 也经过相同 `sink.final(text,start,end)` API，`reason` 未传给翻译器。
- 具体路径：某日语长句连续说话 16 秒触发 `maxlen` -> ASR Final 当前片段，无句末标点 -> 翻译器强加 `。`、立即划句；后续音频成为另一句。日语主 ASR 正确也会产生不自然的中文断句，并可能触发多次翻译。
- 方案：为 Final 增加兼容的 `reason / is_natural_endpoint` 元数据，而非仅凭 Final 判断语义句末；翻译调度对 maxlen 采用有界、可配置的连续句保留/上下文方式。不能为了合并中文而修改已经提交的日语 Final，也不能无上限延迟中文显示。设计后用真人长句、译文抖动和调用次数 A/B 验证。

### H3 原作者“静音时复用 Partial”在当前 Qwen GGUF 上有真实准确率代价（已实测）

- 原作者 [`run_asr()`](https://github.com/TerryKiddy/live-caption/blob/794fea1fd50f34e55a6e1e1537aa1659af61fea1/live_caption.py#L635-L755) 缓存覆盖有效语音长度的 Partial，在 `reason=silence` 时有条件复用；`maxlen` 始终完整重新识别。
- PR #11 [`_select_reusable_partial`](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L1784-L1793) 已按原作者复原；`main` 则始终做 Fresh Final。历史 FLEURS 日语 29.52s 实时输入：`max_seg=16`，`main` CER 3.738%，PR #11 CER 5.607%；正好在自然静音 Final 复用了 Partial、减少 1 次 ASR 调用；`maxlen` 仍旧是新识别。
- 48 条长录音 *关闭 Partial* 的 192 次 FLEURS A/B 两版完全一致（16s CER 6.623%，6s CER 10.316%），这仅证明没有 Partial 的硬切处理等价，**不能**证明真实实时复用更准确。
- 方案：明确性能/准确度取舍。以准确度优先可将“静音 Partial 复用”设为可选、默认 Fresh Final；希望严格保持原作者行为则保留，但不得声称提高 CER。需扩大打开 Partial 的真实日语音频对照集和量化延迟节省。

### H4 GitHub CI 没有执行 Python 应用测试（确定，工作流和结果证据）

- [唯一现有 `.github/workflows/dotnet-build.yml`](https://github.com/A942199/LocalLiveCaption/blob/667392f8db2bce5d54abfbd76a9dd19a06f5fc11/.github/workflows/dotnet-build.yml)：工作流侧重 `src/**` / `Properties/**` 变动和 `dotnet restore/publish/test`；没有 Python venv、`unittest discover`、单文件 bundle 一致性，也没有日语真实语音 A/B。
- PR #11 审计时 GitHub Actions `Build and Test` conclusion 为 `skipped`，不能作为通过。
- 方案：新增 Windows Python workflow：依赖安装、`python -m unittest discover -s tests -p 'test_*.py'`、`--help`、单文件内嵌翻译模块等价测试；真人 Qwen GPU 基准应留独立本地硬件闸门/留存 JSON。保留现有 .NET 工作流服务 .NET 路线，勿混淆它与 Python 部署验收。

## 中优先级：静态确认的风险 / 需要定向验证

### M1 实时音频满队列会主动丢掉最旧块，导致不可恢复漏音（确定代码行为，实际发生频率未知）

- [`_AudioChunkQueue.put()` 135–155 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L135-L155) 固定 128 块，满时丢最旧（约 0.1s callback 的块则约 12.8s 缓冲；实际块长可变）；[`_run_asr_impl()` 2099–2114 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L2099-L2114) 检测序号断档会 Final 当前段并重置 VAD。原作者无界 Queue 不会以此策略丢块，但存在内存失控风险，不能直接照抄。
- 建议加入 `dropped_chunks`、`dropped_samples`、`queue_latency_p95`、Final backlog 等可见指标；实测长时间音频+高 GPU 负载时的语音漏失次数；仍采用有界队列或分级负载控制，不恢复无界队列。

### M2 已开始的 HTTP ASR 请求无法及时取消，程序退出与 Final 延迟可能被服务响应拖住（静态风险）

- `LlamaBackend.transcribe()` [urlopen(timeout=60)](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L1620-L1633)，Final 可重试；[`_run_asr_impl()` 2204–2208 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L2204-L2208) 与 [GUI 关闭时 `th.join()`](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L2399-L2404) 都可能等待在运行的 Final。现有可靠性设计为了避免漏 Final 而等待；不是直接改成超时丢字幕。建议增加 stop/cancel 协议及明确关停 UI 状态，同时区分“正常等待完成”和“服务挂死”。

### M3 翻译和 UI 缺少展示时最后一层版本校验（静态风险，尚未复现闪烁）

- `translation/engine.py` 已有 output_version 和任务取消，但 [`OverlaySink._poll()` 1174–1179 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L1174-L1179) 接到 `('t',(source,translated))` 无条件更新文本，不检查该译文是否仍对应当前显示的日语、句段或翻译配置代号。若旧翻译已被入队后 UI 切换到新 Final，存在短暂错配/闪烁路径；现有引擎在上游已有一定过滤，不能宣称该问题已在实际机器复现。
- 方案：在显示队列中保留 segment_id / translation-generation 进行 *显示时* 最后校验，单元测试故意乱序投递 `p/f/t`，确认不覆盖新段；避免简单字符串相似性误删真实重复句。

### M4 VAD 使用 `faster_whisper.vad` 的内部 ONNX session 结构而非稳定公开 API（升级风险）

- [`_StreamingSileroVAD` 160–211 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L160-L211) 直接访问 `get_vad_model().session` 和 `input/h/c` ONNX 张量字段。
- 这是有意识且有价值的改造：保留 VAD 流式隐藏状态，避免上游每一小块重建识别状态；真实 FLEURS 用现有依赖已经跑通，**不应机械恢复原作者的 VAD**。但依赖未锁定版本（[`requirements.txt`](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/requirements.txt)），将来 `faster-whisper` 内部模型变动会破坏它。
- 方案：锁定已验证依赖版本、模型 session 输入/输出契约测试；如果未来升级再迁移公开流式 VAD API。

### M5 GUI 事件队列无上限且 `_poll()` 尝试一次抽干（高刷新负载下 UI 卡顿风险）

- [`OverlaySink.__init__` 337 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L337) 创建无界队列；[`_poll()` 1163–1204 行](https://github.com/A942199/LocalLiveCaption/blob/3e331eddb9181b8710877ccf1a207aeda1d57859/live-caption-ja.pyw#L1163-L1204) 一直 `get_nowait` 直到空。上游也有类似队列，但移植后多了频繁的翻译事件和设置/重渲染。
- 建议采用有限每帧处理预算和 Partial 事件合并（不能合并 Final / 关闭事件），对高频部分字幕与网络译文压测 Tkinter 主线程响应；尚未证实实机卡顿。

## 发现的有意差异：目前不建议恢复

| 源码差异 | 审计结论 |
|---|---|
| 原作者提供 `QwenBackend` (Transformers) 和 `LlamaBackend`；你的 Python 工具只保留 llama.cpp GGUF | 功能面缩小，但符合当前 RTX 3090 + GGUF 的部署选择。**除非明确需要 Transformers 回退，不要仅为源码一致而引入额外大型依赖。** |
| 原作者每窗口调用 `get_speech_timestamps`，目前使用持久化 Silero VAD 状态和 preroll | 目前是合理的工程增强，应保留、做升级兼容测试 |
| 原作者 Final Queue 无上限，当前有 16 Job 和音频 128 Chunk 上限 | 防止无限内存增长，不能直接恢复无界队列 |
| 原作者简单退出可能漏末段，当前 drain Final、保留短尾音频 | 有效可靠性增强，应保留 |
| 原作者没有 Google Translate、取消 Token、中文覆盖层和历史功能 | 必须作为独立翻译子系统评估，不能认为原作者缺少而删除 |
| `_clean()` 对日语假阳性“重复文字”更保守 | 意图合理，避免把「はいはい」等真实口语删除 |
| 原作者 `--backend transformers` 允许 HF Qwen，当前只提供 GGUF | 可列为低优先级兼容请求而不是 P0 漏迁 |

## 建议实施顺序（本次无代码修改）

1. **先修 H1**，用 CLI 等价输入回归和 GUI 保存配置实测验证，避免配置默默无效。
2. **再修 H2**：给翻译端传递切段原因，对 maxlen 片段制定受控译文策略；保留日语 Final 不变，特别测试边界对齐、翻译重复及字幕闪烁。
3. **明确 H3 策略**：严格保留原作者 Conditional Partial Reuse，则承认质量取舍；若用户优先准确度，可提供默认 Fresh Final 的选项并通过真实日语动态测量。
4. **建立 H4 Python CI**，禁止把 `Skipped` 当作通过，单文件与翻译源码同步测试必须运行。
5. 补 M1/M2/M3 性能/取消/乱序故障注入；锁定 VAD 依赖、压测 Tkinter（M4/M5）。

## 证据与边界

- 源码审计：原作者及 Fork/PR11 固定提交，不是当前正在运行的用户 .exe；该应用 Python 版本不是 .NET/WPF 本体。
- 唯一此次新增的运行复现：`runpy.run_path` 仿真启动器的参数拼接，用 `argparse` 验证 `--lang=auto` 被 `ja` 覆盖、`--out=custom.txt` 被默认路径覆盖。**该复现无需 GPU 或 WASAPI。**
- 实际日本语 CER 数据来自 [前一轮 PR #11 验证报告](https://github.com/A942199/LocalLiveCaption/blob/agent/asr-maxseg-boundary-20261016/docs/ai-orchestrator/runs/asr-maxseg-upstream-restore-20261016/report.md)，测试集与条件如其中所述；不是此次新跑的 GPU 基准。
- 本次未修改任何生产代码，未合并，未验证实体 WASAPI、Tkinter 全套 UI 或在线翻译网络服务。高优先级 H2 和中优先级 M2/M3/M4/M5 的实际量化影响仍需专门测试。
- GitHub PR #11 审计当时仍 Draft/Unmerged，关联 Actions 结果 `Skipped`。

**审计决定：报告已完成，生产发布验收不通过 / 不得凭此自动合并。**
