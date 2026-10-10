# 主窗口外观设置（v1.7.1300.1822 定制版）

本定制版仅增加主窗口字幕卡片的字体、字号、字体颜色、背景颜色设置，保留上游 Google2 等翻译配置。

## 如何使用

1. 先彻底退出程序，并备份正在使用的 `setting.json`。
2. 在 **原有** `setting.json` 的 `MainWindow` 对象里追加以下字段，不要删除其他对象（尤其是 `Configs`）。
3. 保存并重新打开定制版 EXE。程序关闭前不要在运行时编辑 JSON（自动保存可能覆盖编辑）。

```json
"MainWindow": {
  "Topmost": true,
  "CaptionLogEnabled": false,
  "LatencyShow": true,
  "OriginalFontFamily": "Yu Gothic UI",
  "OriginalFontSize": 20,
  "OriginalFontColor": "#F5F5F5",
  "TranslatedFontFamily": "Microsoft YaHei UI",
  "TranslatedFontSize": 24,
  "TranslatedFontColor": "#8DDBFF",
  "CaptionBackgroundColor": "#1B1B1B"
}
```

- 颜色支持 WPF 颜色写法，例如 `#RRGGBB` 或 `#AARRGGBB`；非法颜色会使用备用色。
- 字号限制为 8~40；字体名称应为 Windows 已安装的字体。
- 翻译文本较长时，保留原版自动缩小字号的行为，按照新设字号同比缩小为约 5/6。
- 没有这些新字段的原版 `setting.json` 也可直接读取，并自动启用上述默认外观。
- 原版 EXE 不支持这些新字段；不要用定制版替换官方安装，建议放独立文件夹运行。

源码基础：`SakiRinn/LiveCaptions-Translator` tag `v1.7.1300.1822`。
