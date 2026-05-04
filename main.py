import json
import asyncio
import re
import random
import os
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# 导入 SmartDebounce 的全局跳过列表（可选依赖）
try:
    from astrbot_plugin_smart_debounce.main import global_skip_ids as debounce_skip_ids
except ImportError:
    debounce_skip_ids = None


# 默认提示词预设
DEFAULT_PROMPT_TEMPLATE = (
    "你是一个智能助手，正在分析用户和 AI 的对话。\n"
    "请判断用户是否提到了需要定时提醒的事情。\n\n"
    "判断标准：\n"
    "- 用户明确提到了一个时间点（如晚上八点、明早九点、下周三等）\n"
    "- 用户提到了需要做某件事（如上课、交作业、开会、吃饭等）\n"
    "- 用户语气是告知、安排或请求提醒，而非单纯的陈述\n\n"
    "如果满足以上条件，返回 JSON：\n"
    '{"should_remind": true, "time": "具体时间", "event": "事件", "context": "原文上下文", "is_important": false}\n\n'
    '如果不满足，返回：\n'
    '{"should_remind": false}\n\n'
    "注意：\n"
    '- is_important 根据用户是否使用了强调语气来判断\n'
    '- 时间格式统一为 2026-04-29 20:00 这样的 ISO 格式\n'
    "- 如果时间表述模糊（如下午），尽可能推断为合理时间"
)

DEFAULT_CANCEL_TEMPLATE = (
    "你是一个智能助手，正在分析用户是否在取消之前设定的提醒任务。\n"
    "用户之前设置了以下任务：\n"
    "{tasks}\n\n"
    "请判断用户当前的消息是否在取消其中某个任务。\n\n"
    "支持的表达方式：\n"
    "- 直接取消：「不用提醒了」「取消提醒」\n"
    "- 变更计划：「今天下午不去了」「改到明天了」\n"
    "- 已完成：「作业已经交了」「做完了」\n"
    "- 否定语气：「不用xxx了」「别提醒了」\n\n"
    "如果匹配到某个任务，返回 JSON：\n"
    '{"should_cancel": true, "task_id": "匹配的任务ID", "reason": "取消原因"}\n\n'
    '如果没有匹配，返回：\n'
    '{"should_cancel": false}'
)

DEFAULT_ASK_STYLE_TEMPLATE = (
    "你是一个亲切的朋友，正在提醒用户做某件事。\n"
    "请根据以下信息生成一句简短的提醒，语气自然、贴近生活。\n\n"
    "触发类型：{trigger_type}\n"
    "事件：{event}\n"
    "这是第 {retry_count} 次提醒，前几次用户没有回复。\n\n"
    "风格要求：\n"
    "- 第一次提醒可以直接说事\n"
    "- 后续提醒可以带一点催促或关切的语气\n"
    "- 不要过于正式或生硬\n"
    "- 每次生成的文案要有变化，不要重复\n"
    "- 控制在 30 字以内\n\n"
    "示例风格：\n"
    '- 准时提醒："啊，到点了，该去{event}了~"\n'
    '- 提前提醒："快到时间了，准备一下{event}吧"\n'
    '- 推迟提醒："是不是忘了{event}了..."'
)


@register("astrbot_plugin_smart_reminder", "插件作者", "智能定时提醒插件 - 实时分析对话并自动创建定时任务", "1.0.0")
def _init_plugin_logger() -> logging.Logger:
    """初始化插件专用日志，写入 logs/smart_reminder.log"""
    try:
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.join(plugin_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "smart_reminder.log")
        handler = logging.FileHandler(log_path, encoding="utf-8", mode="a")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger = logging.getLogger("SmartReminderFileLog")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info("=== SmartReminder 日志已启动 ===")
        return logger
    except Exception as e:
        print(f"[SmartReminder] 日志初始化失败: {e}")
        return None

class SmartReminder(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.tasks: List[Dict] = []
        self.scheduled_tasks: Dict[str, asyncio.Task] = {}
        self.waiting_replies: Dict[str, str] = {}
        self.re_remind_tasks: Dict[str, asyncio.Task] = {}
        # 保存每个会话的最新事件，用于主动发送提醒
        self.session_events: Dict[str, AstrMessageEvent] = {}
        self.data_dir = os.path.join(os.getcwd(), "data")
        self.data_file = os.path.join(self.data_dir, "astrbot_plugin_smart_reminder_data.json")
        self._http_client: Optional[httpx.AsyncClient] = None
        self._loaded = False
        self.plugin_logger = _init_plugin_logger()

    # ==================== 初始化与配置读取 ====================

    async def _ensure_loaded(self):
        if not self._loaded:
            await self._load_tasks()
            self._loaded = True

    def _get_config(self, key: str, default=None):
        """安全地获取配置项，兼容 AstrBot 的配置对象"""
        try:
            val = self.config.get(key, default)
            return val
        except Exception:
            return default

    @property
    def enabled(self) -> bool:
        return bool(self._get_config("enabled", True))

    @property
    def api_base(self) -> str:
        return str(self._get_config("api_base", "") or "")

    @property
    def api_key(self) -> str:
        return str(self._get_config("api_key", "") or "")

    @property
    def model(self) -> str:
        return str(self._get_config("model", "") or "")

    def _is_api_configured(self) -> bool:
        return bool(self.api_base and self.api_key and self.model)

    # ==================== LLM API 调用 ====================

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        return self._http_client

    async def _call_llm(self, system_prompt: str, user_content: str, temperature: float = 0.7) -> Optional[str]:
        if not self._is_api_configured():
            logger.error("[SmartReminder] LLM API 未配置，无法调用")
            return None
        client = await self._get_http_client()
        url = self.api_base.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": temperature
        }
        try:
            logger.info(f"[SmartReminder] 调用 LLM API: {url}")
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error(f"[SmartReminder] LLM API 返回错误: {resp.status_code} - {resp.text[:300]}")
                return None
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip()
        except Exception as e:
            logger.error(f"[SmartReminder] LLM API 调用异常: {e}")
            return None

    def _parse_json_from_llm(self, text: str) -> dict:
        if not text:
            return {}
        text = text.strip()
        # 移除 markdown 代码块标记
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines).strip()
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试提取花括号内的内容
        m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return {}

    # ==================== 关键词分类 ====================

    def _classify_keywords(self, text: str) -> str:
        precise_keywords = self._get_config("precise_keywords", ["千万要", "别忘了", "一定要", "务必"])
        casual_keywords = self._get_config("casual_keywords", ["大概", "差不多", "随便", "随意"])
        for kw in precise_keywords:
            if kw in text:
                return "precise"
        for kw in casual_keywords:
            if kw in text:
                return "casual"
        return "normal"

    # ==================== Task Persistence ====================

    # ==================== 任务持久化（存/读） ====================

    async def _load_tasks(self):
        if not self._get_config("restore_tasks_on_startup", True):
            self.tasks = []
            logger.info("[SmartReminder] 启动时不恢复任务（restore_tasks_on_startup=false）")
            return
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self.tasks = json.load(f)
                logger.info(f"[SmartReminder] 从文件加载了 {len(self.tasks)} 个任务")
            except Exception as e:
                logger.error(f"[SmartReminder] 加载任务文件失败: {e}")
                self.tasks = []
        else:
            self.tasks = []
        self._cleanup_expired_tasks()
        for task in self.tasks:
            if not task.get("completed", False) and not task.get("cancelled", False):
                self._schedule_existing_task(task)
        await self._save_tasks()

    async def _save_tasks(self):
        os.makedirs(self.data_dir, exist_ok=True)
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[SmartReminder] 保存任务文件失败: {e}")

    def _cleanup_expired_tasks(self):
        expire_days = self._get_config("task_expire_days", 7)
        cutoff = datetime.now() - timedelta(days=expire_days)
        self.tasks = [
            t for t in self.tasks
            if not (t.get("completed") or t.get("cancelled"))
            or datetime.fromisoformat(t.get("created_at", datetime.now().isoformat())) > cutoff
        ]

    # ==================== Scheduling ====================

    # ==================== 任务调度与时间计算 ====================

    def _schedule_existing_task(self, task: Dict):
        task_id = task["id"]
        if task_id in self.scheduled_tasks:
            self.scheduled_tasks[task_id].cancel()
        try:
            trigger_time = datetime.fromisoformat(task["time"])
        except (ValueError, KeyError):
            logger.error(f"[SmartReminder] 任务 {task_id} 时间格式无效: {task.get('time')}")
            return
        delay = (trigger_time - datetime.now()).total_seconds()
        if delay <= 0:
            delay = 1
        logger.info(f"[SmartReminder] 为现有任务 {task_id} 安排调度, 剩余 {delay:.0f} 秒")
        coro = self._scheduled_trigger(task_id, delay)
        self.scheduled_tasks[task_id] = asyncio.ensure_future(coro)

    def _get_adjusted_trigger_time(self, base_time: datetime, keyword_type: str) -> datetime:
        enable_jitter = self._get_config("enable_jitter", True)
        jitter_range = self._get_config("jitter_range_seconds", 120)
        enable_early = True
        early_prob = self._get_config("early_remind_probability", 5)
        early_max = self._get_config("early_remind_max_seconds", 300)
        enable_forgetting = self._get_config("enable_forgetting", False)
        forget_prob = self._get_config("forget_probability", 15)
        forget_max_delay = self._get_config("forget_max_delay", 600)
        casual_boost = self._get_config("casual_keyword_boost", 25)

        adjusted = base_time

        if keyword_type == "casual":
            early_prob = min(100, early_prob + casual_boost)
            forget_prob = min(100, forget_prob + casual_boost)

        if enable_early and random.randint(1, 100) <= early_prob:
            early_seconds = random.randint(1, early_max)
            adjusted = adjusted - timedelta(seconds=early_seconds)
            logger.info(f"[SmartReminder] 提前提醒: 提前 {early_seconds} 秒")

        if enable_forgetting and random.randint(1, 100) <= forget_prob:
            delay_seconds = random.randint(1, forget_max_delay)
            adjusted = adjusted + timedelta(seconds=delay_seconds)
            logger.info(f"[SmartReminder] 延迟遗忘: 延迟 {delay_seconds} 秒")

        if enable_jitter:
            jitter = random.randint(-jitter_range, jitter_range)
            adjusted = adjusted + timedelta(seconds=jitter)
            if jitter != 0:
                logger.info(f"[SmartReminder] 时间摆动: {'+' if jitter > 0 else ''}{jitter} 秒")

        return adjusted

    async def _scheduled_trigger(self, task_id: str, delay_seconds: float):
        try:
            await asyncio.sleep(delay_seconds)
            await self._on_task_trigger(task_id)
        except asyncio.CancelledError:
            logger.info(f"[SmartReminder] 任务 {task_id} 被取消")
        except Exception as e:
            logger.error(f"[SmartReminder] 任务 {task_id} 触发异常: {e}")

    # ==================== Trigger Logic ====================

    # ==================== 定时任务触发逻辑 ====================

    async def _on_task_trigger(self, task_id: str):
        task = self._find_task(task_id)
        if not task:
            logger.error(f"[SmartReminder] 任务 {task_id} 不存在")
            return
        if task.get("completed") or task.get("cancelled"):
            return

        logger.info(f"[SmartReminder] ===== 任务触发: {task_id} 事件: {task.get('event')} =====")
        self.plugin_logger.info(f"[任务触发] task_id={task_id} event={task.get('event')} time={task.get('time')}")

        keyword_type = task.get("keyword_type", "normal")
        enable_complete_forget = self._get_config("enable_complete_forget", False)
        complete_forget_prob = self._get_config("complete_forget_probability", 5)
        casual_boost = self._get_config("casual_keyword_boost", 25)

        # 计算有效概率
        if keyword_type == "precise":
            effective_prob = 0  # 准时类无视遗忘
        elif keyword_type == "casual":
            effective_prob = min(100, complete_forget_prob + casual_boost)
        else:
            effective_prob = complete_forget_prob

        if keyword_type != "precise" and enable_complete_forget and random.randint(1, 100) <= effective_prob:
            logger.info(f"[SmartReminder] 完全遗忘触发: 任务 {task_id} ({task.get('event')}) 将被删除")
            await self._remove_task(task_id)
            return

        retry_count = task.get("retry_count", 0)

        if retry_count == 0:
            # 第一次提醒
            use_special = self._get_config("use_first_reminder_ai", False)
            if use_special:
                # 用单独配置的 AI 生成提示词（让 LLM 转发消息给用户）
                ask_text = await self._generate_first_reminder_text(task)
            else:
                # 原来的方式：用创建时生成的文案或默认文案
                ask_text = task.get("first_ask_text") or self._get_config("default_ask_text", "到点了~")
        else:
            # 后续提醒：用 LLM 重新生成
            ask_text = await self._generate_ask_text(task, retry_count)

        if ask_text:
            await self._send_reminder(task, ask_text)

        task["retry_count"] = retry_count + 1
        await self._save_tasks()

        # 计算重复提醒次数（随机摆动）
        max_count = self._get_config("re_remind_max_count", 3)
        actual_max = max(1, max_count + random.randint(-1, 1))
        if keyword_type == "precise":
            actual_max = max(1, max_count + random.randint(0, 1))

        enable_re_remind = self._get_config("enable_re_remind", True)
        if enable_re_remind and retry_count < actual_max:
            task["waiting_for_reply"] = True
            await self._save_tasks()
            coro = self._wait_for_user_reply(task_id, retry_count + 1)
            self.re_remind_tasks[task_id] = asyncio.ensure_future(coro)
        else:
            task["completed"] = True
            task["waiting_for_reply"] = False
            await self._save_tasks()
            logger.info(f"[SmartReminder] 任务 {task_id} 完成")

    async def _generate_ask_text(self, task: Dict, retry_count: int) -> Optional[str]:
        use_llm = self._get_config("use_llm_ask", True)
        if not use_llm or not self._is_api_configured():
            return self._get_config("default_ask_text", "到点了~")

        template = self._get_config("ask_style_template", "")
        if not template:
            template = DEFAULT_ASK_STYLE_TEMPLATE

        trigger_type = task.get("trigger_type", "准时提醒")
        prompt = template.format(
            trigger_type=trigger_type,
            event=task.get("event", ""),
            retry_count=retry_count
        )

        result = await self._call_llm(
            "请根据要求生成一句话提醒文案。只返回文案本身，不要加引号。",
            prompt,
            temperature=0.9
        )
        if result:
            logger.info(f"[SmartReminder] LLM 生成询问文案: {result}")
            return result
        return self._get_config("default_ask_text", "到点了~")

    async def _generate_first_reminder_text(self, task: Dict) -> str:
        """首次提醒时，用主 API 配置生成提示词，让 LLM 转发消息给用户"""
        if not self._is_api_configured():
            return task.get("first_ask_text") or self._get_config("default_ask_text", "到点了~")

        template = str(self._get_config("first_reminder_prompt_template", ""))
        if not template:
            return task.get("first_ask_text") or self._get_config("default_ask_text", "到点了~")

        # 读取 AstrBot 人格设定作为系统提示词
        persona_prompt = self._get_astrbot_persona()

        event = task.get("event", "")
        text = task.get("first_ask_text") or "到点了"
        prompt = template.replace("{event}", event).replace("{text}", text)

        result = await self._call_llm(
            persona_prompt,
            prompt,
            temperature=0.7
        )
        if result:
            # 清洗 LLM 输出，过滤函数调用格式
            cleaned = self._clean_llm_output(result)
            logger.info(f"[SmartReminder] 首次提醒 AI 生成(原始): {result}")
            logger.info(f"[SmartReminder] 首次提醒 AI 生成(清洗后): {cleaned}")
            return cleaned
        return task.get("first_ask_text") or self._get_config("default_ask_text", "到点了~")

    def _clean_llm_output(self, text: str) -> str:
        """清洗 LLM 输出，过滤掉可能的函数调用外壳"""
        import re
        # 匹配 send_message_to_user("xxx") 或 send_message_to_user('xxx')
        m = re.search(r'send_message_to_user\([\'"](.+?)[\'"]\)', text)
        if m:
            return m.group(1)
        # 匹配其他函数调用格式，如 func_name("xxx")
        m = re.search(r'\w+\([\'"](.+?)[\'"]\)', text)
        if m:
            return m.group(1)
        return text

    def _get_astrbot_persona(self) -> str:
        """读取 AstrBot 的人格设定（支持 UTF-8 BOM）"""
        try:
            import json, os
            config_path = os.path.join(os.getcwd(), "data", "cmd_config.json")
            if os.path.exists(config_path):
                # 先用 utf-8-sig（自动跳过 BOM）读取
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    config = json.load(f)
                pool = config.get("persona_pool", [])
                if pool and len(pool) > 0:
                    prompt = pool[0].get("prompt", "")
                    if prompt:
                        logger.info(f"[SmartReminder] 已加载人格设定")
                        return prompt
        except Exception as e:
            logger.error(f"[SmartReminder] 读取人格设定失败: {e}")
        # 默认系统提示词
        return "你是一个定时提醒助手。请根据用户要求生成提醒内容。"

    async def _call_llm_with_config(self, system_prompt: str, user_content: str,
                                     api_base: str, api_key: str, model: str,
                                     temperature: float = 0.7) -> Optional[str]:
        """用指定的 API 配置调用 LLM"""
        if not api_base or not api_key or not model:
            return None
        client = await self._get_http_client()
        url = api_base.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": temperature
        }
        try:
            logger.info(f"[SmartReminder] 调用首次提醒 AI: {url}")
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error(f"[SmartReminder] 首次提醒 AI 返回错误: {resp.status_code}")
                return None
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip()
        except Exception as e:
            logger.error(f"[SmartReminder] 首次提醒 AI 调用异常: {e}")
            return None

    # ==================== 发送提醒消息 ====================
    async def _send_reminder(self, task: Dict, text: str):
        """主动发送提醒消息给用户（使用 StarTools，绕过 SmartDebounce）"""
        session_id = task.get("session_id", "")
        logger.info(f"[SmartReminder] 发送提醒 session={session_id} 内容={text}")
        self.plugin_logger.info(f"[发送提醒] session={session_id} 内容={text}")

        event = self.session_events.get(session_id)
        if event:
            try:
                from astrbot.core.message.components import Plain
                from astrbot.core.star.star_tools import StarTools

                new_message = await StarTools.create_message(
                    type=str(event.message_obj.type.value),
                    self_id=event.get_self_id(),
                    session_id=event.session_id,
                    sender=event.message_obj.sender,
                    message=[Plain(text)],
                    message_str=text,
                    group_id=event.get_group_id() or ""
                )
                if debounce_skip_ids is not None:
                    debounce_skip_ids.add(new_message.message_id)
                await StarTools.create_event(
                    abm=new_message,
                    platform=event.get_platform_name(),
                    is_wake=True
                )
                logger.info(f"[SmartReminder] 已通过 StarTools 发送提醒（跳过 SmartDebounce）")
                return
            except Exception as e:
                logger.error(f"[SmartReminder] 发送失败: {e}")

        logger.error(f"[SmartReminder] 无法发送提醒：未找到会话 event，session={session_id}")

    # ==================== 等待用户回复（重复提醒） ====================

    async def _wait_for_user_reply(self, task_id: str, retry_count: int):
        interval_min = self._get_config("re_remind_interval_min", 60)
        interval_max = self._get_config("re_remind_interval_max", 600)
        wait_seconds = random.randint(interval_min, interval_max)
        logger.info(f"[SmartReminder] 等待用户回复, 任务={task_id}, 等待 {wait_seconds} 秒")
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            return

        task = self._find_task(task_id)
        if not task:
            return
        if task.get("waiting_for_reply"):
            logger.info(f"[SmartReminder] 超时未回复，再次提醒, 任务={task_id}")
            task["waiting_for_reply"] = False
            await self._save_tasks()
            await self._on_task_trigger(task_id)

    # ==================== Task Management ====================

    # ==================== 任务管理（增删改查） ====================

    def _find_task(self, task_id: str) -> Optional[Dict]:
        for t in self.tasks:
            if t.get("id") == task_id:
                return t
        return None

    async def _remove_task(self, task_id: str):
        if task_id in self.scheduled_tasks:
            self.scheduled_tasks[task_id].cancel()
            del self.scheduled_tasks[task_id]
        if task_id in self.re_remind_tasks:
            self.re_remind_tasks[task_id].cancel()
            del self.re_remind_tasks[task_id]
        task = self._find_task(task_id)
        if task:
            task["cancelled"] = True
            task["waiting_for_reply"] = False
        await self._save_tasks()
        logger.info(f"[SmartReminder] 任务 {task_id} 已取消")

    async def _create_task(self, task_info: Dict, event: Optional[AstrMessageEvent] = None):
        if not self._is_api_configured():
            logger.warning("[SmartReminder] LLM API 未配置，拒绝创建任务")
            return None

        max_tasks = self._get_config("max_tasks", 20)
        active_count = sum(1 for t in self.tasks if not t.get("completed") and not t.get("cancelled"))
        if active_count >= max_tasks:
            logger.warning(f"[SmartReminder] 任务数已达上限 ({max_tasks})")
            return None

        try:
            base_time = datetime.fromisoformat(task_info["time"])
        except (ValueError, KeyError):
            logger.error(f"[SmartReminder] 无法解析时间: {task_info.get('time')}")
            return None

        text = task_info.get("context", "")
        keyword_type = self._classify_keywords(text)
        adjusted_time = self._get_adjusted_trigger_time(base_time, keyword_type)

        delta = (adjusted_time - base_time).total_seconds()
        if delta < -10:
            trigger_type = "提前提醒"
        elif delta > 10:
            trigger_type = "推迟提醒"
        else:
            trigger_type = "准时提醒"

        task_id = str(uuid.uuid4())[:8]
        session_id = ""
        full_session = ""
        if event:
            try:
                full_session = event.session_id  # 完整格式如 "FriendMessage:2320504270"
                session_id = str(event.get_session_id())  # 简写如 "2320504270"
            except Exception:
                pass
            # 保存会话事件用于主动发送
            self.session_events[session_id] = event
            self.session_events[full_session] = event

        # 生成首次提醒文案
        first_ask_text = await self._generate_first_ask_text(task_info.get("event", ""), keyword_type)
        if not first_ask_text:
            first_ask_text = self._get_config("default_ask_text", "到点了~")

        task = {
            "id": task_id,
            "time": adjusted_time.isoformat(),
            "original_time": task_info["time"],
            "event": task_info.get("event", ""),
            "context": task_info.get("context", ""),
            "is_important": task_info.get("is_important", False) or keyword_type == "precise",
            "keyword_type": keyword_type,
            "trigger_type": trigger_type,
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
            "first_ask_text": first_ask_text,
            "retry_count": 0,
            "waiting_for_reply": False,
            "completed": False,
            "cancelled": False
        }

        self.tasks.append(task)
        await self._save_tasks()

        delay = (adjusted_time - datetime.now()).total_seconds()
        if delay <= 0:
            delay = 1
        logger.info(f"[SmartReminder] 创建任务: {task_id} 事件={task['event']} "
                    f"类型={keyword_type} 触发类型={trigger_type} 剩余{delay:.0f}秒")
        coro = self._scheduled_trigger(task_id, delay)
        self.scheduled_tasks[task_id] = asyncio.ensure_future(coro)
        return task

    async def _generate_first_ask_text(self, event_name: str, keyword_type: str) -> Optional[str]:
        prompt = (
            f"你是一个亲切的朋友。用户设置了一个提醒：{event_name}。"
            f"请生成一句简短的提醒文案，用于在到点时发送给用户。"
            f"要求：自然、亲切、30字以内。只返回文案本身。"
        )
        result = await self._call_llm("生成简短提醒文案，只返回文案本身。", prompt, temperature=0.7)
        if result:
            logger.info(f"[SmartReminder] 首次提醒文案: {result}")
        return result

    # ==================== Conversation Analysis ====================

    @filter.on_llm_response()
    # ==================== 对话分析（识别提醒需求） ====================

    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.enabled:
            return
        await self._ensure_loaded()

        # 保存会话事件用于后续主动发送
        try:
            full_session = event.session_id
            simple_session = str(event.get_session_id())
            self.session_events[simple_session] = event
            self.session_events[full_session] = event
        except Exception:
            pass

        # 获取用户原始消息
        user_message = event.message_str.strip() if event.message_str else ""
        if not user_message or len(user_message) < 3:
            return

        await self._analyze_and_create_task(user_message, event)

    def _preprocess_time_expressions(self, text: str) -> str:
        import re
        now = datetime.now()
        def replace_minutes(m):
            t = now + timedelta(minutes=int(m.group(1)))
            return t.strftime("%H:%M")
        def replace_hours(m):
            t = now + timedelta(hours=int(m.group(1)))
            return t.strftime("%H:%M")
        text = re.sub(r'(\d+)\s*分钟\s*后', replace_minutes, text)
        text = re.sub(r'(\d+)\s*小时\s*后', replace_hours, text)
        text = re.sub(r'半\s*小时\s*后', lambda m: (now + timedelta(minutes=30)).strftime("%H:%M"), text)
        text = re.sub(r'一\s*刻\s*钟\s*后', lambda m: (now + timedelta(minutes=15)).strftime("%H:%M"), text)
        return text

    async def _analyze_and_create_task(self, user_text: str, event: AstrMessageEvent):
        if not self._is_api_configured():
            return

        # 先预处理时间表达
        user_text = self._preprocess_time_expressions(user_text)

        # 先检查是否在取消任务（如果启用了自动取消）
        if self._get_config("enable_auto_cancel", True):
            await self._auto_cancel_from_context(user_text)

        # 再检查是否需要创建任务
        prompt_template = self._get_config("prompt_template", "")
        if not prompt_template:
            prompt_template = DEFAULT_PROMPT_TEMPLATE

        now = datetime.now()
        time_hint = f"当前时间是 {now.strftime('%Y-%m-%d %H:%M')}，请注意推断时间时参考当前日期。"
        self.plugin_logger.info(f"[时间解析] 用户输入: {user_text}")

        result = await self._call_llm(
            "你是一个时间解析助手。请严格按照要求的 JSON 格式返回。",
            f"{prompt_template}\n\n{time_hint}\n\n需要分析的对话：{user_text}",
            temperature=0.3
        )
        if not result:
            return

        parsed = self._parse_json_from_llm(result)
        self.plugin_logger.info(f"[时间解析] LLM 返回: {parsed}")
        logger.info(f"[SmartReminder] LLM 分析结果: {parsed}")

        if parsed.get("should_remind"):
            self.plugin_logger.info(f"[任务创建] 即将创建: {parsed.get('time')} - {parsed.get('event')}")
            await self._create_task(parsed, event)

    # ==================== Auto Cancel ====================

    # ==================== 自动取消任务 ====================

    async def _auto_cancel_from_context(self, text: str):
        active_tasks = [t for t in self.tasks if not t.get("completed") and not t.get("cancelled")]
        if not active_tasks:
            return

        prompt_template = self._get_config("prompt_template_cancel", "")
        if not prompt_template:
            prompt_template = DEFAULT_CANCEL_TEMPLATE
        if not self._is_api_configured():
            return

        tasks_summary = json.dumps([
            {"task_id": t["id"], "event": t["event"],
             "time": t.get("original_time", t["time"]), "context": t.get("context", "")}
            for t in active_tasks
        ], ensure_ascii=False, indent=2)

        prompt = prompt_template.replace("{tasks}", tasks_summary)
        result = await self._call_llm(
            "你是一个任务分析助手。请严格按照要求的 JSON 格式返回。",
            f"{prompt}\n\n用户当前消息：{text}",
            temperature=0.2
        )
        if not result:
            return

        parsed = self._parse_json_from_llm(result)
        self.plugin_logger.info(f"[时间解析] LLM 返回: {parsed}")
        logger.info(f"[SmartReminder] 取消分析结果: {parsed}")

        if parsed.get("should_cancel"):
            task_id = parsed.get("task_id", "")
            reason = parsed.get("reason", "用户取消")
            task = self._find_task(task_id)
            if task:
                logger.info(f"[SmartReminder] 自动取消任务: {task_id} 原因: {reason}")
                await self._remove_task(task_id)

    # ==================== User Reply Detection ====================

    @filter.on_llm_request()
    # ==================== 检测用户回复（标记任务完成） ====================

    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not self.enabled:
            return
        await self._ensure_loaded()

        # 保存会话事件
        try:
            full_session = event.session_id
            simple_session = str(event.get_session_id())
            self.session_events[simple_session] = event
            self.session_events[full_session] = event
        except Exception:
            return

        # 检查是否有任务在等待该会话的回复
        for task in self.tasks:
            if task.get("session_id") == simple_session and task.get("waiting_for_reply"):
                logger.info(f"[SmartReminder] 检测到用户回复, session={simple_session}, 任务={task['id']}")
                task["waiting_for_reply"] = False
                task["completed"] = True
                if task["id"] in self.re_remind_tasks:
                    self.re_remind_tasks[task["id"]].cancel()
                    del self.re_remind_tasks[task["id"]]
                await self._save_tasks()
                break

    # ==================== Commands ====================

    @filter.command("remind")
    # ==================== 指令处理（/remind） ====================

    async def remind(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        await self._ensure_loaded()

        msg = event.message_str.strip()
        parts = msg.split()
        sub_cmd = parts[1] if len(parts) > 1 else ""

        session_id = ""
        try:
            session_id = str(event.get_session_id())
        except Exception:
            pass

        if sub_cmd == "list":
            await self._cmd_list(event, session_id)
        elif sub_cmd == "cancel" and len(parts) > 2:
            await self._cmd_cancel(event, parts[2])
        elif sub_cmd == "clear":
            await self._cmd_clear(event, session_id)
        else:
            help_text = (
                "[SmartReminder] 指令帮助:\n"
                "/remind list - 查看所有待执行的任务\n"
                "/remind cancel <任务ID> - 取消指定任务\n"
                "/remind clear - 清空所有任务"
            )
            await event.send(help_text)

    async def _cmd_list(self, event: AstrMessageEvent, session_id: str = ""):
        active = [t for t in self.tasks if not t.get("completed") and not t.get("cancelled")]
        if not active:
            await event.send("[SmartReminder] 当前没有待执行的任务")
            return

        lines = ["[SmartReminder] 当前任务列表:"]
        for t in active:
            icon = "⭐" if t.get("is_important") else "📌"
            status = " [等待回复]" if t.get("waiting_for_reply") else ""
            lines.append(
                f"  {icon} ID:{t['id']} | {t.get('event','')} | {t.get('time','')} | "
                f"类型:{t.get('keyword_type','normal')} | 重试:{t.get('retry_count',0)}{status}"
            )
        await event.send("\n".join(lines))

    async def _cmd_cancel(self, event: AstrMessageEvent, task_id: str):
        task = self._find_task(task_id)
        if not task:
            await event.send(f"[SmartReminder] 未找到任务 {task_id}")
            return
        await self._remove_task(task_id)
        await event.send(f"[SmartReminder] 已取消任务 {task_id}: {task.get('event','')}")

    async def _cmd_clear(self, event: AstrMessageEvent, session_id: str = ""):
        count = 0
        for task in list(self.tasks):
            if not task.get("completed") and not task.get("cancelled"):
                await self._remove_task(task["id"])
                count += 1
        await event.send(f"[SmartReminder] 已清空 {count} 个任务")

    # ==================== Lifecycle ====================

    # ==================== 插件生命周期 ====================

    async def terminate(self):
        for task in self.scheduled_tasks.values():
            task.cancel()
        for task in self.re_remind_tasks.values():
            task.cancel()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self.session_events.clear()
        logger.info("[SmartReminder] 插件已终止")
