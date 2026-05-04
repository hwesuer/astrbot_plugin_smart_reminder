# 智能定时提醒插件 (SmartReminder)

自动从聊天对话中识别定时提醒意图，通过 LLM 解析时间与事件，到点准时提醒用户。

## 功能特性

- **自动识别**：用户说出「提醒我明天下午三点开会」即可自动创建提醒
- **LLM 解析**：利用大模型理解自然语言中的时间和事件，支持相对/绝对时间
- **相对时间预处理**：先本地转换「X分钟后」「X小时后」等，降低 LLM 负担
- **重复提醒**：到点未回复时按设定间隔多次提醒，超次数自动删除
- **用户命令**：`/remind list` 查看、`/remind cancel` 取消、`/remind cancel all` 全部取消
- **自动取消**：可配置从对话中检测取消意图并自动移除任务
- **重启恢复**：启动时从 JSON 文件恢复未完成的任务并重新调度，逾时太久自动跳过
- **解析失败反馈**：LLM 无法解析时主动告知用户，避免静默失败
- **隐私解析**：通过 StarTools 直接发送提醒，不触发 LLM 回复链
- **并发安全**：asyncio.Lock 保护任务读写，五阶段触发流程加锁重验防竞态
- **关键词预筛**：消息不含时间关键词 / HH:MM 格式时不调用 LLM，节省 API 费用
- **资源管理**：scheduled / delayed 异步任务全生命周期追踪，终止时全部清理

## 安装

将插件文件夹放入 AstrBot 的 `addons` 目录下重启即可。

```
addons/
└── astrbot_plugin_smart_reminder/
    ├── main.py
    ├── _conf_schema.json
    ├── metadata.yaml
    └── README.md
```

## 配置

在 AstrBot 配置面板中配置以下参数：

### 必填

| 配置项 | 说明 |
|--------|------|
| `api_base` | LLM API 地址，如 `https://api.deepseek.com/v1` |
| `api_key` | LLM API 密钥 |
| `model` | 模型名，如 `deepseek-chat` |

### 可选（重要）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 是否启用插件 |
| `prompt_template` | 内置提示词 | 解析用户消息的系统提示词 |
| `enable_auto_cancel` | `true` | 自动检测并取消提醒 |
| `use_llm_ask` | `true` | 重复提醒时 LLM 生成文案 |
| `use_first_reminder_ai` | `false` | 首次提醒是否也调用 LLM |
| `default_ask_text` | `到点了~` | 非 AI 模式首次提醒文案 |
| `first_reminder_prompt_template` | 内置 | 首次提醒 AI 生成时使用的提示词 |
| `ask_style_template` | 内置 | 重复提醒时的文案风格模板 |
| `enable_re_remind` | `true` | 启用重复提醒 |
| `re_remind_interval_min` | `60` | 重复提醒最小间隔（秒） |
| `re_remind_interval_max` | `600` | 重复提醒最大间隔（秒） |
| `re_remind_max_count` | `3` | 最大提醒次数（含首次） |
| `max_tasks` | `20` | 最大任务数 |
| `max_remind_days` | `90` | 拒绝超远日期的提醒（防 LLM 幻觉） |
| `task_expire_days` | `7` | 过期任务自动清理天数 |
| `overdue_trigger_seconds` | `300` | 逾时容忍阈值（秒）。重启恢复时，过期任务仅在此阈值内的才立即触发，超过则静默删除 |
| `restore_tasks_on_startup` | `true` | 启动时恢复任务并重新调度 |

## 命令

| 命令 | 说明 |
|------|------|
| `/remind help` | 显示帮助 |
| `/remind list` | 查看当前所有提醒 |
| `/remind cancel <任务ID>` | 取消指定提醒 |
| `/remind cancel all` | 取消所有提醒 |

## 使用示例

**创建提醒**（直接说话即可）：
```
用户: 提醒我明天下午3点开会
机器人: 好的，已为你设置提醒：2026-05-05 15:00 开会
```

**解析失败时的反馈**：
```
用户: 提醒我下辈子发财
机器人: 抱歉，我没能理解你设置的时间「下辈子」，请用更明确的格式。
```

**管理提醒**：
```
用户: /remind list
机器人: 📋 当前提醒
        [t1717755123456] 2026-05-05 15:00 — 开会 (运行中)
```

## 数据文件

- 任务数据：`data/smart_reminder.json`
- 运行日志：`logs/smart_reminder.log`

## 依赖

- `httpx` — LLM API 调用
- `astrbot_plugin_smart_debounce` — 可选，安装后提醒消息不会被重复拦截

## 已知限制

- 时间基于服务器本地时间，跨时区需注意
- LLM 解析准确度取决于模型能力，建议使用较新模型
- 无持久化会话事件存储，重启后需用户再次发消息才能正常发送（已通过元数据降级处理减轻影响）

## 版本

**1.0.2** — 修复异步任务泄漏、逾时轰炸、解析静默失败等 6 项缺陷，完善资源生命周期管理。
