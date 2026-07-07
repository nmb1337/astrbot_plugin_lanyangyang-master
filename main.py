import json
import random
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except Exception:  # pragma: no cover - handled at runtime inside _render_card
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None


PLUGIN_NAME = "astrbot_plugin_lanyangyang"


@register(
    "astrbot_plugin_lanyangyang",
    "Codex",
    "懒羊羊主题基础群管：发言统计、邀请排行、禁言、撤回、批量撤回、踢出、踢黑、图片回复与偶发语音",
    "1.0.0",
)
class LanYangYangGroupManager(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.base_dir = Path(__file__).parent
        self.data_dir = self.base_dir / "data"
        self.cache_dir = self.base_dir / "cache"
        self.voice_dir = self.base_dir / "voices"
        self.data_file = self.data_dir / "lanyangyang_stats.json"
        self.data_dir.mkdir(exist_ok=True)
        self.cache_dir.mkdir(exist_ok=True)
        self.voice_dir.mkdir(exist_ok=True)
        self.stats = self._load_stats()

    async def initialize(self):
        logger.info("懒羊羊群管插件已加载。发送“菜单”可免唤醒查看命令。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_every_message(self, event: AstrMessageEvent):
        await self._record_message(event)
        await self._record_invite_from_raw(event)

        text = (event.message_str or "").strip()
        if text in self._list_config(
            "direct_menu_keywords", ["菜单", "帮助", "懒羊羊菜单"]
        ):
            yield await self._image_result(event, "懒羊羊菜单", self._menu_lines())
            event.stop_event()
            return

        if self._should_lazy_voice(event, text):
            result = await self._lazy_voice_or_card(event)
            yield result
            event.stop_event()

    @filter.command("菜单", alias={"帮助", "懒羊羊菜单"})
    async def menu(self, event: AstrMessageEvent):
        """显示懒羊羊群管菜单。"""
        yield await self._image_result(event, "懒羊羊菜单", self._menu_lines())
        event.stop_event()

    @filter.command("发言统计", alias={"统计", "水群排行"})
    async def speech_rank(self, event: AstrMessageEvent):
        """查看本群发言排行。"""
        group_id = self._group_id(event)
        if not group_id:
            yield await self._image_result(event, "发言统计", ["这个功能要在群聊里用。"])
            return

        members = self.stats["groups"].get(group_id, {}).get("members", {})
        ranking = sorted(
            members.items(), key=lambda item: item[1].get("count", 0), reverse=True
        )[:10]
        if not ranking:
            lines = ["还没有统计到发言。"]
        else:
            lines = [
                f"{idx}. {info.get('name') or uid}: {info.get('count', 0)} 条 / {info.get('chars', 0)} 字"
                for idx, (uid, info) in enumerate(ranking, 1)
            ]
        yield await self._image_result(event, "本群发言排行", lines)

    @filter.command("我的统计", alias={"我水了多少"})
    async def my_speech(self, event: AstrMessageEvent):
        """查看自己的发言统计。"""
        group_id = self._group_id(event)
        user_id = str(event.get_sender_id())
        info = self.stats["groups"].get(group_id, {}).get("members", {}).get(user_id)
        if not info:
            lines = ["还没有统计到你的发言。"]
        else:
            last = self._format_time(info.get("last_active"))
            lines = [
                f"昵称：{info.get('name') or event.get_sender_name()}",
                f"发言：{info.get('count', 0)} 条",
                f"字数：{info.get('chars', 0)} 字",
                f"最近：{last}",
            ]
        yield await self._image_result(event, "我的发言统计", lines)

    @filter.command("邀请排行", alias={"邀请榜"})
    async def invite_rank(self, event: AstrMessageEvent):
        """查看本群邀请排行。"""
        group_id = self._group_id(event)
        rows = self.stats["invites"].get(group_id, {})
        ranking = sorted(rows.items(), key=lambda item: item[1].get("count", 0), reverse=True)[:10]
        if not ranking:
            lines = [
                "暂时没有邀请记录。",
                "如果协议端没有上报入群邀请人，可以用：记邀请 @邀请人",
            ]
        else:
            lines = [
                f"{idx}. {info.get('name') or uid}: 邀请 {info.get('count', 0)} 人"
                for idx, (uid, info) in enumerate(ranking, 1)
            ]
        yield await self._image_result(event, "邀请排行", lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("记邀请", alias={"登记邀请"})
    async def record_invite(self, event: AstrMessageEvent):
        """手动登记一次邀请。"""
        group_id = self._group_id(event)
        inviter = self._extract_target_user(event)
        if not group_id or not inviter:
            yield await self._image_result(event, "登记邀请", ["用法：记邀请 @邀请人"])
            return
        uid, name = inviter
        self._add_invite(group_id, uid, name)
        self._save_stats()
        yield await self._image_result(event, "登记邀请", [f"已给 {name or uid} 记 1 次邀请。"])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁言", alias={"闭嘴"})
    async def mute(self, event: AstrMessageEvent):
        """禁言群成员。"""
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        duration = self._extract_duration(event, default_seconds=600)
        if not group_id or not target:
            yield await self._image_result(event, "禁言", ["用法：禁言 @成员 10m"])
            return
        ok, msg = await self._onebot_call(
            event,
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(target[0]),
            duration=duration,
        )
        lines = [f"{target[1] or target[0]} 禁言 {self._human_duration(duration)}", msg]
        yield await self._image_result(event, "禁言结果", lines if ok else [msg])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("解禁", alias={"解除禁言"})
    async def unmute(self, event: AstrMessageEvent):
        """解除群成员禁言。"""
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        if not group_id or not target:
            yield await self._image_result(event, "解禁", ["用法：解禁 @成员"])
            return
        ok, msg = await self._onebot_call(
            event,
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(target[0]),
            duration=0,
        )
        yield await self._image_result(
            event, "解禁结果", [f"{target[1] or target[0]} 已解禁。", msg] if ok else [msg]
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤回", alias={"删"})
    async def recall(self, event: AstrMessageEvent):
        """撤回一条消息，支持回复消息后发送“撤回”。"""
        message_id = self._extract_reply_message_id(event) or self._extract_first_number(event)
        if not message_id:
            yield await self._image_result(event, "撤回", ["请回复要撤回的消息，或发送：撤回 消息ID"])
            return
        ok, msg = await self._onebot_call(event, "delete_msg", message_id=int(message_id))
        yield await self._image_result(event, "撤回结果", [msg if ok else f"撤回失败：{msg}"])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("批量撤回", alias={"批撤"})
    async def batch_recall(self, event: AstrMessageEvent):
        """批量撤回最近消息。"""
        group_id = self._group_id(event)
        if not group_id:
            yield await self._image_result(event, "批量撤回", ["这个功能要在群聊里用。"])
            return
        count = max(1, min(self._extract_count(event, default=5), 50))
        target = self._extract_target_user(event)
        ids = self._recent_message_ids(group_id, count, target[0] if target else None, event)
        success = 0
        errors = []
        for msg_id in ids:
            ok, msg = await self._onebot_call(event, "delete_msg", message_id=int(msg_id))
            success += 1 if ok else 0
            if not ok:
                errors.append(msg)
        lines = [f"目标：最近 {count} 条", f"成功撤回：{success} 条"]
        if errors:
            lines.append(f"失败：{errors[0]}")
        yield await self._image_result(event, "批量撤回结果", lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("踢出群", alias={"踢出", "踢"})
    async def kick(self, event: AstrMessageEvent):
        """踢出群成员，不拉黑。"""
        yield await self._kick_impl(event, reject=False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("踢黑", alias={"拉黑踢出"})
    async def kick_black(self, event: AstrMessageEvent):
        """踢出并拒绝再次加群。"""
        yield await self._kick_impl(event, reject=True)

    @filter.on_decorating_result()
    async def decorate_text_reply(self, event: AstrMessageEvent):
        if not self._bool_config("convert_all_text_reply", True):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None)
        if not chain or not self._is_text_only_chain(chain):
            return
        text = "\n".join(self._component_text(item) for item in chain).strip()
        if not text:
            return
        path = await self._render_card(event, "懒羊羊回复", text)
        result.chain = [Comp.Image.fromFileSystem(str(path))]

    async def _kick_impl(self, event: AstrMessageEvent, reject: bool):
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        if not group_id or not target:
            return await self._image_result(event, "踢出群", ["用法：踢出群 @成员 或 踢黑 @成员"])
        ok, msg = await self._onebot_call(
            event,
            "set_group_kick",
            group_id=int(group_id),
            user_id=int(target[0]),
            reject_add_request=reject,
        )
        action = "踢黑" if reject else "踢出"
        lines = [f"{action}：{target[1] or target[0]}", msg]
        return await self._image_result(event, f"{action}结果", lines if ok else [msg])

    async def _image_result(self, event: AstrMessageEvent, title: str, lines: list[str] | str):
        try:
            path = await self._render_card(event, title, lines)
            return event.chain_result([Comp.Image.fromFileSystem(str(path))])
        except Exception as exc:
            logger.exception("生成懒羊羊图片失败")
            text = "\n".join(lines) if isinstance(lines, list) else str(lines)
            return event.plain_result(f"{title}\n{text}\n\n图片生成失败：{exc}")

    async def _render_card(self, event: AstrMessageEvent, title: str, lines: list[str] | str) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，请安装 requirements.txt 里的 Pillow。")

        if isinstance(lines, str):
            raw_lines = lines.splitlines() or [lines]
        else:
            raw_lines = [str(line) for line in lines]

        width = 980
        line_height = 42
        body_lines = []
        for line in raw_lines:
            body_lines.extend(self._wrap_text(line, max_chars=28) or [""])
        height = max(620, 390 + len(body_lines) * line_height)

        img = Image.new("RGB", (width, height), "#fff7cf")
        draw = ImageDraw.Draw(img)
        font_title = self._font(48, bold=True)
        font_name = self._font(28, bold=True)
        font_body = self._font(31)
        font_small = self._font(24)

        draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=36, fill="#fffbe8")
        draw.rounded_rectangle((44, 44, width - 44, 190), radius=30, fill="#d9f2c7")
        draw.ellipse((760, -70, 1070, 240), fill="#ffe68a")
        draw.ellipse((-80, 350, 220, 650), fill="#e6f6d6")

        self._draw_sheep_badge(draw, 800, 82)
        avatar = self._load_avatar(str(event.get_sender_id()))
        if avatar:
            img.paste(avatar, (72, 68), avatar)
        else:
            draw.ellipse((72, 68, 152, 148), fill="#ffffff", outline="#8ac17b", width=4)

        sender_name = event.get_sender_name() or str(event.get_sender_id())
        draw.text((174, 66), title, font=font_title, fill="#4f7b42")
        draw.text((176, 128), f"呼叫人：{sender_name}", font=font_name, fill="#6f865d")

        body_top = 220
        draw.rounded_rectangle(
            (64, body_top, width - 64, height - 120),
            radius=26,
            fill="#ffffff",
            outline="#eadf9a",
            width=3,
        )
        y = body_top + 34
        for line in body_lines:
            draw.text((96, y), line, font=font_body, fill="#41503c")
            y += line_height

        footer = "懒羊羊主题卡片回复 | 发送“菜单”无需唤醒词"
        draw.text((70, height - 74), footer, font=font_small, fill="#8a936f")

        path = self.cache_dir / f"reply_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        img.save(path, "PNG", optimize=True)
        return path

    def _draw_sheep_badge(self, draw: Any, x: int, y: int):
        wool = "#ffffff"
        outline = "#7a9d68"
        for dx, dy, r in [(-38, -22, 30), (0, -36, 34), (38, -22, 30), (-24, 8, 34), (24, 8, 34)]:
            draw.ellipse((x + dx - r, y + dy - r, x + dx + r, y + dy + r), fill=wool, outline=outline, width=3)
        draw.rounded_rectangle((x - 44, y - 8, x + 44, y + 58), radius=28, fill="#ffe8ad", outline=outline, width=3)
        draw.arc((x - 70, y - 8, x - 26, y + 48), 90, 280, fill="#c7a15a", width=5)
        draw.arc((x + 26, y - 8, x + 70, y + 48), -100, 90, fill="#c7a15a", width=5)
        draw.ellipse((x - 20, y + 16, x - 12, y + 24), fill="#4f4a37")
        draw.ellipse((x + 12, y + 16, x + 20, y + 24), fill="#4f4a37")
        draw.arc((x - 14, y + 26, x + 14, y + 44), 15, 165, fill="#8a6b39", width=3)

    def _font(self, size: int, bold: bool = False):
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for item in candidates:
            if item and Path(item).exists():
                return ImageFont.truetype(item, size=size)
        return ImageFont.load_default()

    def _wrap_text(self, text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        lines = []
        current = ""
        for char in text:
            current += char
            if len(current) >= max_chars:
                lines.append(current)
                current = ""
        if current:
            lines.append(current)
        return lines

    def _load_avatar(self, user_id: str):
        if not user_id:
            return None
        cache = self.cache_dir / f"avatar_{user_id}.png"
        try:
            if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
                raw = cache.read_bytes()
            else:
                url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
                with urlopen(url, timeout=4) as resp:
                    raw = resp.read()
                cache.write_bytes(raw)
            avatar = Image.open(BytesIO(raw)).convert("RGBA").resize((80, 80))
            mask = Image.new("L", (80, 80), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, 80, 80), fill=255)
            avatar.putalpha(mask)
            return ImageOps.expand(avatar, border=4, fill="#ffffff")
        except Exception:
            return None

    async def _record_message(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        user_id = str(event.get_sender_id() or "")
        if not group_id or not user_id:
            return
        group = self.stats["groups"].setdefault(group_id, {"members": {}, "history": []})
        member = group["members"].setdefault(user_id, {"name": "", "count": 0, "chars": 0, "last_active": 0})
        member["name"] = event.get_sender_name() or member.get("name") or user_id
        member["count"] = int(member.get("count", 0)) + 1
        member["chars"] = int(member.get("chars", 0)) + len(event.message_str or "")
        member["last_active"] = int(time.time())
        message_id = getattr(event.message_obj, "message_id", None)
        if message_id:
            group["history"].append(
                {
                    "message_id": str(message_id),
                    "user_id": user_id,
                    "name": member["name"],
                    "time": int(time.time()),
                }
            )
            group["history"] = group["history"][-300:]
        self._save_stats()

    async def _record_invite_from_raw(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_increase":
            return
        group_id = str(raw.get("group_id") or self._group_id(event))
        operator_id = str(raw.get("operator_id") or "")
        if not group_id or not operator_id:
            return
        self._add_invite(group_id, operator_id, raw.get("operator_id"))
        self._save_stats()

    def _add_invite(self, group_id: str, user_id: str, name: Any = None):
        rows = self.stats["invites"].setdefault(group_id, {})
        info = rows.setdefault(str(user_id), {"name": "", "count": 0, "last_time": 0})
        info["name"] = str(name or info.get("name") or user_id)
        info["count"] = int(info.get("count", 0)) + 1
        info["last_time"] = int(time.time())

    def _load_stats(self) -> dict:
        if not self.data_file.exists():
            return {"groups": {}, "invites": {}}
        try:
            data = json.loads(self.data_file.read_text(encoding="utf-8"))
            data.setdefault("groups", {})
            data.setdefault("invites", {})
            return data
        except Exception:
            logger.exception("读取懒羊羊统计数据失败，已重新初始化。")
            return {"groups": {}, "invites": {}}

    def _save_stats(self):
        self.data_file.write_text(
            json.dumps(self.stats, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _onebot_call(self, event: AstrMessageEvent, action: str, **payload):
        if event.get_platform_name() != "aiocqhttp" or not hasattr(event, "bot"):
            return False, "当前平台不是 aiocqhttp/OneBot，不能执行这个群管动作。"
        try:
            await event.bot.api.call_action(action, **payload)
            return True, "操作完成。"
        except Exception as exc:
            logger.exception("OneBot API 调用失败：%s", action)
            return False, str(exc)

    def _extract_target_user(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        bot_id = str(getattr(event.message_obj, "self_id", "") or "")
        for item in event.get_messages():
            qq = getattr(item, "qq", None)
            if qq and str(qq) not in {bot_id, "all"}:
                return str(qq), self._member_name_from_stats(event, str(qq))
        text = self._command_args(event)
        numbers = re.findall(r"\b\d{5,12}\b", text)
        if numbers:
            return numbers[0], self._member_name_from_stats(event, numbers[0])
        return None

    def _extract_reply_message_id(self, event: AstrMessageEvent) -> str | None:
        for item in event.get_messages():
            name = item.__class__.__name__.lower()
            if name == "reply":
                for attr in ("id", "message_id", "seq"):
                    value = getattr(item, attr, None)
                    if value:
                        return str(value)
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            reply = raw.get("reply") or raw.get("source")
            if isinstance(reply, dict):
                return str(reply.get("message_id") or reply.get("id") or "") or None
        return None

    def _extract_duration(self, event: AstrMessageEvent, default_seconds: int) -> int:
        text = self._command_args(event)
        target = self._extract_target_user(event)
        matches = list(re.finditer(r"(\d+)\s*(秒|s|分钟|分|m|小时|时|h|天|d)?", text, re.I))
        if not matches:
            return default_seconds
        match = None
        for item in reversed(matches):
            number = item.group(1)
            unit_text = item.group(2)
            if target and number == str(target[0]):
                continue
            if unit_text or int(number) < 10000:
                match = item
                break
        if not match:
            return default_seconds
        value = int(match.group(1))
        unit = (match.group(2) or "m").lower()
        if unit in {"秒", "s"}:
            return value
        if unit in {"小时", "时", "h"}:
            return value * 3600
        if unit in {"天", "d"}:
            return value * 86400
        return value * 60

    def _extract_count(self, event: AstrMessageEvent, default: int) -> int:
        text = self._command_args(event)
        nums = re.findall(r"\b\d{1,3}\b", text)
        return int(nums[-1]) if nums else default

    def _extract_first_number(self, event: AstrMessageEvent) -> str | None:
        nums = re.findall(r"\b\d+\b", self._command_args(event))
        return nums[0] if nums else None

    def _recent_message_ids(
        self,
        group_id: str,
        count: int,
        user_id: str | None,
        event: AstrMessageEvent,
    ) -> list[str]:
        current = str(getattr(event.message_obj, "message_id", "") or "")
        history = self.stats["groups"].get(group_id, {}).get("history", [])
        ids = []
        for row in reversed(history):
            if str(row.get("message_id")) == current:
                continue
            if user_id and str(row.get("user_id")) != str(user_id):
                continue
            ids.append(str(row.get("message_id")))
            if len(ids) >= count:
                break
        return ids

    def _command_args(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        text = text.lstrip("/")
        names = [
            "菜单",
            "帮助",
            "懒羊羊菜单",
            "发言统计",
            "统计",
            "水群排行",
            "我的统计",
            "我水了多少",
            "邀请排行",
            "邀请榜",
            "记邀请",
            "登记邀请",
            "禁言",
            "闭嘴",
            "解禁",
            "解除禁言",
            "撤回",
            "删",
            "批量撤回",
            "批撤",
            "踢出群",
            "踢出",
            "踢",
            "踢黑",
            "拉黑踢出",
        ]
        for name in sorted(names, key=len, reverse=True):
            if text.startswith(name):
                return text[len(name) :].strip()
        return text

    def _member_name_from_stats(self, event: AstrMessageEvent, user_id: str) -> str:
        group_id = self._group_id(event)
        return (
            self.stats["groups"]
            .get(group_id, {})
            .get("members", {})
            .get(str(user_id), {})
            .get("name", str(user_id))
        )

    def _group_id(self, event: AstrMessageEvent) -> str:
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = getattr(event.message_obj, "group_id", "")
        return str(group_id or "")

    def _should_lazy_voice(self, event: AstrMessageEvent, text: str) -> bool:
        if not text:
            return False
        direct = any(key in text for key in self._list_config("voice_trigger_keywords", ["懒羊羊回家"]))
        mentioned = self._is_bot_mentioned(event)
        if not direct and not mentioned:
            return False
        chance = float(self.config.get("voice_reply_chance", 0.35 if direct else 0.12))
        return random.random() <= max(0.0, min(chance, 1.0))

    async def _lazy_voice_or_card(self, event: AstrMessageEvent):
        voices = sorted(self.voice_dir.glob("*.wav"))
        if voices:
            path = str(random.choice(voices))
            if hasattr(Comp.Record, "fromFileSystem"):
                record = Comp.Record.fromFileSystem(path)
            else:
                record = Comp.Record(file=path, url=path)
            return event.chain_result([record])
        roasts = self._list_config(
            "voice_fallback_roasts",
            [
                "叫我回家？我才刚躺下。",
                "你先回，我再睡五分钟。",
                "别催，懒羊羊正在缓慢加载。",
            ],
        )
        return await self._image_result(event, "懒羊羊语音", [random.choice(roasts), "把 wav 语音放进 voices 目录后，我就能发语音了。"])

    def _is_bot_mentioned(self, event: AstrMessageEvent) -> bool:
        bot_id = str(getattr(event.message_obj, "self_id", "") or "")
        if not bot_id:
            return False
        for item in event.get_messages():
            if str(getattr(item, "qq", "")) == bot_id:
                return True
        return False

    def _is_text_only_chain(self, chain: list[Any]) -> bool:
        return all(item.__class__.__name__ == "Plain" for item in chain)

    def _component_text(self, item: Any) -> str:
        return str(getattr(item, "text", getattr(item, "message", "")) or "")

    def _menu_lines(self) -> list[str]:
        return [
            "菜单：无需 /，直接发送“菜单”",
            "发言统计 / 我的统计",
            "邀请排行 / 记邀请 @邀请人",
            "禁言 @成员 10m / 解禁 @成员",
            "回复消息后：撤回",
            "批量撤回 5 / 批量撤回 @成员 10",
            "踢出群 @成员 / 踢黑 @成员",
            "喊“懒羊羊回家”或艾特机器人，会偶尔语音/图片回怼",
        ]

    def _list_config(self, key: str, default: list[str]) -> list[str]:
        value = self.config.get(key, default)
        return value if isinstance(value, list) and value else default

    def _bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return bool(value)

    def _format_time(self, timestamp: Any) -> str:
        if not timestamp:
            return "未知"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(timestamp)))

    def _human_duration(self, seconds: int) -> str:
        if seconds % 86400 == 0 and seconds >= 86400:
            return f"{seconds // 86400}天"
        if seconds % 3600 == 0 and seconds >= 3600:
            return f"{seconds // 3600}小时"
        if seconds % 60 == 0 and seconds >= 60:
            return f"{seconds // 60}分钟"
        return f"{seconds}秒"

    async def terminate(self):
        self._save_stats()
