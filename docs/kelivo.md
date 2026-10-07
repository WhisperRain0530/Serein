# Kelivo 供应商接入

先完成 [Zeabur 部署](zeabur.md)，再在 Serein 网页的「设置 → 模型」中配置实际中转地址、API Key 和模型。Serein 的 Gateway Key 与中转 API Key 是两种不同凭据。

## 手动添加

在 Kelivo 添加 OpenAI 类型供应商：

| 字段 | 内容 |
| --- | --- |
| 名称 | Serein |
| API Base URL | `https://你的域名/v1`，不加尾斜杠 |
| API Key | 当前 Serein 实例的 Gateway Key |
| API Path | `/chat/completions` |
| Response API | 关闭 |

保存后点击「获取模型」，启用返回的模型。模型名来自 Serein 配置，一般形如 `上游名/模型别名`；不要凭空填一个未配置的模型 ID。空实例的模型列表可能为空，表示尚未配置上游。

Kelivo 会发送 `X-Conversation-Id`，本分支会在没有显式 Serein/Ombre 窗口标头时用它区分聊天窗口。每个窗口仍共享当前实例的记忆库，但原话归档、召回冷却等窗口状态不会全部混入默认窗口。无需手动填写 `{conversation_id}`。

## 供应商导入文本

Kelivo v1.3.0 支持粘贴或扫码导入下列格式：

```text
ai-provider:v1:<标准 Base64 编码的 UTF-8 JSON>
```

JSON 示例（必须替换占位符）：

```json
{"type":"openai","name":"Serein","apiKey":"你的 Gateway Key","baseUrl":"https://你的域名/v1"}
```

导入后仍需获取并启用模型。该文本和二维码包含可还原的 Gateway Key，只能私下保存，不要放进公开仓库、公共网页或公开截图。不要把密钥附加到 API Base URL 的查询参数中。

格式依据：[Kelivo v1.3.0 导入实现](https://github.com/Chevey339/kelivo/blob/v1.3.0/lib/features/provider/widgets/import_provider_sheet.dart)、[会话标头实现](https://github.com/Chevey339/kelivo/blob/v1.3.0/lib/features/home/services/message_generation_service.dart)。
