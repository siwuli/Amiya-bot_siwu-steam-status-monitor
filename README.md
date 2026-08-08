# Steam 状态监控（Amiya-Bot 移植版）

监控群内 Steam 玩家在线状态与游戏动态：开始/结束游戏、成就解锁、网络波动自动推送，游戏时长排行榜与每日榜单推送，支持分群管理与绑定。

> 本插件移植自 [astrbot_plugin_steam_status_monitor](https://github.com/Maoer233/astrbot_plugin_steam_status_monitor)（作者：Maoer，GPL-3.0），在此感谢原作者的辛勤付出。

## 功能特性

- **分群监控**：每个群独立维护 SteamID 列表与监控开关，互不干扰
- **智能轮询**：默认按玩家状态自动调整轮询间隔（游戏中 1 分钟，长时间离线最多 30 分钟），也可配置固定轮询间隔；跨群批量查询并去重，节省 API 调用
- **开始/结束游戏通知**：图片卡片 + 文本推送，结束通知延迟 3 分钟结算（退出缓冲），支持合并发送
- **成就监控**：游戏开始后定时对比成就快照，解锁即推送；游戏结束后 15 分钟延迟终检，支持黑名单与失败重试
- **游戏时长排行榜**：今日/最近 N 天/全群排行，每日定时推送昨日榜单（`rank_push_hour` / `rank_push_minute` 配置）
- **绑定查询**：`steam addid SteamID @用户` 将 QQ 与 SteamID 绑定，`steamwho @用户` / `在干嘛 @用户` 一键查询
- **联动推送组**：`push_group` / `delpush_group` 将副群挂到某 SteamID 的推送链上（不重复轮询）
- **多账号支持**：每个群的推送自动路由到消息来源对应的机器人实例

## 安装

在兔兔控制台 -> 插件管理中安装 `siwu-steam-status-monitor-1.0.zip`，然后在插件配置中填写：

| 配置项 | 说明 |
| --- | --- |
| `steam_api_key` | **必填**。Steam Web API Key，获取：https://steamcommunity.com/dev/apikey |
| `sgdb_api_key` | 可选。SteamGridDB Key，用于游戏封面图，获取：https://www.steamgriddb.com/profile/preferences/api |
| `enable_proxy` / `proxy_url` | 可选。国内访问 Steam 接口较慢时可配置 HTTP 代理（SOCKS 不受支持） |

## 指令

指令需带机器人前缀（如 `兔兔`）或 @机器人，以下省略前缀，直接写 `steam` 即可触发。

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `steam on` / `steam off` | 启动 / 彻底停止本群监控 | off 需管理员 |
| `steam addid [SteamID] [@用户] [备注]` | 添加监控玩家（支持 17 位 ID / 链接 / 好友码，逗号分隔多个，可绑定 QQ） | 管理员 |
| `steam delid [SteamID] [群号]` | 删除监控玩家（可跨群） | 管理员 |
| `steam list` | 本群所有玩家当前状态（图片） | 全员 |
| `steam alllist [img\|text]` | 所有群玩家状态 | 全员 |
| `steam openbox [SteamID]` | 查看指定 SteamID 的全部 API 信息 | 全员 |
| `steamwho @用户` / `在干嘛 @用户` | 查询某人绑定账号状态 | 全员 |
| `steam rank [天数]` | 本群排行榜（默认今日，支持 week / month / 数字天数） | 全员 |
| `steam allrank [天数]` | 所有群排行榜 | 全员 |
| `steam rank_on [all\|list\|test\|del]` | 管理每日排行榜推送 | 管理员 |
| `steam achievement_on` / `achievement_off` | 开启 / 关闭本群成就推送 | 管理员 |
| `steam push_group [SteamID]` | 本群加入该 ID 的联动推送组 | 管理员 |
| `steam delpush_group [SteamID] [群号]` | 本群/指定群移出联动推送组 | 管理员 |
| `steam config` | 查看当前配置（敏感项隐藏） | 全员 |
| `steam set [参数] [值]` | 运行时修改配置，立即生效 | 管理员 |
| `steam rs` | 清除所有状态并初始化 | 管理员 |
| `steam clear_cache` / `clear_allids` / `clear_groupids [群号]` | 清理缓存 / 清空所有群 ID / 清空指定群 ID | 管理员 |
| `steam test_game_start_render [sid] [gameid]` 等 | 测试各类图片渲染效果 | 管理员 |
| `steam help` | 帮助 | 全员 |

## 数据存储

运行时数据保存在 `data/steam_status_monitor/` 目录下：

- `steam_groups.json`：分群 SteamID 列表
- `group_*_*.json`：分群状态、开始时间、退出缓冲、待推送日志等
- `notify_sessions.json`：各群推送目标
- `push_groups.json`：联动推送组
- `play_records.json` / `session_records.json`：排行榜与游玩记录
- `bind_data.json`：QQ-SteamID 绑定
- `rank_push_groups.json`：每日榜单推送配置
- `avatars/`、`covers/` 等：图片缓存（可用 `steam clear_cache` 清理）

## 更新日志

> 新版本发布时在表格最上方插入一行即可。

| 版本 | 日期 | 更新内容 |
| --- | --- | --- |
| v1.1 | 2026-08-09 | **修复**：<br>- 修复 HTTP 连接释放后无法读取响应体（`ClientConnectionError`），改为缓存完整 body 的轻量响应对象<br>- 请求失败记录真实异常类型（超时/连接失败/DNS），不再吞成"无响应"<br>- 轮询任务防重叠，避免网络故障期间定时任务被反复跳过<br>- 批量查询失败的玩家本轮跳过状态检测，不再重复逐条重试<br>- 图片缓存有效性校验，0 字节损坏文件自动重新下载<br>- `steam addid SteamID QQ号` 支持尾随 QQ 号绑定（无需 @ 自己）<br>- 素材文件名改 ASCII，规避 Amiya-Bot 解压中文文件名的兼容问题 |
| v1.0 | 2026-08-09 | **主要功能**：<br>- 完整移植原版插件，功能对齐<br>- 分群监控：智能/固定轮询、跨群批量查询去重<br>- 开始/结束游戏、成就解锁、网络波动自动推送（图片卡片 + 文本）<br>- 游戏时长排行榜（今日/最近 N 天/全群）与每日榜单推送<br>- QQ-SteamID 绑定、联动推送组、多账号机器人支持 |

## 许可证

本插件基于 GPL-3.0 许可证开源，衍生自 [astrbot_plugin_steam_status_monitor](https://github.com/Maoer233/astrbot_plugin_steam_status_monitor)（作者：Maoer）。

## 项目地址

https://github.com/siwuli/Amiya-bot_siwu-steam-status-monitor
