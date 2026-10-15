# HTTP 取消实现报告（2026-10-15）

上游对照：third_party/LiveCaptions-Translator/src/models/TranslationTaskQueue.cs。新任务完成后取消此前的 CTS，而非排队时取消。

Python 翻译模块实现每任务 CancellationToken，取消不再只丢弃结果，而会终止正在执行的 HTTP 响应读取、TLS 握手和非阻塞发送。默认整次请求超时预算 8 秒，无新增第三方依赖。保持 ASR/VAD/音频代码未改。

Windows 本地慢速 HTTP 及竞速测试 9/9 通过；全回归 101/101 通过；Google/Google2 实际 HTTPS 通过；完成单 .pyw 六模块重新打包。非翻译前缀 SHA256 保持 7e0667b255cf824e07acfd2da164b4841518c1df42b96bd0ce69d90d632fa5a7。

限制：操作系统 DNS/TCP connect/代理 CONNECT 仍可能受到底层阻塞；未做 .NET 与 Python 同机同步基准，不能宣称所有请求的取消延迟及连接池性能绝对一致；曾观察到一次 Google 空译文，直接 API 重试未复现。没有 Git commit/push/reset/clean。
