# LocalLiveCaption 商业级实时字幕优化方案

## 1. 目标

在保留当前 `Qwen3-ASR-1.7B Q8_0 + llama.cpp + RTX 3090` 架构的前提下，将 LocalLiveCaption 从“周期性整段重识别 + VAD 静音切句”升级为更接近商业实时字幕系统的低延迟、高稳定性架构。

核心目标：

- 降低字幕抖动和整句反复变化。
- 提高 partial 字幕刷新速度。
- 保持 final 字幕的准确率。
- 将“文字已经稳定”和“说话人真正说完一句”分离。
- 减少重复计算。
- 降低 Google Translate 对不稳定 partial 的重复请求。
- 为后续真正的 stateful streaming ASR 预留架构。

---

## 2. 当前推荐基础配置

保留当前已经通过 BTSJ 实测得到的高精度配置：

```text
ASR model              = Qwen3-ASR-1.7B Q8_0
backend                = llama.cpp
GPU                    = RTX 3090

context_enabled        = false
context_history        = 1
context_chars          = 80

llama_ctx              = 4096
slots                  = 2
max_tokens             = 256
temperature            = 0.0

partial_every          = 0.6
silence                = 0.80

vad_win                = 0.20
vad_threshold          = 0.10
vad_min_speech_ms      = 100
vad_min_silence_ms     = 100
vad_speech_pad_ms      = 200

min_seg                = 1.2
max_seg                = 12.0
max_gain               = 60.0
```

完整 BTSJ 331 + 332 + 333 测试结果：

```text
Audio                  = 3646.54 sec
CER                    = 39.660%
Strict char accuracy   = 60.340%
RTF                    = 0.0257
```

当前 RTX 3090 推理性能已经有非常大的余量，因此下一阶段重点不应继续放在单纯降低模型推理耗时，而应优化实时字幕状态管理。

---

## 3. 推荐目标架构

```text
Windows 系统声音
        │
        ▼
0.1 秒 Capture Chunk
        │
        ▼
Audio Frontend
重采样 / Dynamic Gain
        │
        ▼
Silero VAD
        │
        ├───────────────────────────────┐
        │                               │
        ▼                               ▼
Fast Partial Path               Accurate Final Path
低延迟快速路径                    高精度最终路径
        │                               │
每 0.3~0.5 秒                     Endpoint 后触发
        │                               │
Rolling Audio Window              完整 Utterance
        │                               │
Qwen3-ASR                         Qwen3-ASR
        │                               │
Partial Hypothesis                Final Transcript
        │                               │
Stable Prefix                     Second-pass Refine
        └───────────────┬───────────────┘
                        │
                        ▼
                Caption State Manager
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
       Stable/Final Translate   Overlay UI
              │
              ▼
       Google Translate
```

核心思想：

> 快速路径负责“尽快显示”，最终路径负责“最终正确”。

不要强迫同一次识别同时完成最低延迟、最高准确率和最高稳定性。

---

# 4. 第一优先级：Stable Prefix

## 4.1 问题

当前 partial 每次都会整体替换：

```text
今日は天気
今日は天気が
今日は天気がとても
今日は天気がかなりいい
今日は天気がとてもいい
```

用户看到的是整句不断抖动。

商业实时字幕通常不会无限允许整句修改，而是逐渐锁定已经稳定的前缀。

---

## 4.2 Stable Prefix 设计

维护最近若干次 partial：

```text
partial_1 = 今日は天気がとても
partial_2 = 今日は天気がとてもいい
partial_3 = 今日は天気がかなりいい
```

连续多次结果的共同稳定前缀：

```text
今日は天気が
```

将其锁定：

```text
Stable:
今日は天気が

Unstable:
かなりいい
```

UI 只允许 unstable tail 继续改变。

---

## 4.3 推荐判定

建议保存最近 3 次 partial：

```text
history = [p1, p2, p3]
```

满足以下条件之一即可扩大 stable prefix：

- 连续 2~3 次完全相同的字符前缀。
- 某段文字持续超过 0.8~1.2 秒不变。
- final 结果确认该部分。

推荐：

```text
stable_confirm_count = 3
stable_min_chars      = 2
```

日语不能简单按空格分词，因此优先基于字符或 tokenizer token，而不是英文 word。

---

## 4.4 收益

预期收益：

- 大幅减少字幕闪烁。
- 用户阅读体验提升最大。
- 不改变 ASR 模型本身。
- 实现风险低。
- 后续 rolling window 的基础。

优先级：

```text
★★★★★
```

---

# 5. 第二优先级：Partial / Final 双路径

当前系统虽然已有 partial 和 final，但两者使用逻辑应进一步彻底分离。

## 5.1 Partial Path

目标：

> 快。

建议：

```text
partial_every = 0.3 ~ 0.5 秒
```

Partial 只负责实时反馈，不要求最终正确。

允许：

- 少量文字修改。
- 尾部不稳定。
- 不完整标点。
- 暂时错误。

但已经进入 Stable Prefix 的文字不应再次被轻易修改。

---

## 5.2 Final Path

Final 触发时：

```text
完整 utterance audio
        ↓
重新运行一次完整 ASR
        ↓
得到最终结果
        ↓
覆盖 partial
```

Final 不应简单复用最后一次 partial。

原因：

完整 utterance 拥有更多右侧上下文，通常能改善：

- 同音词。
- 长句。
- 助词。
- 人名。
- 标点。
- 句尾。
- 前后语义。

RTX 3090 当前 RTF 很低，因此完全有能力承担 second pass。

---

## 5.3 推荐原则

```text
Partial = Fast / Temporary
Final   = Slow(er) / Accurate / Authoritative
```

Final 永远拥有最高优先级。

优先级：

```text
★★★★★
```

---

# 6. 第三优先级：Rolling Partial Window

## 6.1 当前问题

当前 partial 类似：

```text
0.6 秒 -> 识别 0~0.6
1.2 秒 -> 识别 0~1.2
1.8 秒 -> 识别 0~1.8
...
10 秒  -> 重新识别 0~10
```

随着说话时间增加，大量已经识别过的音频被重复计算。

---

## 6.2 推荐方案

Partial 只处理最近一个滚动窗口：

```text
rolling_window = 4~6 秒
overlap        = 1~2 秒
```

例如已经说了 10 秒：

```text
0~5 秒：
已经稳定并锁定

4~10 秒：
重新送入 Qwen 做 partial

4~5 秒：
作为 overlap，用于对齐
```

---

## 6.3 推荐初始值

```text
partial_window_sec = 6.0
partial_overlap    = 1.5
partial_every      = 0.4
```

之后实测：

```text
window:
4 / 5 / 6 / 8 sec

overlap:
1.0 / 1.5 / 2.0 sec

partial_every:
0.3 / 0.4 / 0.5 sec
```

---

## 6.4 需要解决的问题

Rolling window 需要处理：

- 重叠文本对齐。
- 日语没有空格。
- 重复字符串。
- 模型可能重新改写 overlap。
- partial 与 stable prefix 的拼接。

推荐优先做字符级 LCP/LCS 或 token 级 alignment。

优先级：

```text
★★★★★
```

实现风险：

```text
中
```

---

# 7. 第四优先级：分离 Segment Stable 与 Speech Final

当前：

```text
silence >= 0.8
=> final
```

过于简单。

商业系统通常区分：

```text
segment_stable
```

和：

```text
speech_final
```

---

## 7.1 segment_stable

含义：

> 某部分文字基本已经不会改变。

它主要服务 Stable Prefix。

即使说话人还没讲完整句，也可以锁住已经稳定的部分。

---

## 7.2 speech_final

含义：

> 当前 utterance 确实结束，可以做完整 final ASR 和翻译。

speech_final 才会：

- 完整重新识别。
- 提交字幕历史。
- 正式翻译。
- 清空当前 utterance buffer。

---

# 8. 第五优先级：Smart Endpointing

## 8.1 不再只依赖 silence

Endpoint 应综合：

```text
VAD silence
+
ASR partial stability
+
日语标点
+
语义完整度
+
segment length
```

---

## 8.2 第一版规则

不需要立即加入额外 LLM。

可以先使用规则：

### 明确句末

如果 partial 结尾为：

```text
。
？
！
?!
！？
```

且静音达到：

```text
0.35 ~ 0.50 sec
```

可以快速 final。

---

### 普通停顿

没有明确句末：

```text
silence >= 0.8 sec
```

继续沿用当前高精度配置。

---

### 犹豫词/连接词

如果结尾类似：

```text
あの
えー
えっと
その
だから
それで
でも
そして
というか
```

则延长等待：

```text
1.0 ~ 1.2 sec
```

避免：

```text
今日はですね、
[短暂停顿]
大阪に行こうと思っています。
```

被错误切成两句。

---

### Max Segment

保持：

```text
max_seg = 12 sec
```

超过后强制切段，防止无限增长。

---

## 8.3 动态 Silence

推荐第一版：

```text
明确句末          -> 0.40 sec
普通情况          -> 0.80 sec
犹豫/连接词        -> 1.10 sec
max_seg           -> 12 sec 强制 final
```

比继续微调固定：

```text
0.65 / 0.70 / 0.75 / 0.80
```

更有价值。

优先级：

```text
★★★★☆
```

---

# 9. 第六优先级：翻译与 Stable / Final 联动

## 9.1 当前问题

如果每次 partial 都调用 Google Translate：

```text
今日は
今日は天気
今日は天気が
今日は天気がいい
```

可能产生：

- 大量网络请求。
- 翻译闪烁。
- 旧请求覆盖新请求。
- 被 Google 限流的风险增加。
- 无意义重复翻译。

---

## 9.2 推荐策略

### Unstable partial

```text
不翻译
```

---

### Stable Prefix

如果 stable prefix 增长达到一定长度，可选择进行预翻译：

```text
stable_delta >= 8~12 chars
```

但不是必须。

---

### Final

```text
必须翻译
```

Final translation 是权威结果。

---

## 9.3 推荐方案

第一阶段最简单：

```text
partial -> 只显示日语
final   -> Google Translate
```

之后如果感觉中文出现太晚，再加 stable-prefix translation。

优先级：

```text
★★★★☆
```

---

# 10. 推荐字幕状态机

```text
                ┌───────────┐
                │   IDLE    │
                └─────┬─────┘
                      │
                 VAD speech
                      │
                      ▼
                ┌───────────┐
                │ SPEAKING  │
                └─────┬─────┘
                      │
          ┌───────────┼────────────┐
          │           │            │
      every 0.4s   short pause   endpoint
          │           │            │
          ▼           │            ▼
    PARTIAL ASR       │      ┌─────────────┐
          │           │      │ FINALIZING  │
          ▼           │      └──────┬──────┘
   Stable Prefix      │             │
          │           │       Full ASR Pass
          │           │             │
          └─────> UI  │             ▼
                      │       Final Transcript
                      │             │
                      │       Google Translate
                      │             │
                      │             ▼
                      │      Commit Caption
                      │             │
                      └─────────────┘
                                    │
                                    ▼
                                  IDLE
```

---

# 11. 推荐内部数据结构

可以维护一个 CaptionState：

```python
CaptionState(
    utterance_audio,
    stable_text,
    unstable_text,
    last_partials,
    last_partial_time,
    last_speech_time,
    speech_started_at,
    endpoint_candidate,
    finalizing,
)
```

关键字段：

```text
utterance_audio
当前完整句音频

stable_text
已经锁定、不再轻易变化的部分

unstable_text
当前尾部 provisional 字幕

last_partials
最近若干次 partial，用于稳定性判断

last_speech_time
最后检测到 speech 的时间

speech_started_at
当前 utterance 开始时间

endpoint_candidate
是否进入潜在句尾状态

finalizing
防止 final 重复触发
```

---

# 12. 建议线程/并发模型

RTX 3090 当前性能足够。

推荐保留：

```text
slots = 2
```

分工：

```text
Slot 1
快速 partial

Slot 2
final / refinement
```

但需要优先级：

```text
Final > Partial
```

一旦 final 到来：

- 可以取消过时 partial。
- 不允许旧 partial 覆盖 final。
- final 完成后清除对应 generation id 的旧请求。

建议每个 utterance 使用：

```text
utterance_id
generation_id
```

防止异步结果串线。

---

# 13. 不建议立即做的方案

## 13.1 不建议马上换 vLLM

当前 llama.cpp：

- 启动快。
- Q8 模型性能足够。
- RTX 3090 RTF 极低。
- 单用户场景非常合适。

vLLM 更适合：

- 多用户。
- 高并发。
- 长期服务。
- 大 batch。

目前切换后端带来的工程成本，大于潜在收益。

---

## 13.2 不建议马上换更大模型

目前体验瓶颈更主要来自：

- partial 抖动。
- 重复计算。
- endpointing。
- 字幕状态管理。

而不是模型本身跑不动。

应先完成流式架构升级，再重新评估是否需要更大模型。

---

## 13.3 不建议继续大量抠 VAD 微参数

当前已经完成较充分测试：

```text
vad_win            = 0.20
vad_threshold      = 0.10
min_speech         = 100 ms
min_silence        = 100 ms
speech_pad         = 200 ms
min_seg            = 1.2 sec
max_seg            = 12 sec
silence            = 0.80 sec
```

继续微调这些参数可能只有很小收益。

下一阶段架构级优化收益更大。

---

# 14. 推荐开发顺序

## Phase 1 — Stable Prefix

实现：

- 保存最近 partial。
- 计算公共稳定前缀。
- stable / unstable 分开显示。
- final 永远覆盖。

目标：

> 首先解决字幕“抖”。

---

## Phase 2 — Fast Partial / Accurate Final

实现：

- partial 与 final 明确分离。
- final 使用完整 utterance 再识别一次。
- 加 generation id 防止 stale partial 覆盖 final。

目标：

> 同时得到低延迟和高精度。

---

## Phase 3 — Rolling Partial Window

实现：

- 最近 4~6 秒 window。
- 1~2 秒 overlap。
- stable prefix 不再重复识别。
- overlap 字符/token 对齐。

目标：

> 减少重复计算，并把 partial_every 降到 0.3~0.5 秒。

---

## Phase 4 — Smart Endpointing

实现：

- 动态 silence。
- 句末标点快速结束。
- 犹豫词延迟结束。
- segment stable 与 speech final 分离。

目标：

> 减少错误切句，同时降低明显句尾的延迟。

---

## Phase 5 — Translation Coordination

实现：

- unstable partial 不翻译。
- final 必翻译。
- 可选 stable prefix 预翻译。
- 保留旧任务取消 / generation id 保护。

目标：

> 降低网络请求和中文闪烁。

---

## Phase 6 — Caption Formatter

加入：

- 日语自然换行。
- 最大字符数。
- 最多两行。
- 阅读速度控制。
- 过长句分段显示。
- stable/unstable 可选不同视觉样式。

目标：

> 从“ASR 输出框”升级为真正字幕体验。

---

## Phase 7 — Dynamic Hotwords

支持根据场景动态注入：

```text
动漫名称
角色名
游戏术语
公司名
会议参与者
技术术语
地名
人名
```

目标：

> 提高专有名词准确率。

---

# 15. 建议测试矩阵

每一阶段都不要只凭主观体验判断。

继续使用 BTSJ + 实际日语视频。

建议记录：

```text
CER
RTF
First Partial Latency
Stable Prefix Latency
Final Latency
Revision Count
Endpoint Delay
False Endpoint Count
Translation Request Count
GPU Usage
VRAM Usage
```

---

## 15.1 新增非常重要的实时字幕指标

传统 CER 不足以评价实时字幕体验。

应该新增：

### Revision Count

一条 utterance 在 final 前被修改多少次。

目标：

```text
越少越好
```

---

### Stable Prefix Delay

某个字符说出来之后，到它被锁定 stable 的时间。

目标参考：

```text
< 1 sec
```

---

### First Partial Latency

声音出现后第一次字幕出现的时间。

目标：

```text
约 300~700 ms
```

---

### Finalization Delay

说话结束到 final 出现的时间。

目标：

```text
约 500~1200 ms
```

视准确率取舍调整。

---

### Endpoint Error

包括：

```text
False Split
一句话被错误切成两段

Late Split
明明说完了但迟迟不结束
```

---

# 16. 推荐最终目标参数

架构升级后可先尝试：

```text
capture_chunk          = 0.10 sec
vad_win                = 0.20 sec

partial_every          = 0.40 sec
partial_window         = 6.0 sec
partial_overlap        = 1.5 sec

stable_history         = 3
stable_timeout         = 0.8~1.0 sec

normal_endpoint        = 0.80 sec
punctuation_endpoint   = 0.40 sec
hesitation_endpoint    = 1.10 sec

min_seg                = 1.2 sec
max_seg                = 12.0 sec

slots                  = 2
context_enabled        = false

final_second_pass      = true
translate_partial      = false
translate_final        = true
```

这些参数只是第一版工程默认值，最终必须再次实测。

---

# 17. 收益优先级总结

| 优化 | 体验收益 | 性能收益 | 实现风险 | 优先级 |
|---|---:|---:|---:|---:|
| Stable Prefix | 极高 | 中 | 低 | ★★★★★ |
| Partial / Final 双路径 | 极高 | 中 | 低 | ★★★★★ |
| Rolling Partial Window | 高 | 极高 | 中 | ★★★★★ |
| Smart Endpointing | 高 | 中 | 中 | ★★★★☆ |
| Dynamic Silence | 高 | 中 | 低 | ★★★★☆ |
| Translation Coordination | 高 | 中 | 低 | ★★★★☆ |
| Caption Formatter | 高 | 低 | 低 | ★★★★☆ |
| Dynamic Hotwords | 中 | 低 | 低 | ★★★☆☆ |
| 真正 Stateful Streaming ASR | 极高 | 极高 | 高 | ★★★★★ |
| 换更大 ASR 模型 | 不确定 | 低/负 | 高 | ★★☆☆☆ |
| 切换 vLLM | 不确定 | 场景相关 | 高 | ★★☆☆☆ |

---

# 18. 最终建议

当前最值得做的不是：

```text
换模型
换 vLLM
继续微调 VAD 0.05
```

而是：

```text
1. Stable Prefix
2. Partial / Final 双路径
3. Rolling Partial Window
4. Smart Endpointing
5. Translation 与 stable/final 联动
6. Caption Formatter
7. 再做完整 benchmark
```

最终目标是将 LocalLiveCaption 从：

```text
“每隔一段时间重新识别整段音频”
```

升级为：

```text
“低延迟 provisional 字幕
 + 稳定前缀锁定
 + 智能 endpoint
 + 完整 second-pass final
 + 字幕级状态管理”
```

这条路线最接近当前商业实时字幕系统的工程思路，也最适合现有的 RTX 3090 + Qwen3-ASR + llama.cpp 单机实时字幕环境。

---

# 19. 实施状态（2026-10-06）

当前已直接落地到 `live-caption-ja.pyw`：

- [x] Recoverable Stable Prefix：连续 partial 前缀确认后锁定；单次冲突先抑制，连续 3 次冲突则回退 stable 并接受新 hypothesis，避免早期识别错误永久冻结。
- [↩] Stable UI：已测试后按使用体验恢复原来的整句字幕显示；Recoverable Stable Prefix 内部逻辑继续保留。
- [x] Partial / Final 双路径：partial 只负责即时显示，final 始终使用完整 utterance second-pass 重识别。
- [x] Full-context Partial：A/B 测试否决了 6 秒 Rolling Partial Window。Qwen3-ASR 当前 llama.cpp 路径缺少可靠 token/time alignment，rolling 会造成错拼，因此 production partial 保留完整当前 utterance。
- [x] Smart Endpointing：区分普通静音、句末标点和日语犹豫/连接词。
- [x] Dynamic Silence：默认标点 0.40s / 普通 0.80s / 犹豫词 1.10s。
- [x] Translation 保持原状：继续使用原有 LiveCaptions-Translator 风格调度，不改 partial/final 的翻译触发策略。
- [x] Stale Partial Protection：segment 切换后旧 partial 结果不会覆盖新句/final。
- [x] GUI 参数：Stable 次数、三类 endpoint 静音均已加入识别参数窗口并持久化；rolling 参数已因 A/B 失败撤回。
- [↩] Caption Formatter：已测试后撤回，UI 恢复原来的 Tk Label + wraplength 自动换行。
- [ ] Dynamic Hotwords：现有静态 hotwords 可用，尚未做按场景自动注入。
- [ ] 更完整的真实视频 E2E：仍需长期观察 false endpoint、字幕阅读体验与异步竞争；本地 BTSJ 331+332+333 全量 final 回归及三文件长句 partial A/B 已完成。

当前实现原则：

```text
Partial:
完整当前 utterance
→ Qwen3-ASR
→ Recoverable Stable Prefix
→ 原始字幕 UI

Final:
完整 utterance
→ Qwen3-ASR second pass
→ authoritative final
→ Google Translate
→ UI
```

最终准确率仍由完整 final second-pass 兜底。测试发现 rolling window 在当前非 stateful Qwen3-ASR 路径会降低 partial 一致性，因此已撤回；Stable Prefix 只用于减少实时字幕抖动。

