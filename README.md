# astrbot_plugin_smart_reminder

智能定时提醒插件 for AstrBot v4.23.6

实时分析用户与 AI 的对话，自动检测用户提到的带时间的事件，创建定时任务。到点时根据上下文生成自然询问，并加入时间摆动和概率遗忘机制，让提醒像真人一样自然。用户未回复时自动重复提醒。

## 功能特性

- **自动检测** — 监听 LLM 回复，自动识别对话中的时间+事件组合
- **智能解析** — 调用 LLM 解析时间表达（支持模糊时间如"晚上八点"、"明早"、"下周"等）
- **关键词分类** — 区分准时类（千万要、别忘了）和随意类（大概、差不多），影响后续行为
- **时间摆动** — 提醒时间在设定范围内随机偏移，避免机械感
- **提前提醒** — 一定概率提前提醒
- **延迟遗忘** — 一定概率延迟提醒（模拟"差点忘了"）
- **完全遗忘** — 一定概率直接不提醒（模拟真的忘了，**准时类任务不受影响**）
- **重复提醒** — 用户未回复时自动重试，语气逐次加强
- **自动取消** — 检测用户后续消息中的取消意图，自动移除对应任务
- **指令管理** — 支持 `/remind list/cancel/clear` 指令

## 工作流程

```
用户消息 → on_llm_request
    ├── 检测是否回复了等待中的提醒 → 标记任务完成
    └── 继续...

LLM 回复 → on_llm_response
    ├── 无时间+事件 → 放行
    ├── 有取消意图 → 自动移除任务
    └── 有时间+事件 → LLM 解析
            ├── 关键词分类 (precise/casual/normal)
            ├── LLM 生成首次提醒文案
            ├── 计算实际触发时间 (摆动/提前/延迟)
            ├── 创建任务并调度
            └── 到点时:
                    ├── 完全遗忘检查 (precise 类型跳过)
                    ├── 发送提醒
                    ├── 等待用户回复
                    ├── 超时 → 重复提醒 (LLM 生成新文案，语气逐次加强)
                    └── 达到最大次数 → 结束
```

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | true | 是否启用插件 |
| `api_base` | string | "" | LLM API 地址（必填，未配置则插件降级禁用） |
| `api_key` | string | "" | LLM API 密钥 |
| `model` | string | "" | LLM 模型名 |
| `enable_jitter` | bool | true | 是否启用时间摆动 |
| `jitter_range_seconds` | int | 120 | 时间摆动范围（秒） |
| `enable_forgetting` | bool | false | 是否启用延迟遗忘 |
| `forget_probability` | int | 15 | 延迟遗忘概率（%） |
| `forget_max_delay` | int | 600 | 延迟遗忘最大延迟（秒） |
| `early_remind_probability` | int | 5 | 提前提醒概率（%） |
| `early_remind_max_seconds` | int | 300 | 提前提醒最大提前量（秒） |
| `enable_complete_forget` | bool | false | 是否启用完全遗忘 |
| `complete_forget_probability` | int | 5 | 完全遗忘概率（%） |
| `precise_keywords` | list | ["千万要","别忘了","一定要","务必"] | 准时触发关键词 |
| `casual_keywords` | list | ["大概","差不多","随便","随意"] | 随意关键词 |
| `casual_keyword_boost` | int | 25 | 随意关键词概率增幅（0~50） |
| `enable_re_remind` | bool | true | 是否启用重复提醒 |
| `re_remind_interval_min` | int | 60 | 重复提醒最小间隔（秒） |
| `re_remind_interval_max` | int | 600 | 重复提醒最大间隔（秒） |
| `re_remind_max_count` | int | 3 | 最大重复提醒次数（实际±1随机） |
| `max_tasks` | int | 20 | 最大任务数 |
| `task_expire_days` | int | 7 | 任务过期自动清理天数 |
| `use_llm_ask` | bool | true | 触发时是否调用 LLM 生成询问文案 |
| `default_ask_text` | string | "到点了~" | 默认提醒文案 |
| `ask_style_template` | text | (见默认值) | 重复提醒文案风格模板 |
| `prompt_template` | text | (见默认值) | 时间解析提示词模板 |
| `prompt_template_cancel` | text | (见默认值) | 取消分析提示词模板 |

## 安装

1. 将 `astrbot_plugin_smart_reminder` 目录放入 AstrBot 的 `addons/plugins` 目录
2. 重启 AstrBot
3. 在插件配置中填入 LLM API 信息（api_base、api_key、model）
4. 启用插件

## 使用方法

对话中自然地提及时间和事件即可，例如：

- "晚上八点还要上课" → 自动创建 20:00 的"上课"提醒
- "千万别忘了明天早上九点开会" → 创建准时提醒，不受遗忘机制影响
- "大概下午三点左右去拿快递" → 创建随意提醒，遗忘/摆动概率提高

手动管理：
- `/remind list` — 查看所有任务
- `/remind cancel <id>` — 取消指定任务
- `/remind clear` — 清空所有任务

## ⚠️ Token 消耗提醒

本插件在以下场景会调用 LLM API：

1. **每条 LLM 回复后** — 调用 LLM 分析是否需要创建/取消任务
2. **创建任务时** — 调用 LLM 生成首次提醒文案（可关闭）
3. **重复提醒时** — 每次重试调用 LLM 生成新文案（可关闭）
4. **每条用户消息** — 调用 LLM 分析是否在取消任务

请根据您的 API 额度合理配置 `use_llm_ask` 和重复提醒相关参数。

## 注意事项

1. **必须配置 LLM API** — `api_base`、`api_key`、`model` 三个字段缺一不可，否则插件自动降级为禁用
2. **完全遗忘风险** — 开启 `enable_complete_forget` 后，非准时类任务有一定概率直接被遗忘不触发。**包含"千万要""别忘了""一定要""务必"等关键词的准时类任务不受此影响**
3. **时间解析依赖 LLM** — 模糊时间表达（如"下午"、"明早"）由 LLM 推断，可能存在偏差
4. **数据持久化** — 任务数据存储在 `data/astrbot_plugin_smart_reminder_data.json`，重启后自动恢复
5. **重复提醒间隔** — 默认 60~600 秒随机，可按需调整

## 免责声明

- 本插件依赖 LLM API 进行时间解析和文案生成，解析结果可能存在误差
- 完全遗忘机制可能导致非准时类提醒丢失，请谨慎启用
- 本插件仅供学习和个人使用，不保证 100% 准确提醒
- 因插件遗漏提醒导致的任何损失，开发者不承担责任

## 兼容性

- AstrBot v4.23.6+
- Python 3.8+
