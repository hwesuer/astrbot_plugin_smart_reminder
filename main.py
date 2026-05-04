import json
import asyncio
import random
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, Optional

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

try:
    from astrbot_plugin_smart_debounce.main import global_skip_ids as debounce_skip_ids
except ImportError:
    debounce_skip_ids = None

# ── 预检查关键词：消息中至少包含其一才调用 LLM，节省 API 费用 ──
_TIME_KEYWORDS = [
    "分钟", "小时", "点", "提醒", "明天", "后天", "下周", "下个月",
    "上午", "下午", "晚上", "明早", "今早", "今晚", "明晚",
    "过会", "一会", "等下", "马上", "一会儿", "半小时",
    "点钟", "点半", "刻", "天之后", "天后", "小时后", "分钟后",
    "半小", "一刻",
]
_TIME_PATTERN = re.compile(r'\d{1,2}:\d{2}')


def _has_time_hint(text: str) -> bool:
    """检查消息是否包含时间相关的关键词或 HH:MM 格式"""
    if any(kw in text for kw in _TIME_KEYWORDS):
        return True
    if _TIME_PATTERN.search(text):
        return True
    return False

# ── 独立日志 ──
_logger: Optional[logging.Logger] = None


def _init_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("SmartReminder")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.join(plugin_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "smart_reminder.log")
        handler = logging.FileHandler(log_path, encoding="utf-8", mode="a")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    _logger = logger
    return logger


def _get_data_dir() -> str:
    """获取数据目录，优先 cwd/data，其次插件目录/data"""
    cwd = os.getcwd()
    data_dir = os.path.join(cwd, "data")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except PermissionError:
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(plugin_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
    return data_dir


@register("astrbot_plugin_smart_reminder", "YourName", "清爽版智能定时提醒", "1.0.2")
class SmartReminderClean(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.logger = _init_logger()
        self.tasks: Dict[str, Dict] = {}
        self.scheduled: Dict[str, asyncio.Task] = {}
        self._delayed_tasks: Dict[str, asyncio.Task] = {}
        self.session_events: Dict[str, AstrMessageEvent] = {}
        self.data_path = os.path.join(_get_data_dir(), "smart_reminder.json")
        self._loaded = False
        self._lock = asyncio.Lock()
        self.logger.info("=== SmartReminder 已初始化 ===")

    # ═══════════════ 生命周期 ═══════════════

    async def ensure_loaded(self):
        if not self._loaded:
            async with self._lock:
                if not self._loaded:
                    self._load()
                    self._loaded = True

    def _load(self):
        """从 JSON 文件加载任务并恢复调度"""
        try:
            if os.path.exists(self.data_path):
                with open(self.data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.tasks = data.get("tasks", {})
                self.logger.info(f"从文件加载了 {len(self.tasks)} 个任务")

                if not self.config.get("restore_tasks_on_startup", True):
                    self.logger.info("restore_tasks_on_startup=false，丢弃已加载任务")
                    self.tasks = {}
                    self._save()
                    return

                now = datetime.now()
                expire_days = self.config.get("task_expire_days", 7)
                expire_deadline = now - timedelta(days=expire_days)
                overdue_threshold = self.config.get("overdue_trigger_seconds", 300)
                expired_ids = []

                for tid, t in list(self.tasks.items()):
                    try:
                        target = datetime.strptime(t["time"], "%Y-%m-%d %H:%M")
                        if target < expire_deadline:
                            expired_ids.append(tid)
                            continue
                        if t.get("stopped"):
                            continue
                        # 逾时太久（超过阈值）静默删除，避免启动时消息轰炸
                        late_seconds = (now - target).total_seconds()
                        if late_seconds > overdue_threshold:
                            self.logger.info(f"任务 {tid} 已逾时 {late_seconds:.0f}s（>{overdue_threshold}s），静默丢弃")
                            expired_ids.append(tid)
                            continue
                        # 轻微逾时或未来任务，重新调度（delay=1 立即触发）
                        self._schedule(t)
                    except Exception as e:
                        self.logger.error(f"恢复任务 {tid} 失败: {e}")
                        expired_ids.append(tid)

                for tid in expired_ids:
                    if tid in self.tasks:
                        del self.tasks[tid]
                if expired_ids:
                    self._save()
                    self.logger.info(f"清理了 {len(expired_ids)} 个过期/无效任务")

                self.logger.info(f"已恢复调度 {sum(1 for t in self.tasks.values() if not t.get('stopped'))} 个任务")
        except Exception as e:
            self.logger.error(f"加载任务失败: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump({"tasks": self.tasks}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"保存任务失败: {e}")

    # ═══════════════ LLM ═══════════════

    def _is_api_configured(self) -> bool:
        return bool(self.config.get("api_base") and self.config.get("api_key") and self.config.get("model"))

    async def _call_llm(self, system: str, user: str) -> Optional[str]:
        if not self._is_api_configured():
            return None
        base = self.config.get("api_base").rstrip("/")
        key = self.config.get("api_key")
        model = self.config.get("model")
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{base}/chat/completions",
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": 0.3,
                    },
                    headers={"Authorization": f"Bearer {key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                else:
                    self.logger.error(f"LLM 返回 {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                self.logger.error(f"LLM 调用异常: {e}")
        return None

    async def _get_persona(self) -> str:
        try:
            config_path = os.path.join(os.getcwd(), "data", "cmd_config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
                pool = cfg.get("persona_pool", [])
                if pool and len(pool) > 0:
                    return pool[0].get("prompt", "")
        except Exception as e:
            self.logger.error(f"读取人格设定失败: {e}")
        return "你是一个亲切的助手。"

    def _clean_llm_output(self, text: str) -> str:
        """清洗 LLM 输出：去除 markdown、函数调用包裹、多余引号"""
        if not text:
            return ""
        text = text.strip()
        text = re.sub(r'```[\w]*\n?', '', text)
        text = text.strip('`')
        m = re.search(r'send_\w+\s*\(\s*["\'](.+?)["\']\s*\)', text, re.DOTALL)
        if m:
            return m.group(1)
        if (text.startswith('"') and text.endswith('"')) or \
           (text.startswith("'") and text.endswith("'")):
            text = text[1:-1]
        return text.strip()

    # ═══════════════ 时间处理 ═══════════════

    def _preprocess_time_expressions(self, text: str) -> str:
        """将常见相对时间表述转为 HH:MM，降低 LLM 负担"""
        now = datetime.now()
        text = re.sub(
            r'(\d+)\s*分钟后',
            lambda m: (now + timedelta(minutes=int(m.group(1)))).strftime("%H:%M"),
            text,
        )
        text = re.sub(
            r'(\d+)\s*小时后',
            lambda m: (now + timedelta(hours=int(m.group(1)))).strftime("%H:%M"),
            text,
        )
        text = re.sub(r'半\s*小?时后', lambda _: (now + timedelta(minutes=30)).strftime("%H:%M"), text)
        text = re.sub(r'一\s*刻\s*(钟)?后', lambda _: (now + timedelta(minutes=15)).strftime("%H:%M"), text)
        return text

    def _parse_time(self, time_str: str, now: datetime) -> Optional[datetime]:
        """解析时间字符串，支持完整日期时间或仅 HH:MM"""
        if not time_str:
            return None
        time_str = time_str.strip()
        # 完整日期时间格式
        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S"]:
            try:
                return datetime.strptime(time_str, fmt)
            except ValueError:
                continue
        # 仅 HH:MM → 分配给今天或明天
        try:
            hour_min = datetime.strptime(time_str, "%H:%M")
            t = now.replace(hour=hour_min.hour, minute=hour_min.minute, second=0, microsecond=0)
            if t < now:
                t += timedelta(days=1)
            return t
        except ValueError:
            pass
        return None

    def _format_time(self, time_str: str, now: datetime) -> Optional[str]:
        dt = self._parse_time(time_str, now)
        return dt.strftime("%Y-%m-%d %H:%M") if dt else None

    # ═══════════════ 任务管理 ═══════════════

    async def _create_task(
        self,
        time_str: str,
        event_str: str,
        session_id: str,
        context: str = "",
        is_important: bool = False,
        platform: str = "",
        message_type: str = "",
        self_id: str = "",
        group_id: str = "",
    ) -> Optional[str]:
        """创建新提醒任务并调度"""
        async with self._lock:
            max_tasks = self.config.get("max_tasks", 20)
            if len(self.tasks) >= max_tasks:
                self.logger.warning(f"任务数已达上限 {max_tasks}")
                return None

            now = datetime.now()
            target = self._parse_time(time_str, now)
            if not target:
                self.logger.error(f"无法解析时间: {time_str}")
                return None
            time_formatted = target.strftime("%Y-%m-%d %H:%M")
            if target < now:
                self.logger.warning(f"时间已过: {time_formatted}")
                return None
            # 时间合理性检查：拒绝过远未来的时间（LLM 幻觉常见模式）
            max_days = self.config.get("max_remind_days", 90)
            if (target - now).days > max_days:
                self.logger.warning(f"时间过远（>{max_days}天），可能为 LLM 幻觉: {time_formatted}")
                return None

            task_id = f"t{int(datetime.now().timestamp() * 1000)}"
            use_ai = self.config.get("use_first_reminder_ai", False)
            first_ask = (
                self.config.get("first_reminder_prompt_template", "").replace("{event}", event_str)
                if use_ai
                else self.config.get("default_ask_text", "到点了~")
            )

            task = {
                "id": task_id,
                "time": time_formatted,
                "event": event_str,
                "session_id": session_id,
                "context": context,
                "created": datetime.now().isoformat(),
                "retry": 0,
                "max_retry": self.config.get("re_remind_max_count", 3),
                "first_ask": first_ask,
                "is_important": is_important,
                "platform": platform,
                "message_type": message_type,
                "self_id": self_id,
                "group_id": group_id,
                "stopped": False,
            }
            self.tasks[task_id] = task
            self._save()
            self._schedule(task)
            self.logger.info(f"创建任务 {task_id}: {time_formatted} - {event_str}")
            return task_id

    def _schedule(self, task: Dict):
        """调度一个任务到目标时间触发"""
        tid = task["id"]
        if tid in self.scheduled:
            self.scheduled[tid].cancel()
            del self.scheduled[tid]

        try:
            target = datetime.strptime(task["time"], "%Y-%m-%d %H:%M")
            now = datetime.now()
            delay = (target - now).total_seconds()
            if delay < 0:
                delay = 1  # 已过期，立即触发

            async def sleeper(d):
                try:
                    await asyncio.sleep(d)
                    await self._trigger(tid)
                finally:
                    async with self._lock:
                        self.scheduled.pop(tid, None)

            self.scheduled[tid] = asyncio.create_task(sleeper(delay))
            self.logger.info(f"已调度任务 {tid}，{delay:.0f} 秒后触发")
        except Exception as e:
            self.logger.error(f"调度任务 {tid} 失败: {e}")

    async def _trigger(self, task_id: str):
        """触发提醒：加锁读任务 → LLM 生成 → 加锁重验 → 发送 → 决定是否重复"""
        # ── Phase 1: snapshot task state under lock ──
        async with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.get("stopped"):
                return
            task["retry"] = task.get("retry", 0) + 1
            # 快照关键字段，避免后续使用已释放锁的 task 引用
            current_retry = task["retry"]
            max_retry = task.get("max_retry", 3)
            event_str = task["event"]
            first_ask = task["first_ask"]
            use_first_ai = self.config.get("use_first_reminder_ai", False)
            use_llm_ask = self.config.get("use_llm_ask", True)
            enable_re = self.config.get("enable_re_remind", True)

        if current_retry > max_retry:
            await self._remove_task(task_id)
            return

        # ── Phase 2: generate text outside lock (may call LLM) ──
        text = first_ask
        if current_retry == 1 and use_first_ai:
            persona = await self._get_persona()
            prompt = f"事件：{event_str}\n请生成一句简短的提醒，不要函数调用、不要引号，纯文本，30字以内。"
            resp = await self._call_llm(persona, prompt)
            if resp:
                text = self._clean_llm_output(resp) or text
        elif current_retry > 1 and use_llm_ask and self._is_api_configured():
            persona = await self._get_persona()
            template = self.config.get("ask_style_template", "")
            if template:
                template = template.replace("{event}", event_str)
                template = template.replace("{retry_count}", str(current_retry))
                template = template.replace("{trigger_type}", "准时提醒" if current_retry == 1 else "重复提醒")
                resp = await self._call_llm(persona, template)
                if resp:
                    text = self._clean_llm_output(resp) or text

        # ── Phase 3: re-verify task exists & not stopped (may have been cancelled during LLM) ──
        async with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.get("stopped"):
                self.logger.info(f"任务 {task_id} 在 LLM 生成期间被取消，中止触发")
                return

        # ── Phase 4: send ──
        await self._send_reminder(task, text)

        # ── Phase 5: repeat or remove ──
        if enable_re and current_retry < max_retry:
            interval = random.randint(
                self.config.get("re_remind_interval_min", 60),
                self.config.get("re_remind_interval_max", 600),
            )
            repeat_key = f"{task_id}_repeat"

            async def delayed():
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return
                finally:
                    self._delayed_tasks.pop(repeat_key, None)
                async with self._lock:
                    if task_id not in self.tasks or self.tasks[task_id].get("stopped"):
                        return
                await self._trigger(task_id)

            delayed_task = asyncio.create_task(delayed())
            async with self._lock:
                self._delayed_tasks[repeat_key] = delayed_task
        else:
            await self._remove_task(task_id)

    async def _remove_task(self, task_id: str):
        """删除任务及其所有关联的异步任务（scheduled + delayed）"""
        async with self._lock:
            if task_id in self.scheduled:
                self.scheduled[task_id].cancel()
                del self.scheduled[task_id]
            repeat_key = f"{task_id}_repeat"
            if repeat_key in self._delayed_tasks:
                self._delayed_tasks[repeat_key].cancel()
                del self._delayed_tasks[repeat_key]
            if task_id in self.tasks:
                del self.tasks[task_id]
                self._save()
                self.logger.info(f"任务 {task_id} 已移除")

    async def _cancel_task(self, task_id: str, session_id: str = "") -> bool:
        """取消指定任务（可选校验 session_id）"""
        async with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            if session_id and task["session_id"] != session_id:
                return False
            task["stopped"] = True
            if task_id in self.scheduled:
                self.scheduled[task_id].cancel()
                del self.scheduled[task_id]
            repeat_key = f"{task_id}_repeat"
            if repeat_key in self._delayed_tasks:
                self._delayed_tasks[repeat_key].cancel()
                del self._delayed_tasks[repeat_key]
            del self.tasks[task_id]
            self._save()
            self.logger.info(f"已取消任务 {task_id}")
            return True

    async def _cancel_all_session_tasks(self, session_id: str) -> int:
        """取消某个会话下的所有任务"""
        cnt = 0
        async with self._lock:
            for tid in list(self.tasks.keys()):
                t = self.tasks.get(tid)
                if t and t["session_id"] == session_id:
                    if tid in self.scheduled:
                        self.scheduled[tid].cancel()
                        del self.scheduled[tid]
                    repeat_key = f"{tid}_repeat"
                    if repeat_key in self._delayed_tasks:
                        self._delayed_tasks[repeat_key].cancel()
                        del self._delayed_tasks[repeat_key]
                    del self.tasks[tid]
                    cnt += 1
            if cnt:
                self._save()
        self.logger.info(f"取消会话 {session_id} 的 {cnt} 个任务")
        return cnt

    def _get_session_tasks(self, session_id: str) -> list:
        return [t for t in self.tasks.values() if t["session_id"] == session_id]

    # ═══════════════ 消息发送 ═══════════════

    async def _send_reminder(self, task: Dict, text: str):
        """通过 StarTools 发送提醒（绕过拦截链）"""
        session_id = task["session_id"]
        event = self.session_events.get(session_id)

        from astrbot.core.message.components import Plain
        from astrbot.core.star.star_tools import StarTools

        try:
            if event:
                new_msg = await StarTools.create_message(
                    type=str(event.message_obj.type.value),
                    self_id=event.get_self_id(),
                    session_id=event.session_id,
                    sender=event.message_obj.sender,
                    message=[Plain(text)],
                    message_str=text,
                    group_id=event.get_group_id() or "",
                )
                if debounce_skip_ids is not None:
                    debounce_skip_ids.add(new_msg.message_id)
                await StarTools.create_event(
                    abm=new_msg, platform=event.get_platform_name(), is_wake=True
                )
            else:
                # 降级：使用任务中存储的元数据尝试重建消息
                platform = task.get("platform", "")
                msg_type = task.get("message_type", "1")
                self_id = task.get("self_id", "")
                group_id = task.get("group_id", "")
                new_msg = await StarTools.create_message(
                    type=msg_type,
                    self_id=self_id,
                    session_id=session_id,
                    sender=None,
                    message=[Plain(text)],
                    message_str=text,
                    group_id=group_id,
                )
                if debounce_skip_ids is not None:
                    debounce_skip_ids.add(new_msg.message_id)
                await StarTools.create_event(
                    abm=new_msg, platform=platform, is_wake=True
                )
            self.logger.info(f"已发送提醒: {text}")
        except Exception as e:
            self.logger.error(f"发送提醒失败: {e}")

    async def _reply(self, event: AstrMessageEvent, text: str):
        """向用户发送回复消息"""
        from astrbot.core.message.components import Plain
        from astrbot.core.star.star_tools import StarTools

        try:
            m = await StarTools.create_message(
                type=str(event.message_obj.type.value),
                self_id=event.get_self_id(),
                session_id=event.session_id,
                sender=event.message_obj.sender,
                message=[Plain(text)],
                message_str=text,
                group_id=event.get_group_id() or "",
            )
            await StarTools.create_event(abm=m, platform=event.get_platform_name(), is_wake=False)
        except Exception as e:
            self.logger.error(f"回复消息失败: {e}")

    # ═══════════════ 事件处理 ═══════════════

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """拦截用户消息，解析提醒意图或命令"""
        await self.ensure_loaded()

        if not self.config.get("enabled", True):
            return

        # 保存会话 event，供后续主动发送提醒时使用
        self.session_events[event.session_id] = event

        user_text = (event.get_plain_text() or "").strip()
        if not user_text:
            return

        # 命令优先
        if user_text.startswith("/remind"):
            await self._handle_command(user_text, event)
            return

        if not self._is_api_configured():
            return

        # 关键词预筛 + HH:MM 时间格式预检：避免无意义的 LLM 调用
        if not _has_time_hint(user_text):
            return

        await self._parse_message(user_text, event)

    async def _handle_command(self, text: str, event: AstrMessageEvent):
        """处理 /remind 系列命令"""
        parts = text.split()
        cmd = parts[1].lower() if len(parts) > 1 else ""

        if cmd in ("", "help"):
            await self._reply(
                event,
                "📋 智能提醒插件命令\n"
                "/remind list       — 查看当前提醒\n"
                "/remind cancel <ID> — 取消指定提醒\n"
                "/remind cancel all  — 取消所有提醒\n"
                "/remind help        — 帮助信息",
            )
        elif cmd == "list":
            tasks = self._get_session_tasks(event.session_id)
            if not tasks:
                await self._reply(event, "当前没有待处理的提醒。")
                return
            lines = ["📋 当前提醒"]
            for t in tasks:
                status = "已停" if t.get("stopped") else "运行中"
                lines.append(f"[{t['id']}] {t['time']} — {t['event']} ({status})")
            await self._reply(event, "\n".join(lines))
        elif cmd == "cancel":
            if len(parts) < 3:
                await self._reply(event, "用法：/remind cancel <任务ID> 或 /remind cancel all")
                return
            target = parts[2]
            if target.lower() == "all":
                cnt = await self._cancel_all_session_tasks(event.session_id)
                await self._reply(event, f"已取消 {cnt} 个提醒。")
            else:
                ok = await self._cancel_task(target, event.session_id)
                await self._reply(event, f"已取消 {target}。" if ok else f"未找到任务 {target} 或无权操作。")
        else:
            await self._reply(event, f"未知命令: {cmd}，使用 /remind help 查看帮助。")

    async def _parse_message(self, user_text: str, event: AstrMessageEvent):
        """使用 LLM 解析用户消息：检测新建提醒或取消提醒"""
        now = datetime.now()
        processed = self._preprocess_time_expressions(user_text)

        # 已有任务列表（供取消检测使用）
        session_tasks = self._get_session_tasks(event.session_id)
        tasks_json = "无" if not session_tasks else json.dumps(
            [{"id": t["id"], "time": t["time"], "event": t["event"]} for t in session_tasks],
            ensure_ascii=False,
        )

        # 使用配置文件中的系统提示词
        system = self.config.get("prompt_template", "")
        if not system:
            # fallback 内置提示词
            system = (
                "你是一个严格的时间提醒解析助手。\n"
                "仅在用户明确要求设置/创建提醒时返回 should_remind:true。\n"
                "以下情况必须返回 should_remind=false：\n"
                "- 用户只是在讨论时间表、计划，而非请求提醒\n"
                "- 用户说\"不提醒\"\"不用了\"\"不需要\"等否定\n"
                "- 用户闲聊提到时间但未要求提醒\n"
                "仅在用户请求取消已有提醒时返回 should_cancel:true。\n"
                "当前已有任务：{tasks}\n"
                "当前时间：{time_hint}\n"
                "返回 JSON:\n"
                "新建：{{\"should_remind\":true,\"time\":\"YYYY-MM-DD HH:MM\",\"event\":\"事件简述\",\"is_important\":false}}\n"
                "取消：{{\"should_cancel\":true,\"task_id\":\"匹配ID\",\"reason\":\"原因\"}}\n"
                "无操作：{{\"should_remind\":false,\"should_cancel\":false}}\n"
                "只返回 JSON，无其他内容。"
            )
        system = system.replace("{tasks}", tasks_json)
        system = system.replace("{time_hint}", now.strftime("%Y-%m-%d %H:%M"))

        user_msg = f"用户消息：{processed}"
        llm_out = await self._call_llm(system, user_msg)
        if not llm_out:
            return

        # 从 LLM 返回中提取 JSON
        try:
            obj_start = llm_out.find("{")
            obj_end = llm_out.rfind("}")
            if obj_start < 0 or obj_end <= obj_start:
                self.logger.warning(f"LLM 未返回 JSON: {llm_out[:200]}")
                await self._reply(event, "抱歉，我没能理解你设置的提醒，请换一种方式描述时间和事件。")
                return
            json_str = llm_out[obj_start : obj_end + 1]
            data = json.loads(json_str)
        except Exception as e:
            self.logger.error(f"LLM JSON 解析失败: {e} | {llm_out[:200]}")
            await self._reply(event, "抱歉，我没能解析你的提醒，请重新描述一下时间。")
            return

        # ── 取消已有提醒 ──
        if data.get("should_cancel") and self.config.get("enable_auto_cancel", True):
            task_id = data.get("task_id", "")
            if task_id:
                ok = await self._cancel_task(task_id, event.session_id)
                if ok:
                    await self._reply(event, f"已自动取消提醒 {task_id}。")
                    return  # 取消成功则不再检查新建提醒

        # ── 新建提醒 ──
        if not data.get("should_remind"):
            return

        time_str = data.get("time", "")
        event_str = (data.get("event") or "").strip()

        if not event_str:
            self.logger.warning("LLM 返回事件为空，跳过")
            await self._reply(event, "抱歉，我没能识别出你提的具体事件，请重新描述一下。")
            return

        formatted = self._format_time(time_str, now)
        if not formatted:
            self.logger.warning(f"无法解析 LLM 返回的时间: {time_str}")
            await self._reply(event, f"抱歉，我没能理解你设置的时间「{time_str}」，请用更明确的格式。")
            return

        task_id = await self._create_task(
            formatted,
            event_str,
            event.session_id,
            context=user_text,
            is_important=data.get("is_important", False),
            platform=event.get_platform_name(),
            message_type=str(event.message_obj.type.value),
            self_id=event.get_self_id(),
            group_id=event.get_group_id() or "",
        )

        if task_id:
            await self._reply(event, f"好的，已为你设置提醒：{formatted} {event_str}")

    async def terminate(self):
        async with self._lock:
            for t in list(self.scheduled.values()):
                t.cancel()
            self.scheduled.clear()
            for t in list(self._delayed_tasks.values()):
                t.cancel()
            self._delayed_tasks.clear()
        self._save()
        self.logger.info("插件已终止，所有异步任务已取消")
