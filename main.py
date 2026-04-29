import json
import asyncio
import re
import random
import os
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_smart_reminder", "插件作者", "智能定时提醒插件 - 实时分析对话并自动创建定时任务", "1.0.0")
class SmartReminder(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.tasks: List[Dict] = []
        self.scheduled_tasks: Dict[str, asyncio.Task] = {}
        self.waiting_replies: Dict[str, str] = {}
        self.re_remind_tasks: Dict[str, asyncio.Task] = {}
        self.data_dir = os.path.join(os.getcwd(), "data")
        self.data_file = os.path.join(self.data_dir, "astrbot_plugin_smart_reminder_data.json")
        self._http_client: Optional[httpx.AsyncClient] = None
        self._loaded = False

    async def _ensure_loaded(self):
        if not self._loaded:
            await self._load_tasks()
            self._loaded = True

    def _get_config(self, key: str, default=None):
        val = getattr(self.config, key, None)
        if val is None:
            return default
        return val

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
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
        return {}

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

    async def _load_tasks(self):
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
        before_count = len(self.tasks)
        self.tasks = [
            t for t in self.tasks
            if not (t.get("completed") or t.get("cancelled"))
            or datetime.fromisoformat(t.get("created_at", datetime.now().isoformat())) > cutoff
        ]
        removed = before_count - len(self.tasks)
        if removed > 0:
            logger.info(f"[SmartReminder] 清理了 {removed} 个过期任务")

    # ==================== Scheduling ====================

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

    async def _on_task_trigger(self, task_id: str):
        task = self._find_task(task_id)
        if not task:
            logger.error(f"[SmartReminder] 任务 {task_id} 不存在")
            return
        if task.get("completed") or task.get("cancelled"):
            return

        logger.info(f"[SmartReminder] ===== 任务触发: {task_id} 事件: {task.get('event')} =====")

        keyword_type = task.get("keyword_type", "normal")
        enable_complete_forget = self._get_config("enable_complete_forget", False)
        complete_forget_prob = self._get_config("complete_forget_probability", 5)
        casual_boost = self._get_config("casual_keyword_boost", 25)

        if keyword_type == "precise":
            logger.info(f"[SmartReminder] 任务 {task_id} 为准时类型，跳过完全遗忘检查")
        elif keyword_type == "casual":
            effective_prob = min(100, complete_forget_prob + casual_boost)
        else:
            effective_prob = complete_forget_prob

        if keyword_type != "precise" and enable_complete_forget and random.randint(1, 100) <= effective_prob:
            logger.info(f"[SmartReminder] 完全遗忘触发: 任务 {task_id} ({task.get('event')}) 将被删除不触发")
            await self._remove_task(task_id)
            return

        retry_count = task.get("retry_count", 0)

        if retry_count == 0:
            ask_text = task.get("first_ask_text") or self._get_config("default_ask_text", "到点了~")
        else:
            ask_text = await self._generate_ask_text(task, retry_count)

        if ask_text:
            await self._send_message(task, ask_text)

        task["retry_count"] = retry_count + 1
        await self._save_tasks()

        max_count = self._get_config("re_remind_max_count", 3)
        if keyword_type == "precise":
            actual_max = max(1, max_count + random.randint(0, 1))
            logger.info(f"[SmartReminder] 准时任务重复提醒次数: {actual_max}")
        else:
            actual_max = max(1, max_count + random.randint(-1, 1))
            logger.info(f"[SmartReminder] 普通任务重复提醒次数: {actual_max}")

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
            logger.info(f"[SmartReminder] 任务 {task_id} 完成 (提醒次数已达上限或重提醒已禁用)")

    async def _generate_ask_text(self, task: Dict, retry_count: int) -> Optional[str]:
        use_llm = self._get_config("use_llm_ask", True)
        if not use_llm or not self._is_api_configured():
            return self._get_config("default_ask_text", "到点了~")

        template = self._get_config("ask_style_template", "")
        if not template:
            return self._get_config("default_ask_text", "到点了~")

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

    async def _send_message(self, task: Dict, text: str):
        session_id = task.get("session_id", "")
        logger.info(f"[SmartReminder] 发送提醒 -> session={session_id} 内容={text}")
        try:
            if hasattr(self.context, "send_message"):
                await self.context.send_message(session_id, text)
            else:
                logger.error("[SmartReminder] context 没有 send_message 方法")
        except Exception as e:
            logger.error(f"[SmartReminder] 发送消息失败: {e}")

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
            logger.info(f"[SmartReminder] 超时未回复，进入重复提醒, 任务={task_id}")
            task["waiting_for_reply"] = False
            await self._save_tasks()
            await self._on_task_trigger(task_id)

    # ==================== Task Management ====================

    def _find_task(self, task_id: str) -> Optional[Dict]:
        for t in self.tasks:
            if t.get("id") == task_id:
                return t
        return None

    def _find_tasks_by_session(self, session_id: str) -> List[Dict]:
        return [t for t in self.tasks if t.get("session_id") == session_id and not t.get("cancelled")]

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
        active_count = sum(1 for t in self.tasks if not t.get("completed", False) and not t.get("cancelled", False))
        if active_count >= max_tasks:
            logger.warning(f"[SmartReminder] 任务数已达上限 ({max_tasks})，拒绝创建")
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
        if delta < 0:
            trigger_type = "提前提醒"
        elif delta > 0:
            trigger_type = "推迟提醒"
        else:
            trigger_type = "准时提醒"

        task_id = str(uuid.uuid4())[:8]
        session_id = ""
        if event:
            try:
                session_id = str(event.get_session_id())
            except Exception:
                pass

        first_ask_text = None
        if self._get_config("use_llm_ask", True) and self._is_api_configured():
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
        logger.info(f"[SmartReminder] 创建任务: {task_id} 事件={task['event']} 时间={task['time']} "
                    f"类型={keyword_type} 触发类型={trigger_type} 剩余{delay:.0f}秒")
        coro = self._scheduled_trigger(task_id, delay)
        self.scheduled_tasks[task_id] = asyncio.ensure_future(coro)
        return task

    async def _generate_first_ask_text(self, event: str, keyword_type: str) -> Optional[str]:
        prompt = (
            f"你是一个亲切的朋友。"
            f"用户设置了一个提醒：{event}。"
            f"请生成一句简短的提醒文案，用于在到点时发送给用户。"
            f"要求：自然、亲切、30字以内。只返回文案本身。"
        )
        result = await self._call_llm("生成简短提醒文案，只返回文案本身。", prompt, temperature=0.7)
        if result:
            logger.info(f"[SmartReminder] 首次提醒文案: {result}")
        return result

    # ==================== LLM Response Analysis ====================

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.enabled:
            return
        await self._ensure_loaded()
        await self._analyze_and_create_task(event)

    async def _analyze_and_create_task(self, event: AstrMessageEvent):
        if not self._is_api_configured():
            return
        try:
            user_text = event.get_message_str() or ""
        except Exception:
            user_text = ""
        if not user_text or len(user_text.strip()) < 3:
            return

        await self._auto_cancel_from_context(user_text)

        prompt_template = self._get_config("prompt_template", "")
        if not prompt_template:
            return

        now = datetime.now()
        time_hint = f"当前时间是 {now.strftime('%Y-%m-%d %H:%M')}，请注意推断时间时参考当前日期。"

        result = await self._call_llm(
            "你是一个时间解析助手。请严格按照要求的 JSON 格式返回。",
            f"{prompt_template}\n\n{time_hint}\n\n需要分析的对话：{user_text}",
            temperature=0.3
        )
        if not result:
            return

        parsed = self._parse_json_from_llm(result)
        logger.info(f"[SmartReminder] LLM 分析结果: {parsed}")

        if parsed.get("should_remind"):
            await self._create_task(parsed, event)

    # ==================== Auto Cancel ====================

    async def _auto_cancel_from_context(self, text: str):
        active_tasks = [t for t in self.tasks if not t.get("completed") and not t.get("cancelled")]
        if not active_tasks:
            return

        prompt_template = self._get_config("prompt_template_cancel", "")
        if not prompt_template or not self._is_api_configured():
            return

        tasks_summary = json.dumps([
            {"task_id": t["id"], "event": t["event"], "time": t.get("original_time", t["time"]), "context": t.get("context", "")}
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
    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not self.enabled:
            return
        await self._ensure_loaded()
        try:
            session_id = str(event.get_session_id())
        except Exception:
            return
        for task in self.tasks:
            if task.get("session_id") == session_id and task.get("waiting_for_reply"):
                logger.info(f"[SmartReminder] 检测到用户回复, session={session_id}, 任务={task['id']}")
                task["waiting_for_reply"] = False
                task["completed"] = True
                if task["id"] in self.re_remind_tasks:
                    self.re_remind_tasks[task["id"]].cancel()
                    del self.re_remind_tasks[task["id"]]
                await self._save_tasks()
                break

    # ==================== Commands ====================

    @filter.command("remind")
    async def remind(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        await self._ensure_loaded()

        try:
            msg = event.get_message_str() or ""
        except Exception:
            msg = ""
        parts = msg.strip().split()
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
            await self._send_cmd_help(event)

    async def _send_cmd_help(self, event: AstrMessageEvent):
        help_text = (
            "[SmartReminder] 指令帮助:\n"
            "/remind list — 查看所有待执行的定时任务\n"
            "/remind cancel <任务ID> — 取消指定任务\n"
            "/remind clear — 清空所有任务"
        )
        await self._send_message_to_event(event, help_text)

    async def _cmd_list(self, event: AstrMessageEvent, session_id: str = ""):
        active = [t for t in self.tasks if not t.get("completed") and not t.get("cancelled")]
        if not active:
            await self._send_message_to_event(event, "[SmartReminder] 当前没有待执行的任务")
            return

        lines = ["[SmartReminder] 当前任务列表:"]
        for t in active:
            icon = "⭐" if t.get("is_important") else "📌"
            status = ""
            if t.get("waiting_for_reply"):
                status = " [等待回复]"
            lines.append(
                f"  {icon} ID:{t['id']} | {t.get('event','')} | {t.get('time','')} | "
                f"类型:{t.get('keyword_type','normal')} | 重试:{t.get('retry_count',0)}{status}"
            )
        await self._send_message_to_event(event, "\n".join(lines))

    async def _cmd_cancel(self, event: AstrMessageEvent, task_id: str):
        task = self._find_task(task_id)
        if not task:
            await self._send_message_to_event(event, f"[SmartReminder] 未找到任务 {task_id}")
            return
        await self._remove_task(task_id)
        await self._send_message_to_event(event, f"[SmartReminder] 已取消任务 {task_id}: {task.get('event','')}")

    async def _cmd_clear(self, event: AstrMessageEvent, session_id: str = ""):
        count = 0
        for task in list(self.tasks):
            if not task.get("completed") and not task.get("cancelled"):
                await self._remove_task(task["id"])
                count += 1
        await self._send_message_to_event(event, f"[SmartReminder] 已清空 {count} 个任务")

    async def _send_message_to_event(self, event: AstrMessageEvent, text: str):
        try:
            if hasattr(event, "send"):
                await event.send(text)
            elif hasattr(self.context, "send_message"):
                session_id = ""
                try:
                    session_id = str(event.get_session_id())
                except Exception:
                    pass
                await self.context.send_message(session_id, text)
            else:
                logger.info(f"[SmartReminder] 无法发送消息: {text}")
        except Exception as e:
            logger.error(f"[SmartReminder] 发送消息失败: {e}")

    # ==================== Lifecycle ====================

    async def terminate(self):
        for task_id, task in self.scheduled_tasks.items():
            task.cancel()
        for task_id, task in self.re_remind_tasks.items():
            task.cancel()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("[SmartReminder] 插件已终止")
