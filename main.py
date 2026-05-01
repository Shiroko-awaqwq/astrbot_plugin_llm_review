import re
from astrbot.api.event import filter, AstrMessageEvent, EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register("astrbot_plugin_moderation", "Your Name", "消息审核插件", "1.0.0")
class ModerationPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        # 获取插件配置（正确方式）
        enabled = self.plugin_conf.get("enabled", True)
        if not enabled:
            return

        message_text = event.message_str
        if not message_text or not message_text.strip():
            return

        # 忽略机器人自己发送的消息
        if self.plugin_conf.get("ignore_bot_self", True):
            if getattr(event.message_obj, "self_id", None) == event.get_sender_id():
                return

        # 只处理群聊消息
        if not event.is_group_message():
            return

        # 忽略管理员消息
        if self.plugin_conf.get("ignore_admin", True):
            if await self.is_admin(event):
                return

        # 获取当前会话的 LLM 提供者 ID
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception as e:
            logger.error(f"获取 LLM Provider ID 失败: {e}")
            return

        # 构造审核提示词
        prompt_template = self.plugin_conf.get(
            "moderation_prompt",
            "你是一个内容审核助手。请判断以下用户输入的消息是否包含违禁内容（如色情、暴力、政治敏感、辱骂、广告等）。"
            "如果消息违规，请只回复数字 '1'；如果消息不违规，请只回复数字 '0'。不要输出任何其他内容。\n\n用户消息：{message}"
        )
        prompt = prompt_template.format(message=message_text)

        # 调用 LLM 模型进行审核
        try:
            # 注意：根据 AstrBot 版本，如果 self.context.llm_generate 不存在，可改成 self.context.ai.llm_generate
            # 这里先使用标准写法，若报错请自行切换
            llm_response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except AttributeError:
            # 兼容新版本写法
            llm_response = await self.context.ai.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return

        # 获取响应文本（兼容不同返回结构）
        if hasattr(llm_response, "completion_text"):
            response_text = llm_response.completion_text.strip()
        elif hasattr(llm_response, "choices") and llm_response.choices:
            response_text = llm_response.choices[0].message.content.strip()
        else:
            response_text = str(llm_response).strip()

        # 解析 LLM 返回结果
        is_violation = self._parse_violation_response(response_text)

        if not is_violation:
            return

        # ---- 违规处理 ----
        logger.info(
            f"检测到违规消息 | 用户: {event.get_sender_name()} ({event.get_sender_id()}) | "
            f"群: {event.message_obj.group_id} | 内容: {message_text[:50]}..."
        )

        if self.plugin_conf.get("log_violations", True):
            await self._log_violation(event, message_text, response_text)

        mute_duration = self.plugin_conf.get("mute_duration", 600)
        await self._mute_user(event, mute_duration)

        if self.plugin_conf.get("notify_on_violation", True):
            yield event.plain_result(
                f"⚠️ 检测到违规内容，已对用户 {event.get_sender_name()} 执行禁言 {mute_duration} 秒。"
            )

    def _parse_violation_response(self, response_text: str) -> bool:
        """解析 LLM 响应，判断是否违规"""
        match = re.search(r'\b(1|0)\b', response_text)
        return match is not None and match.group(1) == "1"

    async def _mute_user(self, event: AstrMessageEvent, duration: int) -> bool:
        """执行禁言操作（OneBot 标准 API）"""
        try:
            group_id = event.message_obj.group_id
            user_id = event.get_sender_id()
            if not group_id or not user_id:
                logger.warning("无法获取群组 ID 或用户 ID，跳过禁言")
                return False
            result = await self.context.platform_api.call_api(
                event.unified_msg_origin,
                "set_group_ban",
                {
                    "group_id": int(group_id),
                    "user_id": int(user_id),
                    "duration": duration,
                },
            )
            logger.info(f"已禁言用户 {user_id}，时长 {duration} 秒，API 响应: {result}")
            return True
        except Exception as e:
            logger.error(f"禁言用户失败: {e}")
            return False

    async def _log_violation(
        self, event: AstrMessageEvent, message_text: str, llm_response: str
    ) -> None:
        logger.info(
            f"[违规日志] 用户: {event.get_sender_id()} | 群: {event.message_obj.group_id} | "
            f"消息: {message_text[:100]} | LLM原始响应: {llm_response[:50]}"
        )
        # 如需写入数据库，可使用 self.context.db

    async def is_admin(self, event: AstrMessageEvent) -> bool:
        """检测用户是否为群管理/群主"""
        try:
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
