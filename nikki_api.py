# -*- coding: utf-8 -*-
"""
无限暖暖 · 奇想手账数据接口（AstrBot 插件版）

和 daily_note.py 的区别：
  - 不读写固定的 session.json，登录态由 main.py 按 QQ 号分别管理
  - 不包含终端交互和 ANSI 渲染，只提供纯文本格式化
  - 设备标识保存在插件目录 data/device.json，整个插件共用一个

重要：通行证接口要求发送验证码和登录使用同一个 DOID/web_deviceid，
否则验证码会被判定为无效（code=1005），所以设备标识必须持久化。
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import random
import re
import string
import threading
import time
import urllib.parse
import uuid
from decimal import Decimal, ROUND_HALF_UP

import cramjam
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ---------------- 配置 ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DEVICE_FILE = os.path.join(DATA_DIR, 'device.json')
_device_lock = threading.Lock()

PASSPORT = 'https://passport.papegames.com'
API = 'https://myl-api.nuanpaper.com'
TIME_API = 'https://chaos.papegames.com/system/time'

CLIENT_ID = 1106
APP_ID = '1010013'
APP_KEY = 'NsalbZh76U8VGJp1'
AES_KEY = 'ZTM7fu0xYnzkE5Km'

TZ = dt.timezone(dt.timedelta(hours=8))
ENERGY_CAP = 350
ENERGY_REGEN_SECONDS = 300  # 每 5 分钟恢复 1 点
WISH_CAP = 500
DIG_CAP = 4
DIG_DURATION = {4: 4 * 3600, 8: 8 * 3600, 12: 12 * 3600, 20: 20 * 3600}
BOSS_NAME = '幻象愿望大师·奇格格达'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Content-Type': 'application/json',
    'Origin': 'https://myl.nuanpaper.com',
    'Referer': 'https://myl.nuanpaper.com/',
}


class NikkiError(Exception):
    """可展示给用户的业务/网络错误"""


class SessionExpired(NikkiError):
    """登录态失效，且续期失败，需要重新绑定"""


# ---------------- 加解密与签名 ----------------
def aes_encrypt(text: str) -> str:
    cipher = AES.new(AES_KEY.encode(), AES.MODE_CBC, AES_KEY[:16].encode())
    return base64.b64encode(cipher.encrypt(pad(text.encode(), 16))).decode()


def aes_decrypt(data: str) -> str:
    raw = base64.b64decode(data.replace(' ', '+'))
    plain = AES.new(AES_KEY.encode(), AES.MODE_CBC, AES_KEY[:16].encode()).decrypt(raw)
    return unpad(plain, 16).decode()


def sign_of(params: dict) -> str:
    items = []
    for key in sorted(params):
        if key in ('data', 'sign') or params[key] is None:
            continue
        value = urllib.parse.quote(str(params[key]), safe="!~'()").replace('*', '%2A')
        items.append(f'{key}={value}')
    return hmac.new(APP_KEY.encode(), '&'.join(items).encode(), hashlib.md5).hexdigest()


def _load_device() -> dict:
    """读取或生成本机设备标识；加锁防止首次并发绑定生成不同设备"""
    with _device_lock:
        try:
            with open(DEVICE_FILE, 'r', encoding='utf-8') as f:
                device = json.load(f)
            if device.get('doid') and device.get('web_deviceid'):
                return device
        except (OSError, ValueError):
            pass
        device = {
            'doid': 'fe-' + ''.join(random.choices(string.ascii_letters + string.digits, k=29)),
            'web_deviceid': str(uuid.uuid4()) + '-' + dt.datetime.now().strftime('%Y%m%d%H%M%S%f')[:17],
        }
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(DEVICE_FILE, 'w', encoding='utf-8') as f:
                json.dump(device, f)
        except OSError:
            pass
        return device


def passport_request(path: str, payload: dict, decrypt: bool = True) -> dict:
    """调用叠纸通行证接口，统一加密、签名并携带设备标识"""
    device = _load_device()
    data = {'os_platform': 61, 'sdk_channel': 1}
    data.update(payload)
    data['DOID'] = device['doid']
    data['web_deviceid'] = device['web_deviceid']

    body = {
        'app_id': APP_ID,
        'clientid': str(CLIENT_ID),
        'lang': 'zh-cn',
        'sign_type': 'hmac',
        'timestamp': str(int(time.time())),
    }
    body['data'] = aes_encrypt(json.dumps(data, separators=(',', ':')))
    body['sign'] = sign_of(body)
    try:
        resp = requests.post(
            PASSPORT + path, data=body, timeout=25,
            headers={'User-Agent': HEADERS['User-Agent'],
                     'Content-Type': 'application/x-www-form-urlencoded'})
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise NikkiError(f'网络请求失败：{exc}') from exc

    result = resp.json()
    code = result.get('code')
    if code != 0:
        raise NikkiError(f"{result.get('info') or '请求失败'}（code={code}）")
    if decrypt and result.get('data'):
        return json.loads(aes_decrypt(result['data']))
    return {}


def api_post(path: str, token: str, nid: str, payload=None) -> dict:
    """调用手账数据接口（响应可能是 snappy 压缩）"""
    body = {'client_id': CLIENT_ID, 'token': token, 'openid': nid}
    if payload:
        body.update(payload)
    resp = requests.post(API + path, json=body, timeout=25, headers=HEADERS)
    resp.raise_for_status()
    raw = resp.content
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return json.loads(bytes(cramjam.snappy.decompress_raw(raw)).decode('utf-8'))


def fetch_server_time() -> float:
    """取服务器时间（秒）；失败时退回本机时间"""
    try:
        resp = requests.get(TIME_API, timeout=10, headers={'User-Agent': HEADERS['User-Agent']})
        resp.raise_for_status()
        return resp.json()['timestamp'] / 1000.0
    except Exception:
        return time.time()


# ---------------- 登录 ----------------
def send_login_code(phone: str):
    """发送短信验证码；验证码与设备标识绑定，必须用同一个设备再登录"""
    if not re.fullmatch(r'1\d{10}', phone):
        raise NikkiError('手机号格式不正确')
    passport_request('/v1/user/account/send/code',
                     {'account': phone, 'scene': 'web_login'}, decrypt=False)


def login_with_code(phone: str, code: str) -> dict:
    """手机号 + 短信验证码登录，返回可持久化的登录态"""
    if not re.fullmatch(r'1\d{10}', phone):
        raise NikkiError('手机号格式不正确')
    if not re.fullmatch(r'\d{4,8}', code):
        raise NikkiError('验证码格式不正确')

    data = passport_request('/v1/user/mobile/register', {
        'mobile': phone,
        'code': code,
        'profile': True,
        'addresses': True,
        'enableCookie': False,
        'enableLocalStorage': False,
    })
    if not data.get('nid') or not data.get('token'):
        raise NikkiError('登录失败：未获取到登录态')
    return {
        'nid': str(data['nid']),
        'token': data['token'],
        'refresh_token': data.get('refresh_token', ''),
        'account': phone,
    }


def login_with_password(account: str, password: str) -> dict:
    """账号（手机号/邮箱）+ 密码登录，返回可持久化的登录态"""
    if not account:
        raise NikkiError('账号不能为空')
    if not password:
        raise NikkiError('密码不能为空')

    data = passport_request('/v1/user/login', {
        'account': account,
        'password': password,
        'profile': True,
        'addresses': True,
        'enableCookie': False,
        'enableLocalStorage': False,
    })
    if not data.get('nid') or not data.get('token'):
        raise NikkiError('登录失败：未获取到登录态')
    return {
        'nid': str(data['nid']),
        'token': data['token'],
        'refresh_token': data.get('refresh_token', ''),
        'account': account,
    }


def refresh_session(session: dict):
    """用 refresh_token 换新令牌；失败返回 None（调用方转去提示重新绑定）"""
    refresh_token = session.get('refresh_token')
    if not refresh_token:
        return None
    try:
        data = passport_request('/v1/user/login/token/refresh',
                                {'refresh_token': refresh_token})
    except NikkiError:
        return None
    if not data.get('nid') or not data.get('token'):
        return None

    updated = dict(session)
    updated['nid'] = str(data['nid'])
    updated['token'] = data['token']
    if data.get('refresh_token'):
        updated['refresh_token'] = data['refresh_token']
    return updated


# ---------------- 数据 ----------------
def fetch_note_book_full(session: dict):
    """一次性返回 (info_from_gm, info_from_self)，账号总览需要两部分数据"""
    result = api_post('/v1/strategy/user/note/book/info',
                      session['token'], session['nid'])
    code = result.get('code')
    if code == 1801:
        raise SessionExpired('登录已失效')
    if code not in (None, 0):
        raise NikkiError(f"{result.get('info') or '获取数据失败'}（code={code}）")

    gm = result.get('info_from_gm')
    self_data = result.get('info_from_self')
    if isinstance(gm, str):
        gm = json.loads(gm)
    if isinstance(self_data, str):
        self_data = json.loads(self_data)
    if not isinstance(gm, dict) or not gm:
        raise NikkiError('未查询到游戏数据，请先在游戏内登录过一次')
    return gm, self_data if isinstance(self_data, dict) else {}


def fetch_note_book(session: dict) -> dict:
    return fetch_note_book_full(session)[0]


def fetch_with_refresh(session: dict):
    """令牌失效时自动续期并重试一次，返回 (gm, 最新 session)"""
    try:
        return fetch_note_book(session), session
    except SessionExpired:
        updated = refresh_session(session)
        if not updated:
            raise
        # 续期成功；若重试仍失效，会继续抛出 SessionExpired
        return fetch_note_book(updated), updated


# ---------------- 计算与文本渲染 ----------------
def fmt_hm(seconds: float) -> str:
    """HH:MM，分钟向上取整，与网页端一致"""
    hours = int(seconds // 3600)
    minutes = math.ceil((seconds % 3600) / 60)
    if minutes == 60:
        hours += 1
        minutes = 0
    return f'{hours:02d}:{minutes:02d}'


def fmt_hms(seconds: float) -> str:
    total = max(0, int(seconds))
    return f'{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}'


def fmt_refresh(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int(seconds % 86400 // 3600)
    minutes = int(seconds % 3600 // 60)
    if days > 0:
        return f'{days}天{hours}小时'
    if hours > 0:
        return f'{hours}小时'
    if minutes <= 1:
        return '1分'
    return f'{minutes}分'


def today_four(now_local: dt.datetime) -> dt.datetime:
    return now_local.replace(hour=4, minute=0, second=0, microsecond=0)


def monday_four(now_local: dt.datetime) -> dt.datetime:
    """本周（周日为一周起点）周一 04:00"""
    days_since_sunday = (now_local.weekday() + 1) % 7
    sunday = (now_local - dt.timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return sunday + dt.timedelta(days=1, hours=4)


def compute_card(gm: dict, now_ts: float) -> dict:
    """根据接口快照 + 当前服务器时间，计算网页同款的四项状态"""
    now_local = dt.datetime.fromtimestamp(now_ts, TZ)
    snapshot_ms = int((gm.get('timestamp') or 0) * 1000)
    now_ms = int(now_ts * 1000)

    # 活跃能量：按快照时间每 5 分钟恢复 1 点
    raw_energy = gm.get('energy')
    if raw_energy is None:
        energy = 0
        energy_status = '未完成'
    elif snapshot_ms > now_ms:
        energy = ENERGY_CAP
        energy_status = '已恢复满格！'
    else:
        minutes = (now_ms - snapshot_ms) / 60000
        energy = min(int(raw_energy) + int(minutes // 5), ENERGY_CAP)
        if energy >= ENERGY_CAP:
            energy_status = '已恢复满格！'
        else:
            left = (ENERGY_CAP - energy) * ENERGY_REGEN_SECONDS
            energy_status = f'剩余{fmt_hm(left)}恢复满格！'

    # 朝夕心愿：每日 04:00 重置
    raw_wish = gm.get('daily_task')
    today_reset_ms = int(today_four(now_local).timestamp() * 1000)
    if snapshot_ms < today_reset_ms <= now_ms:
        wish = 0
    else:
        wish = int(raw_wish or 0)
    wish_status = '已完成~撒花！' if raw_wish is not None and wish >= WISH_CAP else '未完成'

    # 美鸭梨挖掘：根据开始时间和时长计算状态
    dispatch = (gm.get('dispatch') or [])[:DIG_CAP]
    dispatch = [item for item in dispatch if item.get('text')]
    dispatch = sorted(dispatch, key=lambda item: item.get('start_time') or 0)
    dig_details = []
    for item in dispatch:
        start = item.get('start_time') or 0
        spend_hours = item.get('spend_time') or 0
        duration = DIG_DURATION.get(spend_hours)
        if duration is None:
            dig_details.append((item, 0, 0))
        elif now_ts - start >= duration:
            dig_details.append((item, 2, 0))
        else:
            dig_details.append((item, 1, duration - (now_ts - start)))

    dig_items = []
    for item, status, rest in dig_details:
        if status == 1:
            status_text = f'努力挖掘中！剩余{fmt_hms(rest)}'
        elif status == 2:
            status_text = '挖掘完成！等待收获~'
        else:
            status_text = '当前没有在挖掘喔~'
        dig_items.append({'text': item['text'], 'status_text': status_text})

    statuses = [detail[1] for detail in dig_details]
    if not statuses:
        dig_status = '当前没有在挖掘喔~'
    elif 1 in statuses:
        rest = min(detail[2] for detail in dig_details if detail[1] == 1)
        dig_status = f'努力挖掘中！剩余{fmt_hms(rest)}'
    elif all(status == 2 for status in statuses):
        dig_status = '挖掘完成！等待收获~'
    else:
        dig_status = '当前没有在挖掘喔~'

    # 心之突破幻境：每周一 04:00 重置
    weekly_snapshot = gm.get('weekly_reward_status')
    monday_reset = monday_four(now_local)
    monday_reset_ms = int(monday_reset.timestamp() * 1000)
    if snapshot_ms < monday_reset_ms <= now_ms:
        weekly = 0
    else:
        weekly = weekly_snapshot
    weekly_status = '已挑战' if weekly == 1 else '未挑战'

    next_reset = monday_reset
    while next_reset.timestamp() <= now_ts:
        next_reset += dt.timedelta(days=7)
    refresh_left = next_reset.timestamp() - now_ts

    return {
        'energy': energy,
        'energy_status': energy_status,
        'wish': wish,
        'wish_status': wish_status,
        'dig_count': len(dispatch),
        'dig_status': dig_status,
        'dig_items': dig_items,
        'weekly_status': weekly_status,
        'refresh_text': fmt_refresh(refresh_left),
    }


def format_card(card: dict, session: dict) -> str:
    """QQ 群聊用的简洁文本；不输出手机号，避免泄露"""
    lines = [
        '日程便利贴',
        f"活跃能量：{card['energy']}/{ENERGY_CAP}（{card['energy_status']}）",
        f"朝夕心愿：{card['wish']}/{WISH_CAP}（{card['wish_status']}）",
        f"美鸭梨挖掘：{card['dig_count']}/{DIG_CAP}（{card['dig_status']}）",
    ]
    lines.extend(f"  {item['text']}：{item['status_text']}" for item in card['dig_items'])
    lines.extend([
        f"心之突破幻境：{card['weekly_status']}（{BOSS_NAME}）",
        f"挑战刷新：{card['refresh_text']}",
        f"搭配师：{session.get('nid', '-')}",
    ])
    return '\n'.join(lines)


# ---------------- 账号总览 ----------------
# 探索度上限由网页端配置提供；地图分类总数来自地图图鉴接口
EXPLORE_MAX_URL = ('https://assets.papegames.com/kv/'
                   '2d65c7f3-4ef0-4e93-8ad2-7738b532d8aa/exploreResMax.json')
EXPLORE_MAX_DEFAULT = {
    'whimsystar_max': 378, 'dew_max': 1993, 'pillar_max': 240,
    'yzzt_whimsystar_max': 627, 'yzzt_dew_max': 3219,
    'imagine_ballon_max': 56, 'flame_light_stone_max': 261,
    'imagine_foam_max': 41, 'worry_free_beads_max': 233,
}
WORLD_CACHE_TTL = 6 * 3600
MAX_CACHE_TTL = 3600
_catalog_index_cache = (0.0, None)   # {分类名: catalog_id}
_stage_totals_cache = (0.0, None)    # {catalog_id: 刷新点总数}
_max_cache = (0.0, None)

# 网页端探索总览用到的地图与分类；分类 ID 在全地图通用
WORLD_DANQING = ('10000010', '10000027')
WORLD_WANXIANG = '4020034'
CATALOG_INDEX_WORLDS = ('10000010', '4020034')
CATALOG_FALLBACK_IDS = {'奇想玉琚': 167, '丹青玉魄': 168, '奇想华灯': 244, '万相金铃': 245}

# 故乡/区域探索货币在 currency_count 里的 item_id
CURRENCY_WISHFIELD_STAR = 8
CURRENCY_WISHFIELD_DEW = 7
CURRENCY_YIZAN_STAR = 25
CURRENCY_YIZAN_DEW = 24


def text_of(value) -> str:
    """接口里的 name 可能是多语言 JSON 字符串，统一取中文"""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get('lang') == 'zh-cn':
                return item.get('text') or ''
        return ''
    if isinstance(value, str) and value[:1] in '["':
        try:
            return text_of(json.loads(value))
        except ValueError:
            return value
    return '' if value is None else str(value)


def fetch_explore_max() -> dict:
    """探索度上限配置；失败时退回内置默认值"""
    global _max_cache
    now = time.time()
    if _max_cache[1] is not None and now - _max_cache[0] < MAX_CACHE_TTL:
        return _max_cache[1]

    data = dict(EXPLORE_MAX_DEFAULT)
    try:
        resp = requests.get(EXPLORE_MAX_URL, timeout=15,
                            headers={'User-Agent': HEADERS['User-Agent']})
        resp.raise_for_status()
        live = resp.json().get('exploreResMax') or {}
        for key, value in live.items():
            if isinstance(value, (int, float)):
                data[key] = value
    except Exception:
        pass
    _max_cache = (now, data)
    return data


def fetch_catalog_index(session: dict) -> dict:
    """分类名 -> catalog_id，用于统计关卡刷新点总数"""
    global _catalog_index_cache
    now = time.time()
    if _catalog_index_cache[1] is not None and now - _catalog_index_cache[0] < WORLD_CACHE_TTL:
        return _catalog_index_cache[1]

    index = {}
    for world_id in CATALOG_INDEX_WORLDS:
        try:
            result = api_post('/v1/strategy/map/catalog/list',
                              session['token'], session['nid'], {'world_id': world_id})
        except Exception:
            continue
        if result.get('code') not in (None, 0):
            continue
        for group in (result.get('data') or {}).get('list') or []:
            for item in group.get('catalogs') or []:
                name = text_of(item.get('name'))
                if name and item.get('id') is not None:
                    index.setdefault(name, item['id'])
    _catalog_index_cache = (now, index)
    return index


def fetch_stage_totals(session: dict) -> dict:
    """全关卡刷新点总数（含野外的全部关卡），网页端探索总览用的就是这个口径"""
    global _stage_totals_cache
    now = time.time()
    if _stage_totals_cache[1] is not None and now - _stage_totals_cache[0] < WORLD_CACHE_TTL:
        return _stage_totals_cache[1]

    totals = {}
    try:
        result = api_post('/v1/strategy/map/stage/spawner/list',
                          session['token'], session['nid'])
        for items in (result.get('stages') or {}).values():
            for item in items or []:
                catalog_id = item.get('catalog')
                if catalog_id is not None:
                    totals[catalog_id] = totals.get(catalog_id, 0) + 1
    except Exception:
        return {}
    _stage_totals_cache = (now, totals)
    return totals


def fetch_overview_with_refresh(session: dict):
    """令牌失效时自动续期并重试一次，返回 (总览文本, 最新 session)"""
    try:
        gm, self_data = fetch_note_book_full(session)
    except SessionExpired:
        updated = refresh_session(session)
        if not updated:
            raise
        gm, self_data = fetch_note_book_full(updated)
        session = updated
    return build_overview(gm, self_data, session), session


def _pair(collected, total) -> str:
    """收集度：已收集/总数；总数缺失时用 ? 占位，且总数不会小于已收集"""
    collected = int(collected or 0)
    total = int(total or 0)
    if total <= 0:
        return f'{collected}/?'
    return f'{collected}/{max(collected, total)}'


def _record(gm: dict, key: str, label: str) -> str:
    """奇想札记里的“最常收集”记录；没有数据时显示 —"""
    item = gm.get(key)
    if not isinstance(item, dict) or not item.get('num') or not item.get('name'):
        return f'- {label}：—'
    return f"- {label}：{item['name']} ×{item['num']}"


def build_overview(gm: dict, self_data: dict, session: dict) -> str:
    """按网页端四个板块生成 Markdown 账号总览"""
    maxima = fetch_explore_max()
    name_to_id = fetch_catalog_index(session)
    stage_totals = fetch_stage_totals(session)

    def currency(item_id):
        for item in gm.get('currency_count') or []:
            if item.get('item_id') == item_id:
                return int(item.get('count') or 0)
        return 0

    def total_of(name):
        catalog_id = name_to_id.get(name) or CATALOG_FALLBACK_IDS.get(name)
        return int(stage_totals.get(catalog_id, 0)) if catalog_id else 0

    pillar = int(gm.get('pillar_num') or 0)
    lantern = int(gm.get('imagine_lantern_num') or 0)
    bell = int(gm.get('imagine_bell_num') or 0)
    jade = int(gm.get('imagine_jade_num') or 0)
    danqing = int(gm.get('danqing_jade_num') or 0)
    balloon = int(gm.get('imagine_ballon_num') or 0)
    flame = int(gm.get('flame_light_stone_num') or 0)
    foam = int(gm.get('imagine_foam_num') or 0)
    beads = int(gm.get('worry_free_beads_num') or 0)

    login_days = int(self_data.get('login_days') or 0)
    play_hours = (self_data.get('total_play_time') or 0) / 3600
    box_count = self_data.get('box_count')
    if box_count is None:
        box_count = gm.get('box_num') or 0

    danqing_jade_total = total_of('奇想玉琚')
    danqing_soul_total = total_of('丹青玉魄')

    heaviest = gm.get('heaviest_caught_fish') or {}
    if isinstance(heaviest, dict) and heaviest.get('num') and heaviest.get('name'):
        heaviest_text = f"- 最重鱼获：{heaviest['name']} {(heaviest['num'] / 10):.2f}kg"
    else:
        heaviest_text = '- 最重鱼获：—'

    return '\n'.join([
        '# 无限暖暖 · 账号总览',
        '',
        f"> 称号：{gm.get('title_str') or '—'}",
        f'> 登录天数：{login_days} 天｜游戏时长：{play_hours:.2f} 小时',
        '',
        '## 账号数据',
        f"- 服装数量：{gm.get('cloth_num') or 0}",
        f'- 背包宝箱：{box_count}',
        f"- 成就数量：{gm.get('achievement_num') or 0}",
        f"- 设计图：{gm.get('designdrawing_num') or 0}",
        f"- 大喵斗篷：{gm.get('momo_num') or 0}",
        f"- 共鸣抽卡：{gm.get('draw_num') or 0} 次"
        f"（永久 {gm.get('permanent_draw_num') or 0}｜周期 {gm.get('periodic_draw_num') or 0}）",
        '',
        '## 探索总览',
        '**伊赞之土**',
        f"- 奇想星：{_pair(currency(CURRENCY_YIZAN_STAR), maxima.get('yzzt_whimsystar_max'))}",
        f"- 灵感露珠：{_pair(currency(CURRENCY_YIZAN_DEW), maxima.get('yzzt_dew_max'))}",
        f"- 流转之柱：{_pair(pillar, maxima.get('pillar_max'))}",
        f'- 万相境：奇想华灯 {_pair(lantern, total_of("奇想华灯"))}'
        f'｜万相金铃 {_pair(bell, total_of("万相金铃"))}',
        '',
        '**心愿原野**',
        f"- 奇想星：{_pair(currency(CURRENCY_WISHFIELD_STAR), maxima.get('whimsystar_max'))}",
        f"- 灵感露珠：{_pair(currency(CURRENCY_WISHFIELD_DEW), maxima.get('dew_max'))}",
        f"- 流转之柱：{_pair(pillar, maxima.get('pillar_max'))}",
        f'- 花焰群岛：奇想气球 {_pair(balloon, maxima.get("imagine_ballon_max"))}'
        f'｜焰光结晶 {_pair(flame, maxima.get("flame_light_stone_max"))}',
        f'- 无忧岛：奇想气泡 {_pair(foam, maxima.get("imagine_foam_max"))}'
        f'｜无忧凝珠 {_pair(beads, maxima.get("worry_free_beads_max"))}',
        f'- 丹青屿：奇想玉琚 {_pair(jade, danqing_jade_total)}'
        f'｜丹青玉魄 {_pair(danqing, danqing_soul_total)}',
        '',
        '## 奇想札记',
        f"- 阅读物图鉴：已解锁 {gm.get('reading_exploration') or 0}",
        f"- 生物图鉴：已解锁 {gm.get('creature_explore_num') or 0}",
        _record(gm, 'most_collected_plant', '最常收集植物'),
        _record(gm, 'most_cleaned_animal', '最多清洁动物'),
        _record(gm, 'most_collected_insect', '最多收集昆虫'),
        _record(gm, 'most_purified_monster', '最多净化魔物'),
        _record(gm, 'most_caught_fish', '最多钓到鱼'),
        heaviest_text,
        '',
        '## 奇迹之冠',
        f"- 奇迹之冠搭配赛：{gm.get('permanent_tower') or 0}/15 层",
        f"- 奇迹之冠巅峰赛：{gm.get('periodic_tower') or 0}/8 层",
    ])


# ---------------- 卡池查询 ----------------
# 版本按卡池 ID 固定映射，不能用开始时间推断（复刻会改变开始时间）。
CARD_POOL_VERSIONS = {
    '1.0': ('2', '3'), '1.1': ('6',), '1.2': ('9', '10'),
    '1.3': ('13',), '1.4': ('16',), '1.5': ('19', '20'),
    '1.6': ('23',), '1.7': ('26',), '1.8': ('29', '30'),
    '1.9': ('33',), '1.10': ('36',), '1.11': ('39',),
    '2.0': ('42', '43'), '2.1': ('46',), '2.2': ('49', '50'),
    '2.3': ('53',), '2.4': ('56',), '2.5': ('59', '60'),
    '2.6': ('63',), '2.7': ('66',), '2.8': ('69', '70'),
    '2.9': ('73',), '2.10': ('76', '77'), '2.11': ('80',),
}
DOUBLE_FOUR_POOLS = {
    '1.0': ('4', '5'), '1.1': ('7', '8'), '1.2': ('11', '12'),
    '1.3': ('14', '15'), '1.4': ('17', '18'), '1.5': ('21', '22'),
    '1.6': ('24', '25'), '1.7': ('27', '28'), '1.8': ('31', '32'),
    '1.9': ('34', '35'), '1.10': ('37', '38'), '1.11': ('40', '41'),
    '2.0': ('44', '45'), '2.1': ('47', '48'), '2.2': ('51', '52'),
    '2.3': ('54', '55'), '2.4': ('57', '58'), '2.5': ('61', '62'),
    '2.6': ('64', '65'), '2.7': ('67', '68'), '2.8': ('71', '72'),
    '2.9': ('74', '75'), '2.10': ('78', '79'),
}
SUIT_ALIASES = {
    '蝴蝶': '蝶屿风袭', '精灵': '花漾梦萦', '纸鹤': '翩翩愿飞去',
    '烟花': '缀夜希光', '气球': '绽响黎明前', '八音盒': '古典乐章',
    '玩偶': '绯夜狂想录', '鱼': '雪境长歌', '鸟': '炽羽不渝',
    '教母': '仙踪绮遇', '蛇': '落入泪之潮', '龙': '龙隐云墨间',
    '猫': '缘缘长相念', '歌剧': '咏叙的旧梦', '女巫': '如雨坠，如花开',
    '哥特': '无名亦无我', '圣女': '隐于祷音', '神女': '生于无瑕',
    '萤火虫': '森之涟', '扇': '万相渡浮生', '鹿': '九色亦尘心',
    '独角兽': '予梦心时', '小红帽': '她是不驯火', '骨': '渡者无归',
    '花': '栖骨生花', '竖琴': '黎明无声', '水花': '梦是心之涟',
    '芭蕾': '心光破晓时', '狮鬃': '白夜长旋舞', '香妃': '花影心期',
    '埃及': '蒙尘之罪', '圣诞': '绽若颂愿心', '玫瑰': '如果爱上一朵花',
    '限定·玫瑰': '如果爱上一朵花',
}
_suit_catalog_cache = (0.0, None)


def _parsed_json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def fetch_suit_catalog(session: dict) -> list:
    """套装/卡池目录，全服共用，缓存 6 小时"""
    global _suit_catalog_cache
    now = time.time()
    if _suit_catalog_cache[1] is not None and now - _suit_catalog_cache[0] < WORLD_CACHE_TTL:
        return _suit_catalog_cache[1]

    result = api_post('/v1/strategy/main/suit/list',
                      session['token'], session['nid'])
    code = result.get('code')
    if code == 1801:
        raise SessionExpired('登录已失效')
    if code not in (None, 0):
        raise NikkiError(f"{result.get('info') or '获取套装目录失败'}（code={code}）")
    suits = (result.get('data') or {}).get('list') or []
    if not suits:
        raise NikkiError('套装目录为空，请稍后重试')
    _suit_catalog_cache = (now, suits)
    return suits


def _suit_statistics(suit: dict, records: list, owned: set) -> dict:
    """与网页端一致：每个部件最多统计前两套，累加同星级出货间隔。"""
    pool_id = str(suit.get('card_pool_id'))
    level = int(suit.get('level') or 0)
    cloths = _parsed_json(suit.get('cloths'), [])
    field = 'times_from_last_five_stars' if level == 5 else 'times_from_last_four_stars'
    total = 0
    hits = 0
    for cloth in cloths:
        cloth_id = str(cloth.get('cloth_id'))
        same_cloth = [r for r in records
                      if str(r.get('card_pool_id')) == pool_id
                      and int(r.get('rarity') or 0) == level
                      and str(r.get('result')) == cloth_id]
        same_cloth.sort(key=lambda r: int(r.get('pool_cnt') or 0))
        for record in same_cloth[:2]:
            total += int(record.get(field) or 0) + 1
            hits += 1

    average = total / hits if hits else 0
    suit_id = str(suit.get('suit_id'))
    collected = suit_id in owned
    evolution = 0
    if collected:
        max_evolution = 3 if level == 5 else 1
        for number in range(1, max_evolution + 1):
            data = _parsed_json(suit.get(f'evolutions{number}'), {})
            if str(data.get('evolution_suit_id')) in owned:
                evolution = number
    return {
        'total': total,
        'average': average,
        'collected': collected,
        'evolution': evolution,
    }


def _format_average(value: float) -> str:
    # JavaScript toFixed(1) 按浮点数的精确值进行 half-up 舍入，
    # Python 默认是 half-even，边界值会相差 0.1。
    rounded = Decimal.from_float(float(value)).quantize(
        Decimal('0.1'), rounding=ROUND_HALF_UP)
    return format(rounded, 'f').rstrip('0').rstrip('.')


def build_gacha_query(query: str, self_data: dict, suits: list) -> str:
    """解析简称、版本、版本双四和常驻池，生成 Markdown 查询结果"""
    compact = re.sub(r'\s+', '', query or '')
    selected = []
    title = compact

    if compact == '常驻':
        selected = [s for s in suits if str(s.get('card_pool_id')) == '1']
        title = '常驻 · 遥远之海'
    else:
        version_match = re.fullmatch(r'[Vv]?(\d+\.\d+)(双四(?:星)?)?', compact)
        if version_match:
            version, double_four = version_match.groups()
            pool_ids = (DOUBLE_FOUR_POOLS if double_four else CARD_POOL_VERSIONS).get(version)
            if not pool_ids:
                label = '双四星卡池' if double_four else '卡池'
                raise NikkiError(f'暂未收录 {version} 的{label}。')
            selected = [s for s in suits
                        if str(s.get('card_pool_id')) in pool_ids
                        and (not double_four or int(s.get('level') or 0) == 4)]
            title = f'{version}{"双四" if double_four else ""}'
            order = {pool_id: index for index, pool_id in enumerate(pool_ids)}
            selected.sort(key=lambda s: (order[str(s.get('card_pool_id'))],
                                         -int(s.get('level') or 0),
                                         int(s.get('suit_id') or 0)))
        else:
            alias_key = compact[:-1] if compact.endswith('套') else compact
            target_name = SUIT_ALIASES.get(alias_key, alias_key)
            selected = [s for s in suits if text_of(s.get('name')) == target_name]
            if alias_key in SUIT_ALIASES:
                selected = [s for s in selected
                            if int(s.get('level') or 0) == 5 and int(s.get('card_type') or 0) == 2]
            if not selected:
                raise NikkiError(
                    f'未识别“{query}”。可使用套装简称（如蝴蝶套）、版本（如1.0）、'
                    '版本双四（如1.0双四）或“常驻”。'
                )

    if not selected:
        raise NikkiError('该卡池暂未查询到套装数据。')

    records = self_data.get('gacha_list') or []
    owned = {str(suit_id) for suit_id in self_data.get('suit_list') or []}
    lines = [f'# 无限暖暖 · 卡池查询（{title}）']
    current_pool = None
    for suit in selected:
        pool_id = str(suit.get('card_pool_id'))
        if pool_id != current_pool:
            current_pool = pool_id
            lines.extend(['', f'## {text_of(suit.get("card_pool_name")) or "未知卡池"}'])
        stat = _suit_statistics(suit, records, owned)
        level = int(suit.get('level') or 0)
        evolution = f'套装进化{stat["evolution"]}' if stat['evolution'] else '0'
        lines.extend([
            '',
            f'**{text_of(suit.get("name"))}｜{"五星" if level == 5 else "四星"}**',
            f'- 平均共鸣次数：{_format_average(stat["average"])}'
            f'｜总计共鸣次数：{stat["total"]}',
            f'- 是否集齐：{"是" if stat["collected"] else "否"}｜最高进化：{evolution}',
        ])
    return '\n'.join(lines)


def fetch_gacha_with_refresh(session: dict, query: str):
    """获取共鸣与套装数据，令牌失效时自动续期，返回 (文本, 最新 session)"""
    try:
        _gm, self_data = fetch_note_book_full(session)
        suits = fetch_suit_catalog(session)
    except SessionExpired:
        updated = refresh_session(session)
        if not updated:
            raise
        session = updated
        _gm, self_data = fetch_note_book_full(session)
        suits = fetch_suit_catalog(session)
    return build_gacha_query(query, self_data, suits), session
