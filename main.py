# -*- coding: utf-8 -*-
"""
siwu-steam-status-monitor：Steam 状态监控插件（Amiya-Bot 移植版）

移植自 astrbot_plugin_steam_status_monitor（作者：Maoer，GPL-3.0）：
    https://github.com/Maoer233/astrbot_plugin_steam_status_monitor

功能（与原版一致，命令改为 Amiya-Bot 前缀触发风格）：
    - 分群监控 Steam 玩家在线状态，智能/固定轮询，批量查询减少 API 调用
    - 开始游戏 / 结束游戏 / 成就解锁 / 网络波动 自动通知（图片卡片 + 文本）
    - 游戏时长排行榜（今日/最近 N 天/全群），每日定时推送昨日榜单
    - 用户 - SteamID 绑定（steam addid ... @用户 [备注]）
    - 联动推送组（push_group / delpush_group）、分群管理、缓存清理
    - Steam / SteamGridDB / 商店封面 / 头像框 图片缓存与超时控制

所有配置通过兔兔控制台（config_default.yaml + jsonSchema.json）管理。

命令入口（需带兔兔前缀，或 @兔兔）：
    兔兔steam on / off                启动/停止本群监控
    兔兔steam list                    本群玩家状态（图片）
    兔兔steam alllist [img|text]      所有群玩家状态
    兔兔steam addid [SteamID] [@用户] 添加监控玩家（可绑定）
    兔兔steam delid [SteamID]         删除监控玩家
    兔兔steam openbox [SteamID]       查看 SteamID 全部信息
    兔兔steamwho @用户 / 兔兔在干嘛 @用户  查询某人绑定账号状态
    兔兔steam rank / allrank          排行榜
    兔兔steam rank_on [all|list|test|del]  每日排行榜推送管理
    兔兔steam help                    帮助
    …… 等
"""

import asyncio
import io
import json
import os
import random
import re
import shlex
import shutil
import tempfile
import time
import traceback

from datetime import datetime, timedelta
from PIL import Image as PILImage
from PIL import ImageChops

from core import AmiyaBotPluginInstance, Message, Chain, log, bot as main_bot

from .http_util import http_get
from .openbox import handle_openbox
from .steam_list import handle_steam_list
from .achievement_monitor import AchievementMonitor
from .game_start_render import render_game_start, set_cache_config
from .game_end_render import render_game_end
from .rank_render import render_rank_image
from .rank_push import build_rank_push_scopes
from .superpower_util import load_abilities, get_daily_superpower

curr_dir = os.path.dirname(os.path.abspath(__file__))

bot = AmiyaBotPluginInstance(
    name="Steam 状态监控",
    version="1.1",
    plugin_id="siwu-steam-status-monitor",
    plugin_type="functional",
    description="监控群内 Steam 玩家在线状态与游戏动态：开始/结束游戏、成就解锁、网络波动自动推送，游戏时长排行榜与每日榜单推送，支持分群管理与绑定。",
    document=f"{curr_dir}/README.md",
    global_config_default=f"{curr_dir}/config_default.yaml",
    global_config_schema=f"{curr_dir}/jsonSchema.json",
)

# 数据目录（Amiya-Bot/data/steam_status_monitor）
DATA_DIR = os.path.abspath(os.path.join(curr_dir, '..', '..', 'data'))


def _cfg(key: str, default=None):
    """从控制台读取全局配置，未设置时返回默认值。"""
    val = bot.get_config(key, channel_id=None)
    return default if val is None else val


def _resolve_instance(bot_id: str = ''):
    """多账号时取对应实例；找不到时回退到第一个可用实例。"""
    try:
        inst = main_bot[bot_id]
        if inst is not None:
            return inst
    except Exception:
        pass
    for inst in main_bot:
        if inst is not None:
            return inst
    return None


async def _push_chain(group_id, chain):
    """向指定群/频道主动推送 Chain。"""
    inst = _resolve_instance(_st.group_bot_ids.get(str(group_id), ''))
    if inst is None:
        log.warning(f"[Steam状态监控] 找不到可用的 bot 实例，跳过推送 group={group_id}")
        return
    try:
        await inst.send_message(chain, channel_id=str(group_id))
    except Exception as e:
        log.warning(f"[Steam状态监控] 主动推送失败 group={group_id}: {e}")


class SteamStatusMonitorV3:
    """Steam 状态监控核心逻辑（移植自原插件 SteamStatusMonitorV3）。"""

    def __init__(self):
        self._plugin_version = "1.1"
        self.API_KEY = _cfg('steam_api_key', '')
        self.SGDB_API_KEY = _cfg('sgdb_api_key', '')
        self.STEAM_API_BASE = (_cfg('steam_api_base', '') or 'https://api.steampowered.com').rstrip('/')
        self.STEAM_STORE_BASE = (_cfg('steam_store_base', '') or 'https://store.steampowered.com').rstrip('/')
        self.SGDB_API_BASE = (_cfg('sgdb_api_base', '') or 'https://www.steamgriddb.com').rstrip('/')
        self.RETRY_TIMES = max(1, int(_cfg('retry_times', 3)))
        # 代理
        self.ENABLE_PROXY = bool(_cfg('enable_proxy', False))
        self.PROXY_URL = _cfg('proxy_url', '') or ''
        self.proxy = self.PROXY_URL if self.ENABLE_PROXY and self.PROXY_URL else None
        # 轮询
        self.fixed_poll_interval = int(_cfg('fixed_poll_interval', 0))
        self.smart_poll_intervals = self._parse_smart_intervals()
        self.detailed_poll_log = bool(_cfg('detailed_poll_log', True))
        self.max_group_size = int(_cfg('max_group_size', 20))
        # 缓存配置写入渲染模块
        set_cache_config({
            "avatar": int(_cfg('cache_avatar_hours', 24)) * 3600,
            "avatar_frame": int(_cfg('cache_avatar_frame_hours', 720)) * 3600,
            "cover_vertical": int(_cfg('cache_cover_vertical_hours', 0)) * 3600,
        })
        # 数据持久化目录
        self.data_dir = os.path.join(DATA_DIR, "steam_status_monitor")
        os.makedirs(self.data_dir, exist_ok=True)
        # 分群状态数据
        self.group_steam_ids = {}            # {group_id: [steamid, ...]}
        self.group_last_states = {}          # {group_id: {steamid: status}}
        self.group_start_play_times = {}     # {group_id: {steamid: {gameid: start_time}}}
        self.group_last_quit_times = {}      # {group_id: {steamid: {gameid: quit_time}}}
        self.group_pending_logs = {}         # {group_id: {steamid: {gameid: log_dict}}}
        self.group_recent_games = {}         # {group_id: [gameid, ...]}
        self.group_pending_quit = {}         # {group_id: {steamid: {gameid: pending_info}}}
        self.next_poll_time = {}             # {group_id: {steamid: next_time}}
        # 运行状态
        self.running_groups = set()
        self.group_monitor_enabled = {}      # {group_id: bool}
        self.group_achievement_enabled = {}  # {group_id: bool}
        self.notify_sessions = {}            # {group_id: group_id}（Amiya-Bot 直接用群ID作为推送目标）
        self.group_bot_ids = {}              # {group_id: bot_id} 记录消息来自哪个账号实例
        # 超能力
        self._superpower_cache = {}
        self._abilities = None
        self._abilities_path = os.path.join(os.path.dirname(__file__), "abilities.txt")
        # 游戏名缓存
        self._game_name_cache = {}
        # 成就监控
        self.achievement_monitor = AchievementMonitor(self.data_dir, steam_api_base=self.STEAM_API_BASE, proxy=self.proxy)
        self.max_achievement_notifications = 5
        self.achievement_poll_tasks = {}
        self.achievement_snapshots = {}
        self.achievement_fail_count = {}
        # 持久化数据脏标志 + 节流保存
        self._data_dirty = False
        self._last_save_time = time.time()
        self._save_interval = 300
        self._last_round_logs = []
        self._pending_end_notifications = {}
        self._pending_quit_tasks = {}
        self._session_dirty = False
        # 排行榜
        self.play_records = {}
        self.session_records = {}
        self._recorded_quit_cache = {}
        self.rank_push_groups = []
        self.rank_push_all = False
        self.rank_push_hour = int(_cfg('rank_push_hour', 8))
        self.rank_push_minute = int(_cfg('rank_push_minute', 30))
        self._last_rank_push_date = None
        # 绑定
        self._bind_data = {}
        # 首次轮询标记
        self._init_done = False
        # 加载数据
        self._load_group_steam_ids()
        self._load_persistent_data()
        self._load_notify_session()
        self._load_push_groups()
        self._load_play_records()
        self._load_session_records()
        self._load_rank_push_groups()
        self._load_bind_data()
        # 重启后自动恢复所有群的轮询
        if self.notify_sessions and self.API_KEY and self.group_steam_ids:
            for group_id in self.notify_sessions:
                if group_id in self.group_steam_ids:
                    self.running_groups.add(group_id)

    # ========== 配置与工具 ==========

    @staticmethod
    def _parse_smart_intervals():
        raw = _cfg('smart_poll_intervals', "1,3,5,10,20,30") or "1,3,5,10,20,30"
        try:
            if isinstance(raw, str):
                intervals = [int(x.strip()) for x in raw.split(",") if x.strip()]
            else:
                intervals = [int(x) for x in raw]
        except Exception:
            intervals = [1, 3, 5, 10, 20, 30]
        return intervals if len(intervals) == 6 else [1, 3, 5, 10, 20, 30]

    def get_font_path(self, font_name=None, bold=False):
        """返回插件 fonts 目录下的字体路径。"""
        if not font_name:
            font_name = 'NotoSansHans-Regular.otf'
        if bold:
            font_name = 'NotoSansHans-Medium.otf'
        path = os.path.join(os.path.dirname(__file__), 'fonts', font_name)
        return path if os.path.exists(path) else font_name

    def get_today_superpower(self, steamid):
        from datetime import date
        today = date.today().isoformat()
        cache_key = (steamid, today)
        if cache_key in self._superpower_cache:
            return self._superpower_cache[cache_key]
        if self._abilities is None:
            self._abilities = load_abilities(self._abilities_path)
        superpower = get_daily_superpower(steamid, self._abilities)
        self._superpower_cache[cache_key] = superpower
        return superpower

    def _get_day_key(self, offset_days=0):
        """基于凌晨4:00边界的日期键。"""
        now = datetime.now()
        if now.hour < 4:
            now = now - timedelta(days=1)
        now = now + timedelta(days=offset_days)
        return now.strftime("%Y-%m-%d")

    def _should_skip_game(self, gameid):
        """根据黑白名单配置判断是否应跳过该游戏的监控/播报。"""
        if not gameid:
            return False
        mode = _cfg('game_filter_mode', '全部游戏')
        if mode == '全部游戏':
            return False
        ids_str = _cfg('game_filter_ids', '') or ''
        if not ids_str.strip():
            return False
        try:
            filter_ids = [x.strip() for x in ids_str.split(',') if x.strip()]
        except Exception:
            return False
        if mode == '白名单':
            return str(gameid) not in filter_ids
        elif mode == '黑名单':
            return str(gameid) in filter_ids
        return False

    # ========== 数据持久化 ==========

    def _get_group_data_path(self, group_id, key):
        return os.path.join(self.data_dir, f"group_{group_id}_{key}.json")

    def _load_persistent_data(self):
        for group_id in self.group_steam_ids:
            for key, attr in (
                ("states", "group_last_states"),
                ("start_play_times", "group_start_play_times"),
                ("last_quit_times", "group_last_quit_times"),
                ("pending_logs", "group_pending_logs"),
                ("pending_quit", "group_pending_quit"),
                ("recent_games", "group_recent_games"),
            ):
                try:
                    path = self._get_group_data_path(group_id, key)
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as f:
                            getattr(self, attr)[group_id] = json.load(f)
                except Exception as e:
                    log.warning(f"加载 group_{key} 失败: {e} (group_id={group_id})")
            # 数据迁移：旧格式 int → 新格式 {gameid: timestamp}
            migrated = 0
            for _sid, _val in list(self.group_start_play_times.get(group_id, {}).items()):
                if not isinstance(_val, dict):
                    self.group_start_play_times[group_id][_sid] = {}
                    migrated += 1
            if migrated:
                log.info(f"[数据迁移] group_id={group_id}: {migrated} 个玩家 start_play_times 从 int 迁移为 dict")

    def _save_persistent_data(self, force=False):
        if not force and (time.time() - self._last_save_time) < self._save_interval:
            self._data_dirty = True
            return
        self._data_dirty = False
        self._last_save_time = time.time()
        for group_id in self.group_steam_ids:
            for key, attr in (
                ("states", "group_last_states"),
                ("start_play_times", "group_start_play_times"),
                ("last_quit_times", "group_last_quit_times"),
                ("pending_logs", "group_pending_logs"),
                ("pending_quit", "group_pending_quit"),
                ("recent_games", "group_recent_games"),
            ):
                try:
                    path = self._get_group_data_path(group_id, key)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(getattr(self, attr).get(group_id, {}), f, ensure_ascii=False)
                except Exception as e:
                    log.warning(f"保存 group_{key} 失败: {e} (group_id={group_id})")
        try:
            self._save_play_records()
        except Exception as e:
            log.warning(f"保存 play_records 失败: {e}")
        try:
            self._save_session_records()
        except Exception as e:
            log.warning(f"保存 session_records 失败: {e}")

    def _load_notify_session(self):
        path = os.path.join(self.data_dir, "notify_sessions.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.notify_sessions = json.load(f)
            except Exception as e:
                log.warning(f"加载 notify_sessions 失败: {e}")
        else:
            self.notify_sessions = {}

    def _save_notify_session(self):
        path = os.path.join(self.data_dir, "notify_sessions.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.notify_sessions, f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"保存 notify_sessions 失败: {e}")

    def _get_groups_file_path(self):
        return os.path.join(self.data_dir, "steam_groups.json")

    def _load_group_steam_ids(self):
        path = self._get_groups_file_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.group_steam_ids = json.load(f)
            except Exception as e:
                log.warning(f"加载 steam_groups.json 失败: {e}")
        else:
            self.group_steam_ids = {}

    def _save_group_steam_ids(self):
        path = self._get_groups_file_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.group_steam_ids, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"保存 steam_groups.json 失败: {e}")

    def _get_push_groups_path(self):
        return os.path.join(self.data_dir, "push_groups.json")

    def _load_push_groups(self):
        path = self._get_push_groups_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.push_groups = json.load(f)
            except Exception as e:
                log.warning(f"加载 push_groups.json 失败: {e}")
        else:
            self.push_groups = {}

    def _save_push_groups(self):
        path = self._get_push_groups_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.push_groups, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"保存 push_groups.json 失败: {e}")

    def _load_play_records(self):
        path = os.path.join(self.data_dir, "play_records.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.play_records = json.load(f)
            except Exception as e:
                log.warning(f"加载 play_records.json 失败: {e}")
                self.play_records = {}
        else:
            self.play_records = {}

    def _save_play_records(self):
        if not hasattr(self, 'play_records'):
            return
        cutoff_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        self.play_records = {d: data for d, data in self.play_records.items() if d >= cutoff_date}
        path = os.path.join(self.data_dir, "play_records.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.play_records, f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"保存 play_records.json 失败: {e}")

    def _load_session_records(self):
        path = os.path.join(self.data_dir, "session_records.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.session_records = json.load(f)
            except Exception as e:
                log.warning(f"加载 session_records.json 失败: {e}")
                self.session_records = {}
        else:
            self.session_records = {}

    def _save_session_records(self):
        if not hasattr(self, "session_records"):
            return
        cutoff_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        cleaned = {}
        for sid, sessions in self.session_records.items():
            cleaned[sid] = [s for s in sessions if s.get("date", "") >= cutoff_date]
        self.session_records = cleaned
        path = os.path.join(self.data_dir, "session_records.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.session_records, f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"保存 session_records.json 失败: {e}")

    def _record_session(self, sid, gameid, game_name, start_time, end_time, duration_min, group_id):
        if duration_min <= 0 or not gameid:
            return
        date_str = self._get_day_key(0)
        session = {
            "session_id": f"{date_str}_{start_time}_{gameid}",
            "gameid": str(gameid),
            "game_name": str(game_name),
            "start_time": int(start_time) if start_time else 0,
            "end_time": int(end_time) if end_time else 0,
            "duration_min": int(duration_min),
            "date": date_str,
            "group_id": str(group_id),
        }
        self.session_records.setdefault(str(sid), []).append(session)
        self._session_dirty = True

    def _load_bind_data(self):
        path = os.path.join(self.data_dir, "bind_data.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._bind_data = json.load(f)
            except Exception as e:
                log.warning(f"加载 bind_data.json 失败: {e}")
                self._bind_data = {}
        else:
            self._bind_data = {}

    def _save_bind_data(self):
        path = os.path.join(self.data_dir, "bind_data.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._bind_data, f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"保存 bind_data.json 失败: {e}")

    def _load_rank_push_groups(self):
        path = os.path.join(self.data_dir, "rank_push_groups.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    self.rank_push_groups = raw.get("groups", [])
                    self.rank_push_all = raw.get("all", False)
                elif isinstance(raw, list):
                    self.rank_push_groups = raw
                    self.rank_push_all = False
                else:
                    self.rank_push_groups = []
                    self.rank_push_all = False
            except Exception as e:
                log.warning(f"加载 rank_push_groups.json 失败: {e}")
                self.rank_push_groups = []
                self.rank_push_all = False
        else:
            self.rank_push_groups = []
            self.rank_push_all = False

    def _save_rank_push_groups(self):
        path = os.path.join(self.data_dir, "rank_push_groups.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"groups": self.rank_push_groups, "all": self.rank_push_all}, f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"保存 rank_push_groups.json 失败: {e}")

    def _resolve_bind_name(self, sid, steam_name=None):
        """根据绑定表返回显示名：自定义备注 > QQ昵称 > Steam原始名。"""
        bind_data = getattr(self, '_bind_data', {})
        for qq, info in bind_data.items():
            if info.get("sid") == str(sid):
                nick = info.get("nickname", "")
                if nick and nick != "*":
                    return nick
                break
        return steam_name or str(sid)

    # ========== Steam API ==========

    async def fetch_player_status(self, steam_id, retry=None):
        """拉取单个玩家的 Steam 状态，失败自动重试多次并指数退避。"""
        url = (
            f"{self.STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
            f"?key={self.API_KEY}&steamids={steam_id}"
        )
        delay = 1
        retry = retry if retry is not None else self.RETRY_TIMES
        for attempt in range(retry):
            try:
                resp = await http_get(url, proxy=self.proxy, timeout=15)
                if resp is None:
                    raise Exception("无响应")
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                try:
                    data = await resp.json()
                except Exception as je:
                    raise Exception(f"JSON解析失败: {je}")
                resp_data = data.get('response')
                if not isinstance(resp_data, dict):
                    raise Exception(f"Steam 返回异常响应（类型={type(resp_data).__name__}），疑似 API Key 无效或触发限流")
                if not resp_data.get('players'):
                    raise Exception("响应中无玩家数据")
                player = resp_data['players'][0]
                return {
                    'name': player.get('personaname'),
                    'gameid': player.get('gameid'),
                    'lastlogoff': player.get('lastlogoff'),
                    'gameextrainfo': player.get('gameextrainfo'),
                    'personastate': player.get('personastate', 0),
                    'avatarfull': player.get('avatarfull'),
                    'avatar': player.get('avatar')
                }
            except Exception as e:
                log.warning(f"拉取 Steam 状态失败: {e} (SteamID: {steam_id}, 第{attempt+1}次重试)")
                if attempt < retry - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
        log.error(f"SteamID {steam_id} 状态获取失败，已重试{retry}次")
        return None

    async def fetch_player_statuses_batch(self, steam_ids, retry=None):
        """批量拉取多个玩家的 Steam 状态（单次请求最多 100 个 ID），返回 {steamid: status}。"""
        if not steam_ids or not self.API_KEY:
            return {}
        result = {}
        retry = retry if retry is not None else self.RETRY_TIMES
        BATCH_SIZE = 100
        id_batches = [steam_ids[i:i + BATCH_SIZE] for i in range(0, len(steam_ids), BATCH_SIZE)]
        for batch in id_batches:
            ids_str = ",".join(batch)
            url = (
                f"{self.STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
                f"?key={self.API_KEY}&steamids={ids_str}"
            )
            delay = 1
            for attempt in range(retry):
                try:
                    resp = await http_get(url, proxy=self.proxy, timeout=15)
                    if resp is None:
                        raise Exception("无响应")
                    if resp.status != 200:
                        raise Exception(f"HTTP {resp.status}")
                    data = await resp.json()
                    resp_data = data.get('response')
                    if not isinstance(resp_data, dict):
                        log.warning("[批量查询] Steam 返回异常响应，疑似 API Key 无效或触发限流，本批降级处理")
                        resp_data = {}
                    for player in resp_data.get('players') or []:
                        sid = player.get('steamid')
                        if sid and sid in batch:
                            result[sid] = {
                                'name': player.get('personaname'),
                                'gameid': player.get('gameid'),
                                'lastlogoff': player.get('lastlogoff'),
                                'gameextrainfo': player.get('gameextrainfo'),
                                'personastate': player.get('personastate', 0),
                                'avatarfull': player.get('avatarfull'),
                                'avatar': player.get('avatar')
                            }
                    missing = [s for s in batch if s not in result]
                    if missing:
                        log.warning(f"[批量查询] 以下 SteamID 在响应中缺失（可能无效/隐私）: {missing}")
                    break
                except Exception as e:
                    log.warning(f"[批量查询] 失败: {e} (本批 {len(batch)} 个 ID, 第{attempt+1}次重试)")
                    if attempt < retry - 1:
                        await asyncio.sleep(delay)
                        delay *= 2
                    else:
                        log.error(f"[批量查询] 本批彻底失败，降级为单查: {batch}")
                        for sid in batch:
                            if sid not in result:
                                try:
                                    single = await asyncio.wait_for(self.fetch_player_status(sid, retry=1), timeout=8)
                                    if single:
                                        result[sid] = single
                                except Exception as se:
                                    log.warning(f'[批量查询] 单查降级也失败 (SteamID={sid}): {se}')
        return result

    async def resolve_steam_input(self, raw):
        """将多种格式的 Steam 输入统一解析为 17 位 SteamID64。"""
        if not raw or not isinstance(raw, str):
            return None
        s = raw.strip()
        if s.isdigit() and len(s) == 17:
            return s
        lowered = s.lower()
        if 'steamcommunity.com' in lowered or 's.team/p/' in lowered:
            path = s.split('?')[0].split('#')[0].rstrip('/')
            segments = path.split('/')
            if len(segments) >= 2:
                last = segments[-1]
                last2 = segments[-2] if len(segments) >= 2 else ''
                if last2 == 'profiles' and last.isdigit() and len(last) == 17:
                    return last
                if last2 == 'id' and last:
                    return await self._resolve_vanity_url(last)
                if 's.team' in lowered and last.isdigit() and len(last) == 17:
                    return last
        if s.isdigit() and len(s) <= 10:
            try:
                steamid64 = str(int(s) + 76561197960265728)
                if len(steamid64) == 17:
                    return steamid64
            except Exception:
                pass
        return None

    async def _resolve_vanity_url(self, vanity):
        if not self.API_KEY or not vanity:
            return None
        url = (
            f"{self.STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v1/"
            f"?key={self.API_KEY}&vanityurl={vanity}"
        )
        try:
            resp = await http_get(url, proxy=self.proxy, timeout=15)
            if resp is None or resp.status != 200:
                return None
            data = await resp.json()
            resp_data = data.get('response') or {}
            if resp_data.get('success') == 1 and resp_data.get('steamid'):
                return resp_data['steamid']
            log.warning(f"[vanity解析] 失败 success={resp_data.get('success')} (vanity={vanity})")
            return None
        except Exception as e:
            log.warning(f"[vanity解析] 异常: {e} (vanity={vanity})")
            return None

    async def get_chinese_game_name(self, gameid, fallback_name=None):
        """优先获取 Steam 商店中文游戏名，否则英文名，最后 fallback。"""
        if not gameid:
            return fallback_name or "未知游戏"
        gid = str(gameid)
        if gid in self._game_name_cache:
            cached = self._game_name_cache[gid]
            if isinstance(cached, tuple):
                return cached[0] if cached[0] else (cached[1] if len(cached) > 1 else "未知游戏")
            return cached
        url_zh = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=schinese"
        url_en = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=en"
        try:
            resp_zh = await http_get(url_zh, proxy=self.proxy, timeout=10)
            if resp_zh and resp_zh.status == 200:
                data_zh = await resp_zh.json()
                name_zh = data_zh.get(gid, {}).get("data", {}).get("name")
                if name_zh:
                    self._game_name_cache[gid] = name_zh
                    return name_zh
            resp_en = await http_get(url_en, proxy=self.proxy, timeout=10)
            if resp_en and resp_en.status == 200:
                data_en = await resp_en.json()
                name_en = data_en.get(gid, {}).get("data", {}).get("name")
                if name_en:
                    self._game_name_cache[gid] = name_en
                    return name_en
        except Exception as e:
            log.warning(f"获取游戏名失败: {e} (gameid={gid})")
        return fallback_name or "未知游戏"

    async def get_game_names(self, gameid, fallback_name=None):
        """返回 (中文名, 英文名)。"""
        if not gameid:
            return (fallback_name or "未知游戏", fallback_name or "未知游戏")
        gid = str(gameid)
        if gid in self._game_name_cache:
            cached = self._game_name_cache[gid]
            if isinstance(cached, tuple):
                return cached
            return (cached, cached)
        url_zh = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=schinese"
        url_en = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=en"
        name_zh = name_en = fallback_name or "未知游戏"
        try:
            resp_zh = await http_get(url_zh, proxy=self.proxy, timeout=10)
            if resp_zh and resp_zh.status == 200:
                data_zh = await resp_zh.json()
                name_zh = data_zh.get(gid, {}).get("data", {}).get("name") or name_zh
            resp_en = await http_get(url_en, proxy=self.proxy, timeout=10)
            if resp_en and resp_en.status == 200:
                data_en = await resp_en.json()
                name_en = data_en.get(gid, {}).get("data", {}).get("name") or name_en
        except Exception as e:
            log.warning(f"获取游戏名失败: {e} (gameid={gid})")
        self._game_name_cache[gid] = (name_zh, name_en)
        return (name_zh, name_en)

    async def get_game_cover_url(self, gameid, force_update=False):
        """获取游戏封面图本地路径（Steam 商店 header_image 转小图缓存），失败返回 None。"""
        if not gameid:
            return None
        gid = str(gameid)
        cover_dir = os.path.join(self.data_dir, "covers")
        os.makedirs(cover_dir, exist_ok=True)
        cover_path = os.path.join(cover_dir, f"{gid}.jpg")
        refresh_interval = 30 * 24 * 3600
        need_refresh = force_update
        if os.path.exists(cover_path) and not force_update:
            if time.time() - os.path.getmtime(cover_path) > refresh_interval:
                need_refresh = True
            else:
                return cover_path
        if not need_refresh and hasattr(self, "_game_cover_cache") and gid in self._game_cover_cache:
            return self._game_cover_cache[gid]
        lang_list = ["schinese", "japanese", "en"]
        try:
            for lang in lang_list:
                url = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l={lang}"
                resp = await http_get(url, proxy=self.proxy, timeout=10)
                if resp is None or resp.status != 200:
                    log.warning(f"获取游戏封面API失败 (gameid={gid}, lang={lang})")
                    continue
                data = await resp.json()
                header_img = data.get(gid, {}).get("data", {}).get("header_image")
                if not header_img:
                    continue
                small_img = header_img.replace("_header.jpg", "_capsule_184x69.jpg")
                img_resp = await http_get(small_img, proxy=self.proxy, timeout=10)
                if img_resp and img_resp.status == 200:
                    with open(cover_path, "wb") as f:
                        f.write(await img_resp.read())
                    return cover_path
        except Exception as e:
            log.warning(f"获取/缓存游戏封面异常: {e} (gameid={gid})")
        if os.path.exists(cover_path):
            return cover_path
        return None

    async def get_game_online_count(self, gameid):
        if not gameid:
            return None
        url = f"{self.STEAM_API_BASE}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={gameid}"
        try:
            resp = await http_get(url, proxy=self.proxy, timeout=10)
            if resp and resp.status == 200:
                data = await resp.json()
                return data.get('response', {}).get('player_count')
        except Exception as e:
            log.warning(f"获取在线人数失败: {e} (gameid={gameid})")
        return None

    # ========== 通知发送 ==========

    def _get_notify_group_ids(self, group_id, sid):
        """获取需要通知的所有群ID（主群 + 联动群），去重返回。"""
        groups = [group_id]
        for push_gid in self.push_groups.get(sid, []):
            if push_gid not in groups:
                groups.append(push_gid)
        return groups

    async def _render_notification_image(self, noti):
        """为单条通知渲染图片（开始游戏/结束游戏），返回 PNG bytes 或 None。"""
        try:
            font_path = self.get_font_path('NotoSansHans-Regular.otf')
            zh_game_name, en_game_name = await self.get_game_names(noti["gameid"], noti["game"])
            if noti["type"] == "start":
                status = noti.get("status", {})
                avatar_url = status.get("avatarfull") or status.get("avatar")
                superpower = self.get_today_superpower(noti["sid"])
                online_count = await self.get_game_online_count(noti["gameid"])
                img_bytes = await render_game_start(
                    self.data_dir, noti["sid"], noti["name"], avatar_url,
                    noti["gameid"], zh_game_name,
                    api_key=self.API_KEY, superpower=superpower,
                    sgdb_api_key=self.SGDB_API_KEY, font_path=font_path,
                    sgdb_game_name=en_game_name, online_count=online_count,
                    appid=noti.get("gameid"), proxy=self.proxy,
                    version=self._plugin_version)
            else:
                end_time_str = datetime.fromtimestamp(noti["quit_time"]).strftime("%Y-%m-%d %H:%M")
                duration_h = noti["duration_min"] / 60 if noti["duration_min"] > 0 else 0
                avatar_url = noti.get("avatar_url")
                tip_text = noti.get("tip_text") or "你已经和椅子合为一体，成为传说中的'椅子精'了喵！"
                img_bytes = await render_game_end(
                    self.data_dir, noti["sid"], noti["name"], avatar_url,
                    noti["gameid"], zh_game_name,
                    end_time_str, tip_text, duration_h,
                    sgdb_api_key=self.SGDB_API_KEY, font_path=font_path,
                    sgdb_game_name=en_game_name, appid=noti.get("gameid"),
                    proxy=self.proxy)
            if not img_bytes:
                return None
            return img_bytes
        except Exception as e:
            log.error(f"渲染通知图片失败 ({noti.get('type')}, {noti.get('name')}): {e}")
            return None

    async def _send_merged_notification(self, group_id, notifications):
        """将一批通知合并为一条 Chain 并推送到所有对应群。"""
        if not notifications:
            return
        send_text = bool(_cfg('notify_send_text', True))
        send_image = bool(_cfg('notify_send_image', True))

        chain = Chain()
        texts = []
        images = []
        for noti in notifications:
            if noti["type"] == "start":
                line = f"🟢【{noti['name']}】开始游玩 {noti['game']}\n"
            else:
                line = f"👋 {noti['name']} 不玩 {noti['game']}，游玩时间 {noti['duration_str']}\n"
            if send_text:
                texts.append(line)
            if send_image:
                img_bytes = await self._render_notification_image(noti)
                if img_bytes:
                    images.append(img_bytes)
        if not texts and not images:
            return
        if texts:
            chain.text("".join(texts))
        if images:
            chain.image(images)

        all_groups = []
        for n in notifications:
            for g in self._get_notify_group_ids(group_id, n["sid"]):
                if g not in all_groups:
                    all_groups.append(g)
        for g in all_groups:
            await _push_chain(g, chain)

    async def _flush_pending_end_notifications(self):
        if not self._pending_end_notifications:
            return
        for group_id, notifications in list(self._pending_end_notifications.items()):
            await self._send_merged_notification(group_id, notifications)
        self._pending_end_notifications.clear()

    async def notify_new_achievements(self, group_id, steamid, player_name, gameid, game_name, new_achievements):
        if not self.group_achievement_enabled.get(group_id, True):
            return
        if not new_achievements:
            return
        if not bool(_cfg('notify_send_image', True)):
            return  # 成就通知只以图片发送
        achievements_to_notify = list(new_achievements)[:self.max_achievement_notifications]
        details = self.achievement_monitor.details_cache.get((group_id, gameid))
        if not details:
            try:
                details = await self.achievement_monitor.get_achievement_details(
                    group_id, gameid, lang="schinese", api_key=self.API_KEY, steamid=steamid)
            except Exception as e:
                details = None
                log.warning(f"获取成就详情失败: {e}")
        if details and game_name:
            for d in details.values():
                d["game_name"] = game_name
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        tmp_path = None
        if details:
            unlocked_set = await self.achievement_monitor.get_player_achievements(
                self.API_KEY, group_id, steamid, gameid)
            if not unlocked_set:
                key = (group_id, steamid, gameid)
                unlocked_set = set(self.achievement_snapshots.get(key, []))
            if unlocked_set is None:
                unlocked_set = set()
            try:
                img_bytes = await self.achievement_monitor.render_achievement_image(
                    details, set(achievements_to_notify), player_name=player_name,
                    steamid=steamid, appid=gameid, unlocked_set=unlocked_set, font_path=font_path)
                if img_bytes:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                        tmp.write(img_bytes)
                        tmp_path = tmp.name
            except Exception as e:
                log.error(f"成就图片渲染失败: {e}\n{traceback.format_exc()}")
        if not tmp_path:
            return
        try:
            for g in self._get_notify_group_ids(group_id, steamid):
                await _push_chain(g, Chain().image(tmp_path))
        except Exception as e:
            log.error(f"发送成就通知失败: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ========== 成就轮询 ==========

    async def achievement_periodic_check(self, group_id, sid, gameid, player_name, game_name):
        """每20分钟对比一次成就列表，直到游戏结束，失败多次自动加入黑名单。"""
        key = (group_id, sid, gameid)
        try:
            while True:
                await asyncio.sleep(1200)
                if str(gameid) in self.achievement_monitor.achievement_blacklist:
                    log.info(f"[成就定时对比] 游戏 {gameid} 已在黑名单，跳过轮询")
                    break
                achievements_a = self.achievement_snapshots.get(key)
                achievements_b = await self.achievement_monitor.get_player_achievements(
                    self.API_KEY, group_id, sid, gameid)
                today = time.strftime('%Y-%m-%d')
                fail_key = (gameid, today)
                if achievements_b is None:
                    cnt = self.achievement_fail_count.get(fail_key, 0) + 1
                    self.achievement_fail_count[fail_key] = cnt
                    if cnt >= 10:
                        self.achievement_monitor.achievement_blacklist.add(str(gameid))
                        self.achievement_monitor._save_blacklist()
                        log.info(f"[成就黑名单] 游戏 {gameid} 当天累计获取失败10次，已加入黑名单")
                        break
                    continue
                if achievements_a is not None:
                    new_achievements = set(achievements_b) - set(achievements_a)
                    if new_achievements:
                        log.info(f"[成就定时对比] {player_name} 在 {game_name} 解锁新成就：{', '.join(new_achievements)}")
                        await self.notify_new_achievements(group_id, sid, player_name, gameid, game_name, new_achievements)
                        self.achievement_snapshots[key] = list(achievements_b)
                    else:
                        log.info(f"[成就定时对比] {player_name} 在 {game_name} 未发现新成就")
        except asyncio.CancelledError:
            log.info(f"[成就定时对比] 任务已取消 group_id={group_id} sid={sid} gameid={gameid}")
        except Exception as e:
            log.error(f"[成就定时对比] group_id={group_id} sid={sid} gameid={gameid} 异常: {e}")

    async def achievement_delayed_final_check(self, group_id, sid, gameid, player_name, game_name):
        """游戏结束后延迟5分钟再做一次成就对比。"""
        key = (group_id, sid, gameid)
        await asyncio.sleep(300)
        if str(gameid) in self.achievement_monitor.achievement_blacklist:
            return
        achievements_a = self.achievement_snapshots.get(key)
        achievements_b = await self.achievement_monitor.get_player_achievements(
            self.API_KEY, group_id, sid, gameid)
        today = time.strftime('%Y-%m-%d')
        fail_key = (gameid, today)
        if achievements_b is None:
            cnt = self.achievement_fail_count.get(fail_key, 0) + 1
            self.achievement_fail_count[fail_key] = cnt
            if cnt >= 10:
                self.achievement_monitor.achievement_blacklist.add(str(gameid))
                self.achievement_monitor._save_blacklist()
                return
        if achievements_a is not None and achievements_b is not None:
            new_achievements = set(achievements_b) - set(achievements_a)
            if new_achievements:
                log.info(f"[成就结束冗余对比] {player_name} 在 {game_name} 解锁新成就：{', '.join(new_achievements)}")
                await self.notify_new_achievements(group_id, sid, player_name, gameid, game_name, new_achievements)
        self.achievement_snapshots.pop(key, None)
        self.achievement_poll_tasks.pop(key, None)
        self.achievement_monitor.clear_game_achievements(group_id, sid, gameid)

    # ========== 轮询主逻辑 ==========

    async def _init_poll_once(self):
        """首次轮询：全员初始化，设置 next_poll_time 并输出一次初始日志。"""
        all_logs = []
        seen_sids = set()
        for group_id in self.group_steam_ids:
            group_lines = []
            for sid in self.group_steam_ids[group_id]:
                if sid in seen_sids:
                    continue
                seen_sids.add(sid)
                msg = await self.check_status_change(group_id, single_sid=sid, skip_push=True)
                if msg:
                    group_lines.append(msg)
            if group_lines:
                all_logs.append(f"群{group_id}：\n" + "\n".join(group_lines))
        if all_logs:
            log.info("====== Steam状态监控初始化日志 ======\n" + "\n".join(all_logs) + "\n=====================================================")

    # ========== 主轮询（由定时 tick 驱动） ==========

    async def poll_tick(self):
        """每个定时 tick 调用一次：对齐分钟边界批量轮询，40 秒后统一输出日志与合并通知。

        相比原版内部无限循环的做法，这里改为状态机，由框架的定时任务驱动，
        卸载/重载插件时框架会自动移除该定时任务，避免后台任务泄漏。
        """
        now = time.time()
        # 1) 每日排行榜自动推送 + 节流保存（与轮询阶段无关，每次 tick 检查）
        try:
            now_dt = datetime.now()
            push_hour = int(getattr(self, 'rank_push_hour', 8))
            push_minute = int(getattr(self, 'rank_push_minute', 30))
            if now_dt.hour == push_hour and now_dt.minute == push_minute:
                push_date_key = self._get_day_key(-1)
                if self._last_rank_push_date != push_date_key and (self.rank_push_groups or self.rank_push_all):
                    self._last_rank_push_date = push_date_key
                    log.info(
                        f"[排行榜] 开始每日自动推送，时间={push_hour}:{push_minute:02d}，"
                        f"目标群: {self.rank_push_groups if self.rank_push_groups else '全部群(rank_push_all)'}"
                    )
                    asyncio.create_task(self._daily_rank_push())
            if self._data_dirty and (now - self._last_save_time) >= self._save_interval:
                self._save_persistent_data(force=True)
        except Exception as e:
            log.error(f"[Steam状态监控] 定时tick基础任务异常: {e}")

        # 2) 轮询状态机：每分钟0秒批量查询，40秒后输出日志
        if not hasattr(self, '_poll_phase'):
            self._poll_phase = "idle"
            self._poll_next_minute = ((int(now) // 60) + 1) * 60
            self._poll_log_time = 0
        if not hasattr(self, '_poll_lock'):
            self._poll_lock = asyncio.Lock()
        if self._poll_lock.locked():
            # 上一轮 poll 仍在执行（如网络重试耗时较长），直接返回，
            # 避免 APScheduler 因 max_instances=1 反复输出 skipped 日志
            return

        if self._poll_phase == "waiting_log":
            if now >= self._poll_log_time:
                self._poll_phase = "idle"
                self._poll_next_minute = self._poll_next_minute + 60 if self._poll_next_minute else ((int(now) // 60) + 1) * 60
                if self._last_round_logs:
                    if self.detailed_poll_log:
                        all_logs = [f"群{gid}：\n{s}" for gid, s in self._last_round_logs]
                        log.info("====== Steam状态监控轮询日志 ======\n" + "\n".join(all_logs) + "\n=====================================================")
                    else:
                        log.info("周期轮询成功")
                self._last_round_logs.clear()
            return

        if now < self._poll_next_minute:
            return

        # 到达分钟边界：跨群收集所有到点的 SteamID，合并为一次批量查询（N群=1次API调用+自动去重）
        group_sids = {}  # {group_id: [sid, ...]}
        all_sids_set = set()
        for group_id in self.group_steam_ids:
            if not self.group_monitor_enabled.get(group_id, True):
                continue
            steam_ids = self.group_steam_ids.get(group_id, [])
            next_poll = self.next_poll_time.setdefault(group_id, {})
            sids_to_query = [sid for sid in steam_ids if now >= next_poll.get(sid, 0)]
            if not sids_to_query:
                continue
            group_sids[group_id] = sids_to_query
            all_sids_set.update(sids_to_query)

        if not group_sids:
            # 本轮无到点，对齐到下一分钟
            self._poll_next_minute = self._poll_next_minute + 60
            return

        async with self._poll_lock:
            global_status_map = await self.fetch_player_statuses_batch(list(all_sids_set))

            # 各群并行处理状态变更检测
            async def query_one_group(gid, sids):
                round_msg_lines = []
                tasks = []
                for sid in sids:
                    status = global_status_map.get(sid)
                    if status is None:
                        # 该玩家本批查询失败（网络/API 问题），本轮跳过状态检测，
                        # 不再重复逐条单查（会再次重试多次并大幅阻塞轮询），下个周期自动重试
                        continue
                    tasks.append(self.check_status_change(gid, single_sid=sid, status_override=status))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for msg in results:
                    if isinstance(msg, Exception):
                        log.error(f"[轮询] check_status_change 异常: {msg} (gid={gid})")
                        continue
                    if msg:
                        round_msg_lines.append(msg)
                if round_msg_lines:
                    self._last_round_logs.append((gid, "\n".join(round_msg_lines)))

            await asyncio.gather(
                *[query_one_group(gid, sids) for gid, sids in group_sids.items()],
                return_exceptions=True,
            )
            # 统一 flush 本轮收集的所有通知（开始游戏 + 延迟退出的结束游戏），合并发送
            await self._flush_pending_end_notifications()
            # 40 秒后统一输出日志
            self._poll_log_time = time.time() + 40
            self._poll_phase = "waiting_log"

    # ========== 状态检测核心 ==========

    async def check_status_change(self, group_id, single_sid=None, status_override=None, poll_level=None, skip_push=False):
        """轮询检测玩家状态变更并推送通知（分群，支持单个 sid）。

        返回精简日志字符串，不直接打印日志。
        """
        now = int(time.time())
        steam_ids = [single_sid] if single_sid else self.group_steam_ids.get(group_id, [])
        last_states = self.group_last_states.setdefault(group_id, {})
        start_play_times = self.group_start_play_times.setdefault(group_id, {})
        last_quit_times = self.group_last_quit_times.setdefault(group_id, {})
        pending_logs = self.group_pending_logs.setdefault(group_id, {})
        pending_quit = self.group_pending_quit.setdefault(group_id, {})
        recent_games = self.group_recent_games.setdefault(group_id, [])
        msg_lines = []
        notifications = []  # 本轮收集的状态变更通知，统一合并发送
        for sid in steam_ids:
            status = status_override if (status_override and sid == single_sid) else await self.fetch_player_status(sid)
            if not status:
                continue
            prev = last_states.get(sid)
            name = self._resolve_bind_name(sid, status.get('name') or sid)
            gameid = status.get('gameid')
            game = status.get('gameextrainfo')
            lastlogoff = status.get('lastlogoff')
            personastate = status.get('personastate', 0)
            zh_game_name = await self.get_chinese_game_name(gameid, game) if gameid else (game or "未知游戏")
            prev_gameid = prev.get('gameid') if prev else None
            current_gameid = gameid
            # --- 退出游戏（缓冲3分钟）---（含游戏切换：直接切到另一款游戏也会结算上一款时长）---
            if prev_gameid and (current_gameid in [None, "", "0"] or current_gameid != prev_gameid):
                log.info(f"[退出逻辑] {name} prev_gameid={prev_gameid} current_gameid={current_gameid}")
                zh_prev_game_name = await self.get_chinese_game_name(prev_gameid, prev.get('gameextrainfo') if prev else None) if prev_gameid else (prev.get('gameextrainfo') if prev else "未知游戏")
                duration_min = 0
                # 安全获取 sid_data，兼容旧格式 int → dict
                sid_data = start_play_times.get(sid)
                if not isinstance(sid_data, dict):
                    sid_data = {}
                    start_play_times[sid] = sid_data
                start_time = sid_data.get(prev_gameid, now)
                if prev_gameid in sid_data:
                    duration_min = (now - sid_data[prev_gameid]) / 60
                    if duration_min == 0:
                        for _ in range(2):
                            start_time = sid_data.get(prev_gameid, now)
                            duration_min = (now - start_time) / 60
                            if duration_min > 0:
                                break
                            await asyncio.sleep(1)
                self.achievement_monitor.clear_game_achievements(group_id, sid, prev_gameid)
                if not self._should_skip_game(prev_gameid):
                    pending_quit.setdefault(sid, {})[prev_gameid] = {
                        "quit_time": now,
                        "name": name,
                        "game_name": zh_prev_game_name,
                        "duration_min": duration_min,
                        "start_time": start_time,
                        "notified": False,
                    }
                    # 成就结算：游戏结束时，延迟15分钟再做一次对比
                    try:
                        player_name = name
                        game_name = zh_prev_game_name
                        key = (group_id, sid, prev_gameid)
                        poll_task = self.achievement_poll_tasks.pop(key, None)
                        if poll_task:
                            poll_task.cancel()
                        if not skip_push:
                            asyncio.create_task(self.achievement_delayed_final_check(group_id, sid, prev_gameid, player_name, game_name))
                    except Exception as e:
                        log.error(f"结算成就时异常: {e}")
                    # 启动延迟任务
                    if not hasattr(self, '_pending_quit_tasks'):
                        self._pending_quit_tasks = {}
                    if sid not in self._pending_quit_tasks:
                        self._pending_quit_tasks[sid] = {}
                    old_task = self._pending_quit_tasks[sid].get(prev_gameid)
                    if old_task:
                        old_task.cancel()
                    if not skip_push:
                        task = asyncio.create_task(self._delayed_quit_check(group_id, sid, prev_gameid))
                        self._pending_quit_tasks[sid][prev_gameid] = task
                else:
                    log.info(f"[游戏过滤] {name} 退出游戏 {zh_prev_game_name}({prev_gameid}) 被跳过（黑白名单过滤）")
                last_quit_times.setdefault(sid, {})[prev_gameid] = now
                last_states[sid] = status
                if current_gameid in [None, "", "0"]:
                    continue  # 纯退出：防止重复推送
                # 游戏切换：不 continue，继续执行下方开始游戏逻辑

            # --- 开始游戏/继续游戏（仅当 gameid 变更时推送）---
            if current_gameid not in [None, "", "0"] and current_gameid != prev_gameid:
                quit_info = pending_quit.setdefault(sid, {}).get(current_gameid)
                # 检查是否为网络波动（3分钟内重启同一游戏）
                if quit_info and now - quit_info["quit_time"] <= 180 and not quit_info.get("notified"):
                    # 取消延迟任务
                    if hasattr(self, '_pending_quit_tasks') and self._pending_quit_tasks.get(sid, {}).get(current_gameid):
                        self._pending_quit_tasks[sid][current_gameid].cancel()
                        self._pending_quit_tasks[sid].pop(current_gameid, None)
                    quit_info["notified"] = True
                    msg = f"⚠️ {name} 游玩 {zh_game_name} 时网络波动了"
                    # 网络波动通知开关检查
                    if not _cfg('enable_network_fluctuation_notify', True):
                        last_states[sid] = status
                        continue
                    if skip_push:
                        last_states[sid] = status
                        continue
                    # 推送到主群和所有联动群
                    for g in self._get_notify_group_ids(group_id, sid):
                        await _push_chain(g, Chain().text(msg))
                    last_states[sid] = status
                    continue  # 只推送网络波动提醒，跳过后续逻辑
                # 开始游戏推送逻辑
                if self._should_skip_game(current_gameid):
                    log.info(f"[游戏过滤] {name} 开始游戏 {zh_game_name}({current_gameid}) 被跳过（黑白名单过滤）")
                    start_play_times.setdefault(sid, {})[current_gameid] = now
                    last_states[sid] = status
                    continue
                start_play_times.setdefault(sid, {})[current_gameid] = now
                # 收集通知，由末尾统一合并发送（不在循环内逐条推送）
                if not skip_push and _cfg('enable_game_start_notify', True):
                    notifications.append({
                        "type": "start",
                        "name": name,
                        "game": zh_game_name,
                        "sid": sid,
                        "gameid": current_gameid,
                        "status": status,
                    })
                # 成就监控任务启动（受 enable_achievement_poll 配置控制）
                if skip_push or not _cfg('enable_achievement_poll', True):
                    last_states[sid] = status
                    continue
                try:
                    player_name = name
                    game_name = zh_game_name
                    key = (group_id, sid, current_gameid)
                    achievements = await self.achievement_monitor.get_player_achievements(self.API_KEY, group_id, sid, current_gameid)
                    self.achievement_snapshots[key] = list(achievements) if achievements else []
                    # 新增日志：已成功获取成就列表
                    unlocked_count = len(achievements) if achievements else 0
                    # 获取总成就数量
                    details = await self.achievement_monitor.get_achievement_details(group_id, current_gameid, lang="schinese", api_key=self.API_KEY, steamid=sid)
                    total_count = len(details) if details else 0
                    log.info(f"[成就初始化] {name} 已成功获取成就列表 {unlocked_count}/{total_count} 游戏名：{zh_game_name}")
                    poll_task = asyncio.create_task(self.achievement_periodic_check(group_id, sid, current_gameid, player_name, game_name))
                    self.achievement_poll_tasks[key] = poll_task
                except Exception as e:
                    log.error(f"启动成就监控任务异常: {e}")
                last_states[sid] = status
                continue

            # 智能轮询间隔设置（支持固定间隔）
            next_poll = self.next_poll_time.setdefault(group_id, {})
            import math
            # intervals 提前定义，固定间隔模式下对齐逻辑也需要使用
            intervals = self.smart_poll_intervals if isinstance(self.smart_poll_intervals, list) and len(self.smart_poll_intervals) == 6 else [1, 3, 5, 10, 20, 30]
            if self.fixed_poll_interval and self.fixed_poll_interval > 0:
                poll_interval = self.fixed_poll_interval
                poll_level_str = f"固定{self.fixed_poll_interval//60 if self.fixed_poll_interval>=60 else self.fixed_poll_interval}{'分钟' if self.fixed_poll_interval>=60 else '秒'}轮询"
            else:
                # 优先级：游戏中 > 在线 > 离线 > 默认
                if gameid:
                    poll_interval = intervals[0] * 60
                    poll_level_str = f"{intervals[0]}分钟轮询"
                elif personastate and int(personastate) > 0:
                    poll_interval = intervals[1] * 60
                    poll_level_str = f"{intervals[1]}分钟轮询"
                elif lastlogoff:
                    minutes_ago = (now - int(lastlogoff)) / 60
                    if minutes_ago <= 12:
                        poll_interval = intervals[1] * 60
                        poll_level_str = f"{intervals[1]}分钟轮询"
                    elif minutes_ago <= 180:
                        poll_interval = intervals[2] * 60
                        poll_level_str = f"{intervals[2]}分钟轮询"
                    elif minutes_ago <= 1440:
                        poll_interval = intervals[3] * 60
                        poll_level_str = f"{intervals[3]}分钟轮询"
                    elif minutes_ago <= 2880:
                        poll_interval = intervals[4] * 60
                        poll_level_str = f"{intervals[4]}分钟轮询"
                    else:
                        poll_interval = intervals[5] * 60
                        poll_level_str = f"{intervals[5]}分钟轮询"
                else:
                    poll_interval = intervals[5] * 60
                    poll_level_str = f"{intervals[5]}分钟轮询"
            interval_min = poll_interval // 60
            next_time = ((now // 60) + math.ceil(interval_min)) * 60
            if interval_min in [intervals[1], intervals[2], intervals[3], intervals[4], intervals[5]]:
                next_time = ((now // 60) // interval_min + 1) * interval_min * 60
            next_poll[sid] = next_time
            # 轮询间隔描述
            if gameid:
                msg_lines.append(f"🟢【{name}】正在玩 {zh_game_name}（{poll_level_str}）")
            elif personastate and int(personastate) > 0:
                _persona_text = {1: '在线', 2: '忙碌', 3: '离开', 4: '打盹'}
                ptext = _persona_text.get(int(personastate), '在线')
                picon = {1: '🟡', 2: '🔴', 3: '🟣', 4: '🟣'}.get(int(personastate), '🟡')
                msg_lines.append(f"{picon}【{name}】{ptext}（{poll_level_str}）")
            elif lastlogoff:
                hours_ago = (now - int(lastlogoff)) / 3600
                msg_lines.append(f"⚪️【{name}】离线 上次在线 {hours_ago:.1f} 小时前（{poll_level_str}）")
            else:
                msg_lines.append(f"⚪️【{name}】离线（{poll_level_str}）")
            last_states[sid] = status

        for sid in pending_quit:
            for gameid in list(pending_quit[sid].keys()):
                info = pending_quit[sid][gameid]
                if now - info["quit_time"] >= 180 and not info.get("notified"):
                    info["notified"] = True
                    # 游戏结束通知开关：关闭则跳过推送，但仍清理 pending_quit
                    if not _cfg('enable_game_end_notify', True):
                        if gameid in pending_quit[sid]:
                            del pending_quit[sid][gameid]
                        continue
                    duration_min = info.get("duration_min", 0)
                    # 优化时间显示
                    if duration_min < 60:
                        time_str = f"{duration_min:.1f}分钟"
                    else:
                        time_str = f"{duration_min/60:.1f}小时"
                    # 收集到通知缓冲，由主轮询统一合并发送（兜底逻辑，正常由 _delayed_quit_check 处理）
                    avatar_url = None
                    ls = last_states.get(sid)
                    if ls:
                        avatar_url = ls.get("avatarfull") or ls.get("avatar")
                    notifications.append({
                        "type": "end",
                        "name": info["name"],
                        "game": info["game_name"],
                        "duration_str": time_str,
                        "sid": sid,
                        "gameid": gameid,
                        "quit_time": info["quit_time"],
                        "duration_min": duration_min,
                        "avatar_url": avatar_url,
                        "tip_text": "你已经和椅子合为一体，成为传说中的'椅子精'了喵！",
                    })
                    if gameid in pending_quit[sid]:
                        del pending_quit[sid][gameid]

        self._save_persistent_data()
        # 将本轮收集的开始/结束游戏通知提交到缓冲区，由主轮询统一 flush 合并发送
        if notifications and not skip_push:
            self._pending_end_notifications.setdefault(group_id, []).extend(notifications)
        # 只返回日志字符串
        return "\n".join(msg_lines) if msg_lines else None

    async def _delayed_quit_check(self, group_id, sid, gameid):
        """游戏结束后延迟3分钟结算：记录时长、清理成就任务并推送结束通知。"""
        await asyncio.sleep(180)
        info = self.group_pending_quit.get(group_id, {}).get(sid, {}).get(gameid)
        if info and not info.get("notified"):
            duration_min = info["duration_min"]
            if duration_min == 0:
                for _ in range(2):
                    last_quit_time = info["quit_time"]
                    start_time = info["start_time"]
                    if start_time and last_quit_time:
                        duration_min = (last_quit_time - start_time) / 60
                        if duration_min > 0:
                            info["duration_min"] = duration_min
                            break
                    await asyncio.sleep(1)
            info["notified"] = True
            # 排行榜数据采集：记录本次游玩时长（在推送/return之前执行，确保即使关闭通知也能记录）
            self._record_playtime(sid, gameid, info.get("game_name", "未知游戏"), info.get("duration_min", 0))
            # Session 级别游玩记录采集（甘特图/热力图数据源）
            self._record_session(
                sid=sid, gameid=gameid, game_name=info.get("game_name", "未知游戏"),
                start_time=info.get("start_time"), end_time=info.get("quit_time"),
                duration_min=info.get("duration_min", 0), group_id=group_id,
            )
            # 游戏结束通知开关：关闭则跳过推送，但仍清理成就任务和 pending_quit
            if not _cfg('enable_game_end_notify', True):
                key = (group_id, sid, gameid)
                poll_task = self.achievement_poll_tasks.pop(key, None)
                if poll_task:
                    poll_task.cancel()
                self.achievement_snapshots.pop(key, None)
                self.achievement_monitor.clear_game_achievements(group_id, sid, gameid)
                self.group_pending_quit.get(group_id, {}).get(sid, {}).pop(gameid, None)
                return
            duration_min = info["duration_min"]
            if duration_min < 60:
                time_str = f"{duration_min:.1f}分钟"
            else:
                time_str = f"{duration_min/60:.1f}小时"
            # 获取提示词（供后续图片渲染时使用）
            if duration_min < 5:
                tip_text = "风扇都没转热，主人就结束了？"
            elif duration_min < 10:
                tip_text = "杂鱼杂鱼~主人你就这水平？"
            elif duration_min < 30:
                tip_text = "热身一下就结束了？"
            elif duration_min < 60:
                tip_text = "歇会儿再来，别太累了喵！"
            elif duration_min < 120:
                tip_text = "沉浸在游戏世界，时间过得飞快喵！"
            elif duration_min < 300:
                tip_text = "肝到手软了喵！主人不如陪陪咱~"
            elif duration_min < 600:
                tip_text = "你吃饭了吗？还是说你已经忘了吃饭这件事？"
            elif duration_min < 1200:
                tip_text = "家里电费都要被你玩光了喵！"
            elif duration_min < 1800:
                tip_text = "咱都要给你颁发'不眠猫'勋章了！"
            elif duration_min < 2400:
                tip_text = "主人你还活着喵？你是不是忘了关电脑呀~"
            else:
                tip_text = "你已经和椅子合为一体，成为传说中的'椅子精'了喵！"
            avatar_url = None
            last_state = self.group_last_states.get(group_id, {}).get(sid)
            if last_state:
                avatar_url = last_state.get("avatarfull") or last_state.get("avatar")
            # 写入通知缓冲区，由主轮询统一 flush 合并发送
            self._pending_end_notifications.setdefault(group_id, []).append({
                "type": "end",
                "name": info["name"],
                "game": info["game_name"],
                "duration_str": time_str,
                "sid": sid,
                "gameid": gameid,
                "quit_time": info["quit_time"],
                "duration_min": duration_min,
                "avatar_url": avatar_url,
                "tip_text": tip_text,
            })
            # 三分钟后再关闭成就轮询和清理快照
            key = (group_id, sid, gameid)
            poll_task = self.achievement_poll_tasks.pop(key, None)
            if poll_task:
                poll_task.cancel()
            self.achievement_snapshots.pop(key, None)
            self.achievement_monitor.clear_game_achievements(group_id, sid, gameid)
            self.group_pending_quit.get(group_id, {}).get(sid, {}).pop(gameid, None)

    # ========== 排行榜 ==========

    def _record_playtime(self, sid, gameid, game_name, duration_min):
        """记录游玩时长到 play_records，带5分钟去重（防止多群重复记录）。"""
        try:
            if duration_min <= 0 or not gameid:
                return
            # 防御性清洗：确保 game_name 是字符串（可能被缓存污染为 tuple/list）
            if isinstance(game_name, (tuple, list)):
                game_name = game_name[0] if game_name else "未知游戏"
            game_name = str(game_name) if game_name else "未知游戏"
            cache_key = (str(sid), str(gameid))
            now = time.time()
            last_ts = self._recorded_quit_cache.get(cache_key, 0)
            if now - last_ts < 300:
                log.debug(f"[排行榜] 去重跳过: {sid} {game_name} (上次记录{int(now - last_ts)}秒前)")
                return
            self._recorded_quit_cache[cache_key] = now
            today_key = self._get_day_key(0)
            if today_key not in self.play_records:
                self.play_records[today_key] = {}
            if str(sid) not in self.play_records[today_key]:
                self.play_records[today_key][str(sid)] = {}
            gid = str(gameid)
            if gid not in self.play_records[today_key][str(sid)]:
                self.play_records[today_key][str(sid)][gid] = {"name": game_name, "minutes": 0}
            self.play_records[today_key][str(sid)][gid]["minutes"] += int(duration_min)
            self.play_records[today_key][str(sid)][gid]["name"] = game_name
            self._data_dirty = True
            log.info(f"[排行榜] 记录游玩时长: {sid} {game_name} +{int(duration_min)}分钟")
            # 清理过期的去重缓存（超过10分钟）
            expired = [k for k, v in self._recorded_quit_cache.items() if now - v > 600]
            for k in expired:
                self._recorded_quit_cache.pop(k, None)
        except Exception as e:
            log.error(f"[排行榜] 记录游玩时长异常: {e}")

    def _get_rank_data(self, days=1, group_id=None, base_day_offset=0):
        """聚合游玩时长数据，返回已排序的排行榜列表。

        Args:
            days: 1=今日, 7=最近7天, 30=最近30天
            group_id: 指定群则只统计该群的SteamID，None则统计全部

        Returns:
            [{sid, name, total_minutes, games: [{name, minutes}]}] 按总时长降序
        """
        try:
            today_str = self._get_day_key(base_day_offset)
            base_date = datetime.strptime(today_str, "%Y-%m-%d")
            date_keys = []
            for i in range(days):
                d = base_date - timedelta(days=i)
                date_keys.append(d.strftime("%Y-%m-%d"))
            # 确定要统计的 SteamID 集合
            if group_id:
                target_sids = set(self.group_steam_ids.get(group_id, []))
            else:
                target_sids = set()
                for gids in self.group_steam_ids.values():
                    target_sids.update(gids)
            if not target_sids:
                return []
            # 聚合
            merged = {}  # {sid: {gameid: {name, minutes}}}
            for date_key in date_keys:
                day_data = self.play_records.get(date_key, {})
                for sid, games in day_data.items():
                    if sid not in target_sids:
                        continue
                    if sid not in merged:
                        merged[sid] = {}
                    for gid, info in games.items():
                        # 防御性清洗：name 可能被缓存污染为 tuple/list
                        raw_name = info.get("name", "未知游戏")
                        if isinstance(raw_name, (tuple, list)):
                            raw_name = raw_name[0] if raw_name else "未知游戏"
                        raw_name = str(raw_name) if raw_name else "未知游戏"
                        if gid not in merged[sid]:
                            merged[sid][gid] = {"name": raw_name, "minutes": 0}
                        merged[sid][gid]["minutes"] += info.get("minutes", 0)
                        merged[sid][gid]["name"] = info.get("name", merged[sid][gid]["name"])
            # 构建排行榜列表
            rank_list = []
            for sid, games in merged.items():
                total = sum(g["minutes"] for g in games.values())
                if total <= 0:
                    continue
                game_list = sorted(
                    [{"name": g["name"], "minutes": g["minutes"]} for g in games.values()],
                    key=lambda x: x["minutes"],
                    reverse=True,
                )
                rank_list.append({
                    "sid": sid,
                    "name": game_list[0]["name"] if game_list else sid,  # 临时用游戏名占位，后续替换为玩家名
                    "total_minutes": total,
                    "games": game_list,
                })
            rank_list.sort(key=lambda x: x["total_minutes"], reverse=True)
            return rank_list
        except Exception as e:
            log.error(f"[排行榜] 聚合数据异常: {e}")
            return []

    async def _render_daily_rank_file(self, rank_data):
        """补齐排行榜展示信息并渲染为图片 bytes（昨日榜单专用）。"""
        sid_set = {player["sid"] for player in rank_data}
        sid_info = {}
        if sid_set:
            status_map = await self.fetch_player_statuses_batch(list(sid_set))
            for sid, info in status_map.items():
                sid_info[sid] = {
                    "name": info.get("name") or sid,
                    "avatar_url": info.get("avatarfull") or info.get("avatar"),
                }

        yesterday = self._get_day_key(-1)
        day_data = self.play_records.get(yesterday, {})
        for player in rank_data:
            sid = player["sid"]
            info = sid_info.get(sid, {})
            player["name"] = self._resolve_bind_name(sid, info.get("name", sid[-8:]))
            player["avatar_url"] = info.get("avatar_url")
            player["top_game_id"] = None
            if not player["games"]:
                continue
            top_name = player["games"][0]["name"]
            for game_id, game_info in day_data.get(sid, {}).items():
                if game_info.get("name") == top_name:
                    player["top_game_id"] = game_id
                    break

        async def cover_fetcher(gameid):
            return await self.get_game_cover_url(gameid)

        avatar_frame_paths = {}
        from .game_start_render import get_avatar_frame_path, get_avatar_frame_url

        for player in rank_data:
            sid = player.get("sid", "")
            if not sid:
                continue
            frame_path = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
            if not frame_path:
                frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
                if frame_url:
                    frame_path = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
            if frame_path:
                avatar_frame_paths[sid] = frame_path

        font_path = self.get_font_path("NotoSansHans-Regular.otf")
        img_bytes = await render_rank_image(
            self.data_dir,
            rank_data,
            "昨日",
            font_path=font_path,
            proxy=self.proxy,
            cover_fetcher=cover_fetcher,
            avatar_frame_paths=avatar_frame_paths,
        )
        return img_bytes

    async def _daily_rank_push(self, test_mode=False):
        """推送昨日榜单；默认按目标群独立聚合，显式全局模式才共享总榜。"""
        use_global_rank = getattr(self, "rank_push_all", False)
        scopes = build_rank_push_scopes(getattr(self, "rank_push_groups", []), use_global_rank=use_global_rank)
        if not scopes:
            log.warning("[排行榜] 没有目标群可推送（请先使用「steam rank_on」或「steam rank_on all」开启推送）")
            return

        rendered = {}
        try:
            for target_group_id, data_group_id in scopes:
                render_key = ("global", None) if data_group_id is None else ("group", data_group_id)
                if render_key not in rendered:
                    rank_data = self._get_rank_data(days=1, group_id=data_group_id, base_day_offset=-1)
                    if not rank_data:
                        rendered[render_key] = None
                    else:
                        try:
                            rendered[render_key] = await self._render_daily_rank_file(rank_data)
                        except Exception as e:
                            log.error(f"[排行榜] 渲染群 {data_group_id or '全局'} 昨日榜单失败: {e}")
                            rendered[render_key] = None

                img_bytes = rendered[render_key]
                if not img_bytes:
                    continue
                try:
                    chain = Chain().text("📊 昨日游戏时长排行榜来啦！\n").image(img_bytes)
                    await _push_chain(target_group_id, chain)
                    log.info(f"[排行榜] 已推送昨日排行榜到群 {target_group_id}")
                except Exception as e:
                    log.error(f"[排行榜] 推送群 {target_group_id} 失败: {e}")
        except Exception as e:
            log.error(f"[排行榜] 每日推送异常: {e}")

    async def _render_and_send_rank(self, data, group_id, days, period_label, is_all=False):
        """生成排行榜图片并发送。"""
        try:
            rank_data = self._get_rank_data(days=days, group_id=None if is_all else group_id)
            if not rank_data:
                await _send(data, f"暂无{period_label}游玩记录，玩家游戏结束后才会有数据。")
                return
            # 补充玩家昵称和头像URL
            sid_set = {p["sid"] for p in rank_data}
            sid_info = {}
            if sid_set:
                status_map = await self.fetch_player_statuses_batch(list(sid_set))
                for sid, info in status_map.items():
                    sid_info[sid] = {
                        "name": info.get("name") or sid,
                        "avatar_url": info.get("avatarfull") or info.get("avatar"),
                    }
            for p in rank_data:
                info = sid_info.get(p["sid"], {})
                p["name"] = self._resolve_bind_name(p["sid"], info.get("name", p["sid"][-8:]))
                p["avatar_url"] = info.get("avatar_url")
                p["top_game_id"] = None
            # 从 play_records 中反查每个玩家 top 游戏的 gameid（用于封面获取）
            for p in rank_data:
                if not p["games"]:
                    continue
                top_name = p["games"][0]["name"]
                for di in range(days):
                    dk = self._get_day_key(-di)
                    sid_games = self.play_records.get(dk, {}).get(p["sid"], {})
                    for gid, ginfo in sid_games.items():
                        if ginfo.get("name") == top_name:
                            p["top_game_id"] = gid
                            break
                    if p.get("top_game_id"):
                        break

            async def cover_fetcher(gameid):
                return await self.get_game_cover_url(gameid)

            avatar_frame_paths = {}
            from .game_start_render import get_avatar_frame_path, get_avatar_frame_url
            for p in rank_data:
                sid = p.get("sid", "")
                if sid:
                    fp = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
                    if not fp:
                        frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
                        if frame_url:
                            fp = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
                    if fp:
                        avatar_frame_paths[sid] = fp

            font_path = self.get_font_path('NotoSansHans-Regular.otf')
            img_bytes = await render_rank_image(
                self.data_dir, rank_data, period_label,
                font_path=font_path, proxy=self.proxy,
                cover_fetcher=cover_fetcher,
                avatar_frame_paths=avatar_frame_paths,
            )
            if img_bytes:
                await _send_image(data, img_bytes)
            else:
                await _send(data, "排行榜渲染失败，请稍后重试。")
        except Exception as e:
            log.error(f"[排行榜] 渲染失败: {e}\n{traceback.format_exc()}")
            await _send(data, f"排行榜生成失败: {e}")

    # ========== 指令实现（分群） ==========

    def _group_id_of(self, data: Message) -> str:
        """从消息中解析出群 ID（私聊时回退到用户 ID）。"""
        return str(data.channel_id or data.user_id)

    async def steam_on(self, data: Message, args: list):
        """手动启动本群 Steam 状态监控轮询（分群）。"""
        group_id = self._group_id_of(data)
        self.group_monitor_enabled[group_id] = True
        if not self.API_KEY:
            await _send(data, "未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        steam_ids = self.group_steam_ids.get(group_id, [])
        if not steam_ids or not any(isinstance(x, str) and x.strip() for x in steam_ids):
            await _send(data, "未设置监控的 SteamID 列表，请先使用「steam addid [SteamID]」添加要监控的玩家。")
            return
        if group_id in self.running_groups:
            await _send(data, "本群 Steam 监控已在运行。")
            return
        self.running_groups.add(group_id)
        self.notify_sessions[group_id] = group_id
        self.group_bot_ids[group_id] = data.instance.appid
        self._save_notify_session()
        now = int(time.time())
        self.group_last_states.setdefault(group_id, {})
        self.group_start_play_times.setdefault(group_id, {})
        status_map = await self.fetch_player_statuses_batch(steam_ids) if steam_ids else {}
        for sid in steam_ids:
            status = status_map.get(sid)
            if status:
                self.group_last_states[group_id][sid] = status
                gid = status.get('gameid')
                if gid:
                    start_map = self.group_start_play_times[group_id].setdefault(sid, {})
                    if gid not in start_map:
                        start_map[gid] = now
        await _send(data, "本群 Steam 状态监控启动完成喔！ヾ(≧ω≦)ゞ")

    async def steam_addid(self, data: Message, args: list):
        """添加 SteamID 到本群监控列表（分群），支持逗号分隔多个 ID，末尾可 @用户 绑定。"""
        group_id = self._group_id_of(data)
        if not args:
            await _send(data, "用法：steam addid [SteamID] [@用户] [备注]（支持 17 位ID / 链接 / 好友码，逗号分隔多个）")
            return
        raw_steamids = args[0]
        bind_qq = data.at_target[0] if data.at_target else ''
        bind_nickname = ' '.join(args[1:]) if len(args) > 1 else ''
        # 兼容写法：未 @ 用户时，若尾随参数中含 QQ 号（5-11 位数字），将其作为绑定目标，
        # 其余部分作为备注（例如：steam addid 好友码 1554808351）
        if not bind_qq and len(args) > 1:
            rest = args[1:]
            for i, token in enumerate(rest):
                token_clean = token.strip().lstrip('@')
                if token_clean.isdigit() and 5 <= len(token_clean) <= 11:
                    bind_qq = token_clean
                    bind_nickname = ' '.join(rest[i + 1:]).strip()
                    break
        raw_list = [x.strip() for x in re.split(r'[,，]+', raw_steamids) if x.strip()]
        resolved_list, invalid_list = [], []
        for raw in raw_list:
            sid = await self.resolve_steam_input(raw)
            if sid and sid.isdigit() and len(sid) == 17:
                resolved_list.append(sid)
            else:
                invalid_list.append(raw)
        if invalid_list:
            await _send(data, f"以下输入无法解析为有效 SteamID：{', '.join(invalid_list)}\n"
                              f"支持格式：17位 SteamID64 / 个人资料链接 / 自定义ID链接 / 8位好友码")
            return
        seen, steamid_list = set(), []
        for sid in resolved_list:
            if sid not in seen:
                seen.add(sid)
                steamid_list.append(sid)
        steam_ids = self.group_steam_ids.setdefault(group_id, [])
        added, already = [], []
        limit = self.max_group_size
        for sid in steamid_list:
            if sid in steam_ids:
                already.append(sid)
            elif len(steam_ids) < limit:
                steam_ids.append(sid)
                added.append(sid)
            else:
                break
        self.group_steam_ids[group_id] = steam_ids
        self._save_group_steam_ids()
        if bind_qq and steamid_list:
            for sid in steamid_list:
                self._bind_data[bind_qq] = {"sid": sid, "nickname": bind_nickname or "*"}
            self._save_bind_data()
            log.info(f"[绑定] QQ {bind_qq} -> SteamID {added[0] if added else steamid_list[0]}，备注={bind_nickname or '无'}")
        msg = ""
        if added:
            msg += f"已为本群添加 SteamID: {', '.join(added)}\n"
        if already:
            msg += f"以下 SteamID 已存在于本群监控组: {', '.join(already)}\n"
        if len(steam_ids) >= limit and len(added) < len(steamid_list):
            msg += f"本群监控组人数已达上限（{limit}人），部分 ID 未添加。\n"
        if added and group_id not in self.running_groups:
            self.group_monitor_enabled[group_id] = True
            self.running_groups.add(group_id)
            self.notify_sessions[group_id] = group_id
            self.group_bot_ids[group_id] = data.instance.appid
            self._save_notify_session()
            self.group_last_states.setdefault(group_id, {})
            self.group_start_play_times.setdefault(group_id, {})
            msg += "监控已自动启动。\n"
        await _send(data, msg.strip() if msg else "未添加任何 SteamID。")

    async def steam_delid(self, data: Message, args: list):
        """从监控组删除 SteamID；支持好友码/链接；可选传群号跨群删除。"""
        if not args:
            await _send(data, "用法：steam delid [SteamID] [群号可选]")
            return
        group_id = args[1].strip() if len(args) > 1 and args[1].strip() else self._group_id_of(data)
        sid = await self.resolve_steam_input(args[0])
        if not sid or not sid.isdigit() or len(sid) != 17:
            await _send(data, "无法解析为有效 SteamID，支持格式：17位 SteamID64 / 个人资料链接 / 8位好友码")
            return
        steam_ids = self.group_steam_ids.get(group_id, [])
        if not steam_ids:
            await _send(data, f"群 {group_id} 没有监控任何 SteamID")
            return
        if sid not in steam_ids:
            await _send(data, f"该 SteamID 不存在于群 {group_id} 的监控组")
            return
        steam_ids.remove(sid)
        self.group_steam_ids[group_id] = steam_ids
        self._save_group_steam_ids()
        removed_bind = []
        for qq, info in list(self._bind_data.items()):
            if info.get("sid") == sid:
                removed_bind.append(qq)
                del self._bind_data[qq]
        if removed_bind:
            self._save_bind_data()
            log.info(f"[绑定] 删除 SteamID {sid} 时同步清理绑定: QQ {', '.join(removed_bind)}")
        await _send(data, f"已为群 {group_id} 删除 SteamID: {sid}")

    async def steam_list(self, data: Message, args: list):
        """列出本群所有玩家当前状态（分群）。"""
        group_id = self._group_id_of(data)
        steam_ids = self.group_steam_ids.get(group_id, [])
        if not self.API_KEY:
            await _send(data, "未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        if not steam_ids:
            await _send(data, "本群未设置监控的 SteamID 列表，请先添加。")
            return
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        img_bytes = await handle_steam_list(self, group_id=group_id, font_path=font_path, proxy=self.proxy)
        if img_bytes:
            await _send_image(data, img_bytes)
        else:
            await _send(data, "渲染玩家列表失败，请稍后重试。")

    async def steam_alllist(self, data: Message, args: list):
        """所有群聊玩家状态（默认图片，steam alllist text 输出文本）。"""
        mode = args[0].lower() if args else 'img'
        from .game_start_render import get_avatar_frame_url, get_avatar_frame_path, get_cover_path
        from .steam_list_render import get_status_text, render_steam_list_image
        _persona_status = {0: 'offline', 1: 'online', 2: 'busy', 3: 'away', 4: 'snooze'}
        user_list = []
        now = int(time.time())
        all_sids = []
        for gid_ in self.group_steam_ids:
            all_sids.extend(self.group_steam_ids[gid_])
        status_map = await self.fetch_player_statuses_batch(all_sids) if all_sids else {}
        for group_id, steam_ids in self.group_steam_ids.items():
            start_play_times = self.group_start_play_times.get(group_id, {})
            next_poll = self.next_poll_time.get(group_id, {})
            for sid in steam_ids:
                nt = next_poll.get(sid, now)
                sl = int(nt - now)
                p_str = f"下次轮询{sl}秒后" if sl < 60 else f"下次轮询{sl // 60}分钟后"
                status = status_map.get(sid)
                if not status:
                    user_list.append({'sid': sid, 'name': self._resolve_bind_name(sid, sid), 'status': 'error',
                                      'avatar_url': '', 'game': '', 'gameid': '', 'play_str': '获取失败',
                                      'group_id': group_id, 'poll_str': p_str})
                    continue
                name = status.get('name') or sid
                gameid = status.get('gameid')
                game = status.get('gameextrainfo')
                avatar_url = status.get('avatarfull') or status.get('avatar') or ''
                zh_game_name = await self.get_chinese_game_name(gameid, game) if gameid else (game or "未知游戏")
                if gameid:
                    st_map = start_play_times.get(sid)
                    st = st_map.get(gameid) if isinstance(st_map, dict) else st_map
                    ps = now - st if st else 0
                    pm = ps / 60
                    ps_str = f"{pm:.1f}分钟" if pm < 60 else f"{pm / 60:.1f}小时"
                    user_list.append({'sid': sid, 'name': name, 'status': 'playing', 'avatar_url': avatar_url,
                                      'game': zh_game_name, 'gameid': gameid, 'play_str': ps_str,
                                      'group_id': group_id, 'poll_str': p_str})
                elif status.get('personastate', 0) > 0:
                    p_status = _persona_status.get(status.get('personastate', 0), 'online')
                    user_list.append({'sid': sid, 'name': name, 'status': p_status, 'avatar_url': avatar_url,
                                      'game': '', 'gameid': '', 'play_str': '', 'group_id': group_id, 'poll_str': p_str})
                elif status.get('lastlogoff'):
                    ha = (now - int(status['lastlogoff'])) / 3600
                    user_list.append({'sid': sid, 'name': name, 'status': 'offline', 'avatar_url': avatar_url,
                                      'game': '', 'gameid': '', 'play_str': f"上次在线 {ha:.1f} 小时前",
                                      'group_id': group_id, 'poll_str': p_str})
                else:
                    user_list.append({'sid': sid, 'name': name, 'status': 'offline', 'avatar_url': avatar_url,
                                      'game': '', 'gameid': '', 'play_str': '', 'group_id': group_id, 'poll_str': p_str})
        if mode == 'text':
            lines = ["=== Steam 全群玩家状态 ===\n"]
            by_group = {}
            for u in user_list:
                by_group.setdefault(u.get('group_id', '?'), []).append(u)
            for gid, members in by_group.items():
                lines.append(f"📋 群: {gid}")
                for u in members:
                    sicon = {'playing': '🎮', 'online': '🔵', 'offline': '💤', 'busy': '🔴',
                             'away': '🟣', 'snooze': '🟣', 'error': '⚠️'}.get(u['status'], '❓')
                    stext = get_status_text(u['status'])
                    detail = f" 正在玩：{u['game']}" if u['status'] == 'playing' and u.get('game') else ""
                    play = f" | 时长：{u['play_str']}" if u.get('play_str') else ""
                    offline_info = f" | {u['play_str']}" if u['status'] == 'offline' and u.get('play_str') else ""
                    poll = f" | {u.get('poll_str', '')}" if u.get('poll_str') else ""
                    lines.append(f"  {sicon} {u['name']} {stext}{detail}{play}{offline_info}")
                    lines.append(f"     ID: {u['sid']}{poll}")
                lines.append("")
            online_count = sum(1 for u in user_list if u['status'] in ('playing', 'online', 'away', 'snooze', 'busy'))
            lines.append(f"📊 在线: {online_count} / 总数: {len(user_list)}")
            await _send(data, "\n".join(lines))
            return
        _status_rank = {'playing': 0, 'online': 1, 'busy': 2, 'away': 3, 'snooze': 4, 'offline': 5, 'error': 6}
        user_list.sort(key=lambda u: _status_rank.get(u['status'], 9))
        avatar_frame_paths = {}
        for u in user_list:
            sid = u.get('sid', '')
            if sid:
                fp = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
                if not fp:
                    frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
                    if frame_url:
                        fp = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
                if fp:
                    avatar_frame_paths[sid] = fp
        covers = {}
        for u in user_list:
            gid = u.get('gameid', '')
            if gid:
                cp = await get_cover_path(self.data_dir, gid, u.get('game', ''), proxy=self.proxy)
                if cp:
                    covers[u['sid']] = cp
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        img_bytes = await render_steam_list_image(self.data_dir, user_list, font_path=font_path, proxy=self.proxy,
                                                  avatar_frame_paths=avatar_frame_paths, covers=covers)
        if img_bytes:
            await _send_image(data, img_bytes)
        else:
            await _send(data, "渲染图片失败")

    async def steam_config(self, data: Message, args: list):
        """显示当前插件配置（敏感信息已隐藏）。"""
        lines = []
        for k in _CONFIG_KEYS:
            if k in _CONFIG_HIDDEN:
                lines.append(f"{k}: ****** (已隐藏)")
            else:
                v = _cfg(k, None)
                if isinstance(v, list):
                    v = ",".join(str(x) for x in v)
                lines.append(f"{k}: {v}")
        lines.append(f"智能轮询间隔（分钟）: {self.smart_poll_intervals}（依次为[游戏中, 12分钟内, 12分钟~3小时, 3小时~24小时, 24~48小时, 超过48小时]）")
        await _send(data, "当前配置：\n" + "\n".join(lines))

    async def steam_set(self, data: Message, args: list):
        """设置配置参数，立即生效（如 steam set fixed_poll_interval 600）。"""
        if len(args) < 2:
            await _send(data, "用法：steam set [参数] [值]")
            return
        key = args[0]
        if key not in _CONFIG_KEYS:
            await _send(data, f"无效参数: {key}，可用参数: {', '.join(_CONFIG_KEYS)}")
            return
        value = ' '.join(args[1:])
        old = _cfg(key, None)
        try:
            if isinstance(old, bool):
                value = value.strip().lower() in ('true', '1', 'yes', 'on', '开启')
            elif isinstance(old, int):
                value = int(value)
            elif isinstance(old, float):
                value = float(value)
            elif isinstance(old, list):
                value = [int(x.strip()) for x in re.split(r'[,，]', value) if x.strip()]
            else:
                value = value
        except Exception:
            await _send(data, "类型错误，值格式与配置项类型不符。")
            return
        bot.set_config(key, value, channel_id=None)
        # 同步到实例属性，立即生效
        self.API_KEY = _cfg('steam_api_key', '')
        self.SGDB_API_KEY = _cfg('sgdb_api_key', '')
        self.RETRY_TIMES = max(1, int(_cfg('retry_times', 3)))
        self.fixed_poll_interval = int(_cfg('fixed_poll_interval', 0))
        self.smart_poll_intervals = self._parse_smart_intervals()
        self.max_group_size = int(_cfg('max_group_size', 20))
        self.rank_push_hour = int(_cfg('rank_push_hour', 8))
        self.rank_push_minute = int(_cfg('rank_push_minute', 30))
        self.ENABLE_PROXY = bool(_cfg('enable_proxy', False))
        self.PROXY_URL = _cfg('proxy_url', '') or ''
        self.proxy = self.PROXY_URL if self.ENABLE_PROXY and self.PROXY_URL else None
        await _send(data, f"已设置 {key} = {value}")

    async def steam_rs(self, data: Message, args: list):
        """清除所有状态并初始化（重启插件用）。"""
        self.group_last_states.clear()
        self.group_start_play_times.clear()
        self.group_last_quit_times.clear()
        self.group_pending_logs.clear()
        self.group_pending_quit.clear()
        self.group_recent_games.clear()
        self.next_poll_time.clear()
        self._superpower_cache.clear()
        self._game_name_cache.clear()
        self.achievement_poll_tasks.clear()
        self.achievement_snapshots.clear()
        self.achievement_fail_count.clear()
        self._recorded_quit_cache.clear()
        self._pending_end_notifications.clear()
        self.running_groups.clear()
        self.group_monitor_enabled.clear()
        self.group_achievement_enabled.clear()
        self.notify_sessions = {}
        self._save_persistent_data(force=True)
        await _send(data, "Steam 状态监控插件已重置，所有状态已清空。")

    async def steam_rank(self, data: Message, args: list):
        """查看本群玩家游戏时长排行榜（默认今日，可选 week/month/天数）。"""
        group_id = self._group_id_of(data)
        period = args[0].strip().lower() if args else ''
        if period == "week":
            days, label = 7, "最近7天"
        elif period == "month":
            days, label = 30, "最近30天"
        elif period.isdigit():
            days = max(1, int(period))
            label = f"最近{days}天"
        else:
            days, label = 1, "今日"
        await self._render_and_send_rank(data, group_id, days, label, is_all=False)

    async def steam_allrank(self, data: Message, args: list):
        """查看所有群玩家游戏时长排行榜（默认今日，可选 week/month/天数）。"""
        period = args[0].strip().lower() if args else ''
        if period == "week":
            days, label = 7, "最近7天"
        elif period == "month":
            days, label = 30, "最近30天"
        elif period.isdigit():
            days = max(1, int(period))
            label = f"最近{days}天"
        else:
            days, label = 1, "今日"
        await self._render_and_send_rank(data, None, days, label, is_all=True)

    async def steam_rank_on(self, data: Message, args: list):
        """每日排行榜推送管理；参数: all=全局排行, list=查看状态, test=即刻推送, del [群号]=删除推送。"""
        param = ' '.join(args).strip().lower()
        group_id = self._group_id_of(data)
        if param == "list":
            is_all = self.rank_push_all
            groups = list(self.rank_push_groups)
            if groups:
                mode = '全局' if is_all else '分群'
                await _send(data, f"当前排行榜推送模式：{mode}排行，推送群：{', '.join(groups)}")
            else:
                await _send(data, "当前未开启任何排行榜推送。使用「steam rank_on」或「steam rank_on all」开启。")
            return
        if param == "test":
            await _send(data, "正在生成昨日排行榜，稍等...")
            await self._daily_rank_push(test_mode=True)
            return
        if param.startswith("del"):
            parts = param.split()
            target = parts[1] if len(parts) >= 2 else group_id
            if target in self.rank_push_groups:
                self.rank_push_groups.remove(target)
                self._save_rank_push_groups()
                await _send(data, f"已关闭群 {target} 的每日排行榜推送。")
            else:
                await _send(data, f"群 {target} 未在推送列表中。")
            return
        if param == "all":
            self.rank_push_all = True
            if group_id not in self.rank_push_groups:
                self.rank_push_groups.append(group_id)
            self._save_rank_push_groups()
            await _send(data, "已开启每日排行榜自动推送（全局排行）")
        else:
            self.rank_push_all = False
            if group_id not in self.rank_push_groups:
                self.rank_push_groups.append(group_id)
                self._save_rank_push_groups()
            await _send(data, "已开启本群每日排行榜自动推送。")

    async def steam_help(self, data: Message, args: list):
        """显示所有指令帮助。"""
        help_text = (
            "Steam 状态监控插件指令：\n"
            "steam on - 启动本群监控\n"
            "steam off - 停止本群监控\n"
            "steam list - 列出本群所有玩家状态（图片）\n"
            "steam alllist [img|text] - 查看所有群玩家状态\n"
            "steam config - 查看当前配置\n"
            "steam set [参数] [值] - 设置配置参数\n"
            "steam addid [SteamID] [@用户] - 添加 SteamID（可绑定 QQ）\n"
            "steam delid [SteamID] [群号] - 删除 SteamID\n"
            "steam push_group [SteamID] - 本群加入该 ID 的联动推送组\n"
            "steam delpush_group [SteamID] [群号] - 移出联动推送组\n"
            "steam openbox [SteamID] - 查看指定 SteamID 全部信息\n"
            "steamwho @用户 / 在干嘛 @用户 - 查询某人绑定账号状态\n"
            "steam rank [天数] - 本群排行榜（默认今日）\n"
            "steam allrank [天数] - 所有群排行榜\n"
            "steam rank_on [all|list|test|del] - 管理每日排行榜推送\n"
            "steam achievement_on / achievement_off - 开启/关闭本群成就推送\n"
            "steam rs - 清除状态并初始化\n"
            "steam help - 显示本帮助\n"
        )
        await _send(data, help_text)

    async def steam_openbox(self, data: Message, args: list):
        """查询指定 SteamID 的全部 API 返回信息。"""
        if not args:
            await _send(data, "用法：steam openbox [SteamID]")
            return
        if not self.API_KEY:
            await _send(data, "未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        sid = await self.resolve_steam_input(args[0])
        if not sid or not sid.isdigit() or len(sid) != 17:
            await _send(data, "无法解析为有效 SteamID，支持格式：17位 SteamID64 / 个人资料链接 / 自定义ID链接 / 8位好友码")
            return
        avatar_url, text = await handle_openbox(self, sid)
        if avatar_url:
            await data.send(Chain(data, at=False).text(text).image(url=avatar_url))
        else:
            await _send(data, text)

    async def steam_who(self, data: Message, args: list):
        """查询指定 QQ 绑定的 Steam 玩家状态（steamwho @用户 / 在干嘛 @用户）。"""
        qq = args[0] if args else (data.at_target[0] if data.at_target else data.user_id)
        qq_clean = qq.strip().lstrip('@')
        info = self._bind_data.get(qq_clean)
        if not info:
            await _send(data, f"QQ {qq_clean} 未绑定任何 SteamID，请先使用「steam addid SteamID @{qq_clean}」（或「steam addid SteamID {qq_clean}」）绑定")
            return
        sid = info.get("sid", "")
        if not sid:
            await _send(data, f"QQ {qq_clean} 的绑定数据异常")
            return
        status = await self.fetch_player_status(sid)
        if not status:
            await _send(data, f"无法获取 {sid} 的 Steam 状态")
            return
        name = self._resolve_bind_name(sid, status.get('name') or sid)
        gameid = status.get('gameid')
        game = status.get('gameextrainfo')
        personastate = status.get('personastate', 0)
        avatar_url = status.get('avatarfull') or status.get('avatar') or ''
        lastlogoff = status.get('lastlogoff')
        zh_game_name = await self.get_chinese_game_name(gameid, game) if gameid else (game or '')
        now = int(time.time())
        group_id = self._group_id_of(data)
        start_play_times = self.group_start_play_times.get(group_id, {}).get(sid, {})
        if gameid:
            start_time = start_play_times.get(gameid) if isinstance(start_play_times, dict) else None
            if not start_time and isinstance(start_play_times, dict) and start_play_times:
                start_time = max(start_play_times.values())
            if not start_time and not isinstance(start_play_times, dict):
                start_time = start_play_times
            play_seconds = now - start_time if start_time else 0
            play_minutes = play_seconds / 60
            play_str = f"{play_minutes / 60:.1f}小时" if play_minutes >= 60 else f"{play_minutes:.1f}分钟"
            user_list = [{'sid': sid, 'name': name, 'status': 'playing', 'avatar_url': avatar_url,
                          'game': zh_game_name, 'gameid': gameid, 'play_str': play_str, 'lastlogoff': lastlogoff}]
        elif personastate and int(personastate) > 0:
            _persona_status = {0: 'offline', 1: 'online', 2: 'busy', 3: 'away', 4: 'snooze'}
            p_status = _persona_status.get(int(personastate), 'online')
            user_list = [{'sid': sid, 'name': name, 'status': p_status, 'avatar_url': avatar_url,
                          'game': '', 'gameid': '', 'play_str': '', 'lastlogoff': lastlogoff}]
        else:
            hours_ago = (now - int(lastlogoff)) / 3600 if lastlogoff else 0
            play_str = f"上次在线 {hours_ago:.1f}小时前" if lastlogoff else ''
            user_list = [{'sid': sid, 'name': name, 'status': 'offline', 'avatar_url': avatar_url,
                          'game': '', 'gameid': '', 'play_str': play_str, 'lastlogoff': lastlogoff}]
        from .game_start_render import get_avatar_frame_url, get_avatar_frame_path, get_cover_path
        avatar_frame_paths = {}
        fp = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
        if not fp:
            frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
            if frame_url:
                fp = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
        if fp:
            avatar_frame_paths[sid] = fp
        covers = {}
        if gameid:
            cp = await get_cover_path(self.data_dir, gameid, game or zh_game_name, proxy=self.proxy)
            if cp:
                covers[sid] = cp
        from .steam_list_render import render_steam_list_image
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        img_bytes = await render_steam_list_image(self.data_dir, user_list, font_path=font_path, proxy=self.proxy,
                                                  avatar_frame_paths=avatar_frame_paths, covers=covers)
        if img_bytes:
            await _send_image(data, img_bytes)
        else:
            await _send(data, "渲染图片失败")

    async def steam_zai_gan_ma(self, data: Message, args: list):
        """在干嘛 @用户 —— steamwho 的别名。"""
        await self.steam_who(data, args)

    async def steam_off(self, data: Message, args: list):
        """彻底停止本群 Steam 状态监控轮询，释放轮询资源。"""
        group_id = self._group_id_of(data)
        self.group_monitor_enabled[group_id] = False
        if group_id in self.running_groups:
            self.running_groups.remove(group_id)
        self.next_poll_time.pop(group_id, None)
        self.group_pending_quit.pop(group_id, None)
        keys_to_cancel = [k for k in list(self.achievement_poll_tasks.keys()) if k[0] == group_id]
        for key in keys_to_cancel:
            task = self.achievement_poll_tasks.pop(key, None)
            if task:
                task.cancel()
        for sid in list(self.group_steam_ids.get(group_id, [])):
            task_map = self._pending_quit_tasks.pop(sid, None)
            if task_map:
                for task in task_map.values():
                    task.cancel()
        await _send(data, f"已为本群彻底关闭 Steam 监控，轮询已停止。使用「steam on」可重新启动。")

    async def steam_achievement_on(self, data: Message, args: list):
        """开启本群 Steam 成就推送。"""
        group_id = self._group_id_of(data)
        self.group_achievement_enabled[group_id] = True
        await _send(data, "已为本群开启 Steam 成就推送。")

    async def steam_achievement_off(self, data: Message, args: list):
        """关闭本群 Steam 成就推送。"""
        group_id = self._group_id_of(data)
        self.group_achievement_enabled[group_id] = False
        await _send(data, "已为本群关闭 Steam 成就推送。")

    async def steam_test_achievement_render(self, data: Message, args: list):
        """测试成就消息渲染效果（steam test_achievement_render [steamid] [gameid] [数量]）。"""
        if len(args) < 2:
            await _send(data, "用法：steam test_achievement_render [steamid] [gameid] [数量可选]")
            return
        steamid, gameid = args[0], int(args[1])
        count = int(args[2]) if len(args) > 2 else 3
        player_name = steamid
        game_name = await self.get_chinese_game_name(gameid)
        group_id = self._group_id_of(data)
        achievements = await self.achievement_monitor.get_player_achievements(self.API_KEY, group_id, steamid, gameid)
        if not achievements:
            await _send(data, "未获取到任何成就，可能为隐私或无成就。")
            return
        details = await self.achievement_monitor.get_achievement_details(group_id, gameid, lang="schinese",
                                                                         api_key=self.API_KEY, steamid=steamid)
        count = max(1, min(count, len(achievements)))
        unlocked = set(random.sample(list(achievements), count))
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        try:
            img_bytes = await self.achievement_monitor.render_achievement_image(
                details, unlocked, player_name=player_name, font_path=font_path)
            if img_bytes:
                await _send_image(data, img_bytes)
            else:
                msg = self.achievement_monitor.render_achievement_message(details, unlocked, player_name=player_name)
                await _send(data, msg)
        except Exception as e:
            log.error(f"成就图片渲染失败: {e}\n{traceback.format_exc()}")
            msg = self.achievement_monitor.render_achievement_message(details, unlocked, player_name=player_name)
            await _send(data, msg)

    async def test_game_start_render(self, data: Message, args: list):
        """测试开始游戏图片渲染效果（steam test_game_start_render [steamid] [gameid]）。"""
        if len(args) < 2:
            await _send(data, "用法：steam test_game_start_render [steamid] [gameid]")
            return
        steamid, gameid = args[0], int(args[1])
        try:
            status = await self.fetch_player_status(steamid)
            player_name = self._resolve_bind_name(steamid, status.get("name") if status else steamid)
            avatar_url = status.get("avatarfull") or status.get("avatar") or "" if status else ""
            zh_game_name, en_game_name = await self.get_game_names(gameid)
            superpower = self.get_today_superpower(steamid)
            font_path = self.get_font_path('NotoSansHans-Regular.otf')
            online_count = await self.get_game_online_count(gameid)
            img_bytes = await render_game_start(
                self.data_dir, steamid, player_name, avatar_url, gameid, zh_game_name,
                api_key=self.API_KEY, superpower=superpower, sgdb_api_key=self.SGDB_API_KEY,
                font_path=font_path, sgdb_game_name=en_game_name, online_count=online_count,
                appid=gameid, proxy=self.proxy, version=self._plugin_version)
            if img_bytes:
                img = PILImage.open(io.BytesIO(img_bytes))
                cropped = self.crop_image_auto(img, bg_color=(51, 81, 66), threshold=15)
                buf = io.BytesIO()
                cropped.save(buf, format="PNG")
                await _send_image(data, buf.getvalue())
            else:
                await _send(data, "渲染失败，未获取到图片数据。")
        except Exception as e:
            log.error(f"测试开始游戏图片渲染失败: {e}\n{traceback.format_exc()}")
            await _send(data, f"渲染异常: {e}")

    async def steam_test_game_end_render(self, data: Message, args: list):
        """测试游戏结束图片渲染（steam test_game_end_render [steamid] [gameid] [时长分钟] [结束时间] [提示]）。"""
        if len(args) < 2:
            await _send(data, "用法：steam test_game_end_render [steamid] [gameid] [时长分钟可选] [结束时间可选] [提示可选]")
            return
        steamid, gameid = args[0], int(args[1])
        duration_min = float(args[2]) if len(args) > 2 else 120
        end_time = args[3] if len(args) > 3 else None
        tip_text = ' '.join(args[4:]) if len(args) > 4 else None
        try:
            status = await self.fetch_player_status(steamid)
            player_name = self._resolve_bind_name(steamid, status.get("name") if status else steamid)
            avatar_url = status.get("avatarfull") or status.get("avatar") or "" if status else ""
            zh_game_name, en_game_name = await self.get_game_names(gameid)
            end_time_str = end_time or datetime.now().strftime("%Y-%m-%d %H:%M")
            duration_h = duration_min / 60 if duration_min else 0
            if not tip_text:
                if duration_min < 5:
                    tip_text = "风扇都没转热，主人就结束了？"
                elif duration_min < 10:
                    tip_text = "杂鱼杂鱼~主人你就这水平？"
                elif duration_min < 30:
                    tip_text = "热身一下就结束了？"
                elif duration_min < 60:
                    tip_text = "歇会儿再来，别太累了喵！"
                elif duration_min < 120:
                    tip_text = "沉浸在游戏世界，时间过得飞快喵！"
                elif duration_min < 300:
                    tip_text = "肝到手软了喵！主人不如陪陪咱~"
                elif duration_min < 600:
                    tip_text = "你吃饭了吗？还是说你已经忘了吃饭这件事？"
                elif duration_min < 1200:
                    tip_text = "家里电费都要被你玩光了喵！"
                elif duration_min < 1800:
                    tip_text = "咱都要给你颁发'不眠猫'勋章了！"
                elif duration_min < 2400:
                    tip_text = "主人你还活着喵？你是不是忘了关电脑呀~"
                else:
                    tip_text = "你已经和椅子合为一体，成为传说中的'椅子精'了喵！"
            font_path = self.get_font_path('NotoSansHans-Regular.otf')
            img_bytes = await render_game_end(
                self.data_dir, steamid, player_name, avatar_url, gameid, zh_game_name,
                end_time_str, tip_text, duration_h, sgdb_api_key=self.SGDB_API_KEY,
                font_path=font_path, sgdb_game_name=en_game_name, appid=gameid, proxy=self.proxy)
            msg = f"👋 {player_name} 不玩 {zh_game_name} 了\n游玩时间 {duration_h:.1f}小时"
            await _send(data, msg)
            if img_bytes:
                await _send_image(data, img_bytes)
        except Exception as e:
            log.error(f"测试游戏结束图片渲染失败: {e}\n{traceback.format_exc()}")
            await _send(data, f"渲染异常: {e}")

    async def steam_clear_cache(self, data: Message, args: list):
        """清除所有头像、封面图等图片缓存（慎用）。"""
        try:
            cache_dirs = [
                os.path.join(self.data_dir, "avatars"),
                os.path.join(self.data_dir, "covers"),
                os.path.join(self.data_dir, "covers_v"),
            ]
            cleared = []
            for d in cache_dirs:
                if os.path.exists(d):
                    shutil.rmtree(d)
                    cleared.append(d)
            msg = "已清除以下缓存目录：\n" + "\n".join(cleared) if cleared else "未找到任何缓存目录，无需清理。"
            await _send(data, msg)
        except Exception as e:
            await _send(data, f"清除缓存失败: {e}")

    async def steam_clear_allids(self, data: Message, args: list):
        """删除所有群聊的所有已监控 SteamID，并清空相关状态数据。"""
        self.group_steam_ids.clear()
        self._save_group_steam_ids()
        self.group_last_states.clear()
        self.group_start_play_times.clear()
        self.group_last_quit_times.clear()
        self.group_pending_logs.clear()
        self.group_pending_quit.clear()
        self.group_recent_games.clear()
        self._save_persistent_data(force=True)
        await _send(data, "已删除所有群聊的所有 SteamID，相关状态数据已清空。")

    async def steam_clear_groupids(self, data: Message, args: list):
        """删除指定群聊的所有已监控 SteamID，并清空相关状态数据。"""
        group_id = args[0] if args else ''
        if not group_id:
            await _send(data, "用法：steam clear_groupids [群号]")
            return
        if group_id not in self.group_steam_ids:
            await _send(data, f"群聊 {group_id} 未绑定任何 SteamID，无需清理。")
            return
        self.group_steam_ids.pop(group_id, None)
        self._save_group_steam_ids()
        self.group_last_states.pop(group_id, None)
        self.group_start_play_times.pop(group_id, None)
        self.group_last_quit_times.pop(group_id, None)
        self.group_pending_logs.pop(group_id, None)
        self.group_pending_quit.pop(group_id, None)
        self.group_recent_games.pop(group_id, None)
        self._save_persistent_data(force=True)
        self.notify_sessions.pop(group_id, None)
        await _send(data, f"已删除群聊 {group_id} 的所有 SteamID，相关状态数据已清空。")

    async def steam_push_group(self, data: Message, args: list):
        """将本群加入指定 SteamID 的联动推送组（不重复轮询，仅同步推送）。"""
        if not args:
            await _send(data, "用法：steam push_group [SteamID]")
            return
        steamid = args[0]
        group_id = self._group_id_of(data)
        if not steamid.isdigit() or len(steamid) != 17:
            await _send(data, "SteamID 无效（需为64位数字串，17位）")
            return
        found = False
        for gid, ids in self.group_steam_ids.items():
            if steamid in ids:
                found = True
                break
        if not found:
            await _send(data, "未找到已轮询该 SteamID 的主群，请先在任一群添加并开启监控。")
            return
        self.push_groups.setdefault(steamid, [])
        if group_id not in self.push_groups[steamid]:
            self.push_groups[steamid].append(group_id)
            self._save_push_groups()
            await _send(data, f"本群已加入 SteamID {steamid} 的联动推送组。")
        else:
            await _send(data, "本群已在该 SteamID 的推送组中。")

    async def steam_delpush_group(self, data: Message, args: list):
        """将当前群/指定群从 SteamID 的联动推送组移除；可传群号指定。"""
        if not args:
            await _send(data, "用法：steam delpush_group [SteamID] [群号可选]")
            return
        steamid = args[0]
        group_id = args[1].strip() if len(args) > 1 and args[1].strip() else self._group_id_of(data)
        if not steamid.isdigit() or len(steamid) != 17:
            await _send(data, "SteamID 无效（需为64位数字串，17位）")
            return
        if steamid not in self.push_groups or group_id not in self.push_groups[steamid]:
            await _send(data, f"群 {group_id} 未在 SteamID {steamid} 的推送组中。")
            return
        self.push_groups[steamid].remove(group_id)
        if not self.push_groups[steamid]:
            self.push_groups.pop(steamid)
        self._save_push_groups()
        if args[1] if len(args) > 1 else '':
            await _send(data, f"已从 SteamID {steamid} 的联动推送组中移除群 {group_id}。")
        else:
            await _send(data, f"本群已从 SteamID {steamid} 的联动推送组移除。")

    def crop_image_auto(self, img, bg_color=None, threshold=15):
        """自动裁剪图片内容区域，去除边缘与背景色相近的空白（纯 Pillow 实现）。"""
        if not isinstance(img, PILImage.Image):
            img = PILImage.open(io.BytesIO(img) if isinstance(img, bytes) else img).convert("RGB")
        img = img.convert("RGB")
        if bg_color is None:
            w, h = img.size
            corners = [img.getpixel((0, 0)), img.getpixel((w - 1, 0)), img.getpixel((0, h - 1)), img.getpixel((w - 1, h - 1))]
            bg_color = tuple(int(round(sum(c[i] for c in corners) / 4)) for i in range(3))
        diff = ImageChops.difference(img, PILImage.new("RGB", img.size, bg_color))
        mask = diff.convert("L").point(lambda p: 255 if p > threshold else 0)
        bbox = mask.getbbox()
        if not bbox:
            return img
        pad = 2
        x0, y0 = max(bbox[0] - pad, 0), max(bbox[1] - pad, 0)
        x1, y1 = min(bbox[2] + pad, img.size[0]), min(bbox[3] + pad, img.size[1])
        return img.crop((x0, y0, x1, y1))


# ============================================================
# 模块级发送辅助
# ============================================================

async def _send(data: Message, text: str, *, at: bool = False):
    await data.send(Chain(data, at=at).text(text))


async def _send_image(data: Message, img_bytes: bytes, *, at: bool = False):
    await data.send(Chain(data, at=at).image(img_bytes))


# 配置项清单（与 config_default.yaml 保持一致）
_CONFIG_KEYS = [
    'steam_api_key', 'sgdb_api_key', 'steam_api_base', 'steam_store_base', 'sgdb_api_base',
    'retry_times', 'enable_proxy', 'proxy_url', 'fixed_poll_interval', 'smart_poll_intervals',
    'detailed_poll_log', 'max_group_size', 'cache_avatar_hours', 'cache_avatar_frame_hours',
    'cache_cover_vertical_hours', 'rank_push_hour', 'rank_push_minute', 'notify_send_text',
    'notify_send_image', 'enable_network_fluctuation_notify', 'enable_game_start_notify',
    'enable_game_end_notify', 'enable_achievement_poll', 'game_filter_mode', 'game_filter_ids',
]
_CONFIG_HIDDEN = {'steam_api_key', 'sgdb_api_key'}

# 需要管理员权限的子指令
_ADMIN_COMMANDS = {
    'addid', 'delid', 'set', 'rs', 'rank_on', 'off',
    'achievement_on', 'achievement_off',
    'test_achievement_render', 'test_game_start_render', 'test_game_end_render',
    'clear_cache', 'clear_allids', 'clear_groupids',
    'push_group', 'delpush_group',
}


def _in_steam_context(text: str) -> bool:
    """判断消息是否处于 steam 指令语境（支持 bot 前缀或裸指令）。"""
    m = re.search(r'(?i)steam', text)
    if not m:
        return False
    head = text[:m.start()].strip()
    if not head:
        return True
    try:
        prefixes = [p for p in (bot.prefix_keywords or []) if p]
    except Exception:
        prefixes = []
    return any(head == p or head.endswith(p) for p in prefixes)


async def _dispatch_steam(data: Message, text: str):
    m = re.search(r'(?i)steam', text)
    tail = text[m.end():].strip()
    if not tail:
        await _send(data, _st.steam_help(data, []))
        return
    try:
        parts = shlex.split(tail)
    except Exception:
        parts = tail.split()
    if not parts:
        await _st.steam_help(data, [])
        return
    sub = parts[0].lower()
    args = parts[1:]
    if not data.is_direct:
        _st.group_bot_ids[_st._group_id_of(data)] = data.instance.appid
    handler = _COMMAND_ROUTES.get(sub)
    if handler is None:
        await _send(data, f"未知指令：steam {sub}，输入「steam help」查看帮助。")
        return
    if sub in _ADMIN_COMMANDS and not data.is_admin and not data.is_direct:
        await _send(data, "该指令需要管理员权限。")
        return
    try:
        await handler(data, args)
    except Exception as e:
        log.error(f"[Steam状态监控] 指令 steam {sub} 执行失败: {e}\n{traceback.format_exc()}")
        await _send(data, f"指令执行失败: {e}")


# ============================================================
# 实例与注册
# ============================================================

_st = SteamStatusMonitorV3()

_COMMAND_ROUTES = {
    'on': _st.steam_on,
    'addid': _st.steam_addid,
    'delid': _st.steam_delid,
    'list': _st.steam_list,
    'alllist': _st.steam_alllist,
    'config': _st.steam_config,
    'set': _st.steam_set,
    'rs': _st.steam_rs,
    'rank': _st.steam_rank,
    'allrank': _st.steam_allrank,
    'rank_on': _st.steam_rank_on,
    'help': _st.steam_help,
    'openbox': _st.steam_openbox,
    'who': _st.steam_who,
    'off': _st.steam_off,
    'achievement_on': _st.steam_achievement_on,
    'achievement_off': _st.steam_achievement_off,
    'test_achievement_render': _st.steam_test_achievement_render,
    'test_game_start_render': _st.test_game_start_render,
    'test_game_end_render': _st.steam_test_game_end_render,
    'clear_cache': _st.steam_clear_cache,
    'clear_allids': _st.steam_clear_allids,
    'clear_groupids': _st.steam_clear_groupids,
    'push_group': _st.steam_push_group,
    'delpush_group': _st.steam_delpush_group,
}


@bot.on_message(keywords=['steam'], allow_direct=True, level=5)
async def _(data: Message):
    text = (data.text or '').strip()
    if not text or not _in_steam_context(text):
        return None
    await _dispatch_steam(data, text)
    return None


@bot.on_message(keywords=['在干嘛'], allow_direct=True, level=5)
async def _(data: Message):
    text = (data.text or '').strip()
    m = re.search(r'在干嘛', text)
    if not m:
        return None
    if not data.is_direct:
        _st.group_bot_ids[_st._group_id_of(data)] = data.instance.appid
    tail = text[m.end():].strip()
    qq = data.at_target[0] if data.at_target else tail.lstrip('@').strip()
    await _st.steam_who(data, [qq] if qq else [])
    return None


@bot.timed_task(each=20, sub_tag='steam_poll')
async def _steam_poll_tick(_):
    await _st.poll_tick()


def install():
    log.info("[Steam状态监控] 插件安装/加载完成")


def uninstall():
    """卸载时取消所有未完成的延迟任务并强制保存数据。"""
    for sid, task_map in list(_st._pending_quit_tasks.items()):
        for task in task_map.values():
            task.cancel()
    _st._pending_quit_tasks.clear()
    for task in _st.achievement_poll_tasks.values():
        try:
            task.cancel()
        except Exception:
            pass
    _st.achievement_poll_tasks.clear()
    try:
        _st._save_persistent_data(force=True)
    except Exception as e:
        log.warning(f"[Steam状态监控] 卸载时保存数据失败: {e}")
    log.info("[Steam状态监控] 插件已卸载，定时任务与推送均已清理")
