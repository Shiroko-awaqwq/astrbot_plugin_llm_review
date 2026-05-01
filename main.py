import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register("astrbot_plugin_moderation", "Your Name", "消息审核插件", "1.0.0")
class ModerationPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # 监听所有群聊消息
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        # 获取插件配置
        enabled = self.get_config().get("enabled", True)
        if not enabled:
            return

        # 获取消息文本内容
        message_text = event.message_str
        if not message_text or not message_text.strip():
            return

        # 忽略机器人自己发送的消息
        if self.get_config().get("ignore_bot_self", True):
            if getattr(event.message_obj, "self_id", None) == event.get_sender_id():
                return

        # 私聊消息不处理（只处理群聊）
        if not event.is_group_message():
            return

        # 可选：忽略管理员消息
        if self.get_config().get("ignore_admin", True):
            if await self.is_admin(event):
                return

        # 获取会话的 LLM 模型 ID
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception as e:
            logger.error(f"获取 LLM Provider ID 失败: {e}")
            return

        # 构造审核提示词
        prompt_template = self.get_config().get("moderation_prompt", "")
        prompt = prompt_template.format(message=message_text)

        # 调用 LLM 模型进行审核
        try:
            llm_response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return

        response_text = llm_response.completion_text.strip()

        # 解析 LLM 返回结果（判断是否为 "1"）
        is_violation = self._parse_violation_response(response_text)

        if not is_violation:
            return

        # ---- 违规处理 ----
        logger.info(
            f"检测到违规消息 | 用户: {event.get_sender_name()} ({event.get_sender_id()}) | "
            f"群: {event.message_obj.group_id} | 内容: {message_text[:50]}..."
        )

        # 记录违规日志
        if self.get_config().get("log_violations", True):
            await self._log_violation(event, message_text, response_text)

        # 执行禁言
        mute_duration = self.get_config().get("mute_duration", 600)
        await self._mute_user(event, mute_duration)

        # 发送通知
        if self.get_config().get("notify_on_violation", True):
            yield event.plain_result(
                f"⚠️ 检测到违规内容，已对用户 {event.get_sender_name()} 执行禁言 {mute_duration} 秒。"
            )

    def _parse_violation_response(self, response_text: str) -> bool:
        """
        解析 LLM 返回的违规判断结果。
        若返回内容包含 "1"（非其他数字前缀匹配），视为违规，否则视为不违规。
        """
        # 提取响应中的数字部分
        match = re.search(r'\b(1|0)\b', response_text)
        if match and match.group(1) == "1":
            return True
        return False

    async def _mute_user(self, event: AstrMessageEvent, duration: int) -> bool:
        """
        执行禁言操作。
        通过平台的通用 API 调用群组禁言功能。
        """
        try:
            group_id = event.message_obj.group_id
            user_id = event.get_sender_id()

            if not group_id or not user_id:
                logger.warning("无法获取群组 ID 或用户 ID，跳过禁言")
                return False

            # 调用平台 API 执行禁言
            # set_group_ban 是 OneBot v11 标准 API，其他平台适配器可能需调整
            result = await self.context.platform_api.call_api(
                event.unified_msg_origin,
                "set_group_ban",
                {
                    "group_id": int(group_id),
                    "user_id": int(user_id),
                    "duration": duration,
                },
            )
            logger.info(
                f"已禁言用户 {user_id}，时长 {duration} 秒，"
                f"API 响应: {result}"
            )
            return True
        except Exception as e:
            logger.error(f"禁言用户失败: {e}")
            return False

    async def _log_violation(
        self, event: AstrMessageEvent, message_text: str, llm_response: str
    ) -> None:
        """记录违规日志到控制台（可扩展为写入文件或数据库）"""
        logger.info(
            f"[违规日志] 用户: {event.get_sender_id()} | 群: {event.message_obj.group_id} | "
            f"消息: {message_text[:100]} | LLM原始响应: {llm_response[:50]}"
        )
        # 如需持久化存储，可使用 self.context.db 写入数据库

    async def is_admin(self, event: AstrMessageEvent) -> bool:
        """
        检测用户是否为群管理员/群主。
        注意：此功能依赖平台适配器的实现，不同平台行为可能不同。
        """
        try:
            # 通过平台 API 获取用户角色
            group_id = event.message_obj.group_id
            user_id = event.get_sender_id()

            if not group_id or not user_id:
                return False

            result = await self.context.platform_api.call_api(
                event.unified_msg_origin,
                "get_group_member_info",
                {
                    "group_id": int(group_id),
                    "user_id": int(user_id),
                    "no_cache": False,
                },
            )
            role = result.get("role", "member")
            return role in ["owner", "admin"]
        except Exception as e:
            logger.error(f"获取用户角色失败: {e}")
            return False
