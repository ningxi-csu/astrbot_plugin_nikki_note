# -*- coding: utf-8 -*-
"""
AstrBot 插件：无限暖暖 · 奇想手账日程便利贴

功能：
  - 每位用户私聊机器人，用自己的手机号 + 短信验证码绑定自己的账号
  - 绑定后在群聊或私聊发送 /暖暖日程 查看自己的日程
  - 登录态过期时自动用 refresh_token 续期；续期失败才要求重新绑定

数据保存在插件目录的 data/users.json，按 QQ 号隔离；
data/device.json 是通行证设备标识，必须稳定，不要手动删除。
"""
import asyncio
import json
import os
import re
import sys
import threading
import time

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# AstrBot 可能以包或单文件方式加载插件，两种方式都保证能导入同目录的 nikki_api
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
try:
    from . import nikki_api as nikki
except ImportError:  # pragma: no cover
    import nikki_api as nikki

DATA_DIR = os.path.join(BASE_DIR, 'data')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')

# ---- 可调参数 ----
CACHE_TTL = 60           # 同一账号的数据缓存秒数，避免群友频繁查询放大请求
OVERVIEW_TTL = 300       # 账号总览缓存秒数；探索度数据变化慢，减少地图接口请求
GACHA_TTL = 60           # 卡池查询缓存秒数
QUERY_COOLDOWN = 10      # 每个 QQ 的查询冷却秒数
SMS_COOLDOWN = 60        # 每个 QQ 发送验证码的冷却，防止短信轰炸
PENDING_TTL = 300        # 绑定流程保留时间；短信验证码实际 3 分钟有效
TIME_SYNC_TTL = 300      # 服务器时间偏移缓存秒数

HELP_TEXT = """无限暖暖 · 奇想手账

/暖暖日程        查看自己的日程（群聊/私聊）
/暖暖总览        查看账号数据、探索总览、奇想札记与奇迹之冠
/查询卡池 蝴蝶套  查询指定限定五星套装
/查询卡池 1.0    查询版本限定五星及陪跑四星
/查询卡池 1.0双四  查询版本双四星套装
/查询卡池 常驻    查询常驻池四套五星与四套四星
/暖暖绑定 手机号  私聊绑定自己的账号（短信验证码）
/暖暖验证码 验证码  私聊完成绑定
/暖暖密码登录 账号 密码  私聊用账号密码直接绑定
/暖暖解绑        私聊解除绑定

绑定需要接收短信验证码，请在与机器人的私聊中操作。"""


@register(
    "astrbot_plugin_nikki_note",
    "自定义",
    "无限暖暖奇想手账查询：每人绑定自己的账号，查看日程、账号总览及卡池共鸣统计",
    "1.0.0",
    "",
)
class NikkiNotePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._lock = threading.Lock()
        self._users = self._load_users()   # {qq: {nid, token, refresh_token, account}}
        self._pending = {}                 # {qq: (phone, 发送时间)} 绑定中的手机号
        self._last_sms = {}                # {qq: 上次发送验证码时间}
        self._last_query = {}              # {qq: 上次查询时间}
        self._last_overview = {}           # {qq: 上次总览时间}
        self._last_gacha = {}              # {qq: 上次卡池查询时间}
        self._cache = {}                   # {nid: (缓存时间, 卡片数据)}
        self._overview_cache = {}          # {nid: (缓存时间, 总览文本)}
        self._gacha_cache = {}             # {(nid, 查询参数): (缓存时间, 卡池文本)}
        self._time_offset = 0.0            # 服务器时间 - 本机时间
        self._time_synced_at = 0.0

    # ---------------- 存储 ----------------
    def _load_users(self) -> dict:
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_users(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        with self._lock:
            with open(USERS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._users, f, ensure_ascii=False, indent=2)

    # ---------------- 工具 ----------------
    @staticmethod
    def _phone_arg(event: AstrMessageEvent) -> str:
        """取指令后的第一个参数；用 message_str 手动解析，避免缺参时报错"""
        parts = event.message_str.strip().split()
        return parts[1].strip() if len(parts) > 1 else ''

    def _server_now(self) -> float:
        """带偏移缓存的服务器时间，减少对时间接口的请求"""
        now = time.time()
        if now - self._time_synced_at < TIME_SYNC_TTL:
            return now + self._time_offset
        server = nikki.fetch_server_time()
        self._time_offset = server - now
        self._time_synced_at = now
        return server

    def _fetch_card(self, qq: str, user: dict):
        """同步网络操作，在 asyncio.to_thread 中执行，避免阻塞事件循环"""
        nid = str(user['nid'])
        cached = self._cache.get(nid)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1], user

        gm, session = nikki.fetch_with_refresh(user)
        # 续期成功后同步写回该 QQ 的登录态
        if session.get('token') != user.get('token'):
            with self._lock:
                self._users[qq] = session
            self._save_users()

        card = nikki.compute_card(gm, self._server_now())
        self._cache[nid] = (time.time(), card)
        return card, session

    def _fetch_overview(self, qq: str, user: dict):
        """账号总览；数据量较大，单独缓存，减少地图分类接口请求"""
        nid = str(user['nid'])
        cached = self._overview_cache.get(nid)
        if cached and time.time() - cached[0] < OVERVIEW_TTL:
            return cached[1], user

        text, session = nikki.fetch_overview_with_refresh(user)
        if session.get('token') != user.get('token'):
            with self._lock:
                self._users[qq] = session
            self._save_users()

        self._overview_cache[nid] = (time.time(), text)
        return text, session

    def _fetch_gacha(self, qq: str, user: dict, query: str):
        """卡池查询按账号和查询参数分别缓存"""
        nid = str(user['nid'])
        cache_key = (nid, query)
        cached = self._gacha_cache.get(cache_key)
        if cached and time.time() - cached[0] < GACHA_TTL:
            return cached[1], user

        text, session = nikki.fetch_gacha_with_refresh(user, query)
        if session.get('token') != user.get('token'):
            with self._lock:
                self._users[qq] = session
            self._save_users()

        self._gacha_cache[cache_key] = (time.time(), text)
        return text, session

    @staticmethod
    def _markdown_result(event: AstrMessageEvent, text: str):
        """优先使用平台支持的 markdown 消息，否则退回普通文本"""
        markdown = getattr(event, 'markdown_result', None)
        if callable(markdown):
            return markdown(text)
        return event.plain_result(text)

    # ---------------- 指令 ----------------
    @filter.command("暖暖帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        """查看插件用法"""
        yield event.plain_result(HELP_TEXT)

    @filter.command("暖暖绑定")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def bind(self, event: AstrMessageEvent):
        """私聊绑定：发送手机号，机器人发送短信验证码"""
        qq = str(event.get_sender_id())
        phone = self._phone_arg(event)
        if not re.fullmatch(r'1\d{10}', phone):
            yield event.plain_result('用法：/暖暖绑定 手机号\n例如：/暖暖绑定 13800138000')
            return

        now = time.time()
        if now - self._last_sms.get(qq, 0) < SMS_COOLDOWN:
            wait = int(SMS_COOLDOWN - (now - self._last_sms.get(qq, 0))) + 1
            yield event.plain_result(f'发送太频繁，请 {wait} 秒后再试。')
            return

        try:
            await asyncio.to_thread(nikki.send_login_code, phone)
        except nikki.NikkiError as exc:
            yield event.plain_result(f'验证码发送失败：{exc}')
            return

        self._last_sms[qq] = now
        self._pending[qq] = (phone, now)
        yield event.plain_result(
            f'验证码已发送至 {phone}，请回复：\n'
            '/暖暖验证码 收到的验证码\n'
            '短信验证码 3 分钟内有效。'
        )

    @filter.command("暖暖验证码")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def verify(self, event: AstrMessageEvent):
        """私聊提交短信验证码，完成绑定"""
        qq = str(event.get_sender_id())
        code = self._phone_arg(event)
        pending = self._pending.get(qq)
        if not pending:
            yield event.plain_result('请先发送：/暖暖绑定 手机号')
            return
        if time.time() - pending[1] > PENDING_TTL:
            self._pending.pop(qq, None)
            yield event.plain_result('绑定流程已超时，请重新发送：/暖暖绑定 手机号')
            return
        if not re.fullmatch(r'\d{4,8}', code):
            yield event.plain_result('验证码格式不正确，请直接发送数字验证码。')
            return

        phone = pending[0]
        try:
            session = await asyncio.to_thread(nikki.login_with_code, phone, code)
        except nikki.NikkiError as exc:
            yield event.plain_result(f'绑定失败：{exc}\n可重新发送 /暖暖绑定 手机号 获取新验证码。')
            return

        with self._lock:
            self._users[qq] = session
        self._save_users()
        self._pending.pop(qq, None)
        yield event.plain_result(
            f'绑定成功。\n搭配师编号：{session["nid"]}\n'
            '现在可以在群里或私聊发送 /暖暖日程 查看日程。'
        )

    @filter.command("暖暖密码登录")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def password_login(self, event: AstrMessageEvent):
        """私聊用账号密码直接绑定，无需短信验证码"""
        qq = str(event.get_sender_id())
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result(
                '用法：/暖暖密码登录 账号 密码\n'
                '例如：/暖暖密码登录 13800138000 mypassword\n'
                '账号支持手机号或邮箱。'
            )
            return
        account, password = parts[1].strip(), parts[2].strip()

        try:
            session = await asyncio.to_thread(nikki.login_with_password, account, password)
        except nikki.NikkiError as exc:
            yield event.plain_result(f'登录失败：{exc}')
            return

        with self._lock:
            self._users[qq] = session
        self._save_users()
        self._pending.pop(qq, None)
        yield event.plain_result(
            f'绑定成功。\n搭配师编号：{session["nid"]}\n'
            '现在可以在群里或私聊发送 /暖暖日程 查看日程。'
        )

    @filter.command("暖暖日程")
    async def schedule(self, event: AstrMessageEvent):
        """查看当前绑定的账号日程；群聊、私聊均可"""
        qq = str(event.get_sender_id())
        user = self._users.get(qq)
        if not user:
            yield event.plain_result('你还没有绑定账号。\n请私聊机器人发送：/暖暖绑定 手机号')
            return

        now = time.time()
        last = self._last_query.get(qq, 0)
        if now - last < QUERY_COOLDOWN:
            wait = int(QUERY_COOLDOWN - (now - last)) + 1
            yield event.plain_result(f'查询太频繁，请 {wait} 秒后再试。')
            return
        self._last_query[qq] = now

        try:
            card, session = await asyncio.to_thread(self._fetch_card, qq, user)
        except nikki.SessionExpired:
            yield event.plain_result(
                '登录已失效且自动续期失败，请私聊重新绑定：\n/暖暖绑定 手机号'
            )
            return
        except Exception as exc:  # 网络、接口异常等，仅向用户返回简短原因
            logger.warning(f'暖暖日程查询失败 qq={qq}: {exc}')
            yield event.plain_result(f'查询失败：{exc}')
            return

        yield event.plain_result(nikki.format_card(card, session))

    @filter.command("暖暖总览")
    async def overview(self, event: AstrMessageEvent):
        """查看账号数据、探索总览、奇想札记与奇迹之冠；群聊、私聊均可"""
        qq = str(event.get_sender_id())
        user = self._users.get(qq)
        if not user:
            yield event.plain_result('你还没有绑定账号。\n请私聊机器人发送：/暖暖绑定 手机号')
            return

        now = time.time()
        last = self._last_overview.get(qq, 0)
        if now - last < QUERY_COOLDOWN:
            wait = int(QUERY_COOLDOWN - (now - last)) + 1
            yield event.plain_result(f'查询太频繁，请 {wait} 秒后再试。')
            return
        self._last_overview[qq] = now

        try:
            text, _session = await asyncio.to_thread(self._fetch_overview, qq, user)
        except nikki.SessionExpired:
            yield event.plain_result(
                '登录已失效且自动续期失败，请私聊重新绑定：\n/暖暖绑定 手机号'
            )
            return
        except Exception as exc:
            logger.warning(f'暖暖总览查询失败 qq={qq}: {exc}')
            yield event.plain_result(f'查询失败：{exc}')
            return

        yield self._markdown_result(event, text)

    @filter.regex(r'^/?查询卡池')
    async def gacha(self, event: AstrMessageEvent):
        """查询卡池共鸣统计；兼容“/查询卡池蝴蝶套”和“/查询卡池 蝴蝶套”"""
        query = re.sub(r'^/?查询卡池\s*', '', event.message_str.strip(), count=1).strip()
        if not query:
            yield event.plain_result(
                '用法：/查询卡池 蝴蝶套\n'
                '也支持：/查询卡池 1.0、/查询卡池 1.0双四、/查询卡池 常驻'
            )
            return

        qq = str(event.get_sender_id())
        user = self._users.get(qq)
        if not user:
            yield event.plain_result('你还没有绑定账号。\n请私聊机器人发送：/暖暖绑定 手机号')
            return

        now = time.time()
        last = self._last_gacha.get(qq, 0)
        if now - last < QUERY_COOLDOWN:
            wait = int(QUERY_COOLDOWN - (now - last)) + 1
            yield event.plain_result(f'查询太频繁，请 {wait} 秒后再试。')
            return
        self._last_gacha[qq] = now

        try:
            text, _session = await asyncio.to_thread(self._fetch_gacha, qq, user, query)
        except nikki.SessionExpired:
            yield event.plain_result(
                '登录已失效且自动续期失败，请私聊重新绑定：\n/暖暖绑定 手机号'
            )
            return
        except nikki.NikkiError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:
            logger.warning(f'卡池查询失败 qq={qq}, query={query}: {exc}')
            yield event.plain_result(f'查询失败：{exc}')
            return

        yield self._markdown_result(event, text)

    @filter.command("暖暖解绑")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def unbind(self, event: AstrMessageEvent):
        """私聊解除绑定，删除本地保存的登录态"""
        qq = str(event.get_sender_id())
        if qq not in self._users:
            yield event.plain_result('你还没有绑定账号。')
            return
        with self._lock:
            self._users.pop(qq, None)
        self._save_users()
        yield event.plain_result('已解除绑定，本地登录态已删除。')
