# 实时增量工作归档

“工作归档”把用户已经确认的会话持续写入本地 Markdown/JSON 目录。它直接读取 realtime WCDB，不要求关闭微信，也不会为了追赶进度写入可能滞后的 decrypted snapshot。

## 使用流程

1. 在桌面侧栏打开“工作归档”，选择现有归档根目录和对应微信账号。
2. 运行接管预检。预检会核对 `_archive_plan.json`、会话目录、八字段 JSON、manifest、本地媒体目标和 realtime 源消息数。
3. 只有全部一致时才能接管。接管会导入 `work_groups` 与 `work_singles`，并把计划中的 `borderline` 和 `excluded` 记为已排除，避免旧会话被误报为新会话。
4. 接管后自动归档仍保持关闭。用户明确开启后，开关会保存并随下次启动恢复。

默认节奏是每 10 秒检测一次数据库变化，静默 30 秒后批量处理；持续聊天最多等待 120 秒，每 30 分钟执行一次完整对账。软件退出期间不会后台运行，重新启动后会根据 SQLite 游标和消息去重键追赶。

## 数据与安全边界

- Profile 注册表位于 `get_output_dir()/work_archive/profiles.json`。
- 每个归档根目录的运行状态位于 `.work-archive/state.sqlite3`，日志和接管差异报告也只保存在本机。
- 消息优先按 `server_id` 去重；缺失时使用 `数据库 + 消息表 + local_id`。游标同时记录 `create_time/sort_seq/local_id`，同步时回读 5 秒重叠窗口。
- 新会话只保存稳定 username、显示名和群聊标识，并进入“待确认”。确认前不读取消息内容、不创建会话目录。
- 媒体只通过上游已有的本地资源查找、图片解密和 realtime 语音读取能力写入，不使用远程 key 或 CDN。缺失媒体按 30 秒、2 分钟、10 分钟、1 小时重试，之后每 6 小时重试。
- JSON、Markdown、manifest 和状态报告先写同目录临时文件，再使用原子替换。单个文件替换失败时原文件保持不变。

## 输出兼容

每个 `聊天记录_<会话>.json` 继续是单文件数组，记录字段固定为：

```text
time, ts, sender, sender_wxid, type, content, archived, card
```

显示名变化不会改变归档身份；稳定 username 始终用于游标、去重和会话状态。已有媒体不会复制，新媒体放入会话目录下的 `图片/文件/视频/语音` 等目录并使用相对链接。

## 本地 API

所有接口位于 `/api/work-archive`：

- `GET/POST /profiles`，`PUT/DELETE /profiles/{id}`
- `POST /adoption/preflight`，`POST /adoption`
- `POST /profiles/{id}/conversations/status`
- `GET /profiles/{id}/preview|status|pending|verify`
- `POST /profiles/{id}/sync|cancel`
- `GET /profiles/{id}/events`（SSE）

## MCP

只读工具：

- `wechat.archive.list_profiles`
- `wechat.archive.get_status`
- `wechat.archive.list_pending`
- `wechat.archive.preview_sync`
- `wechat.archive.verify`

写入工具：

- `wechat.archive.set_conversation_status`
- `wechat.archive.run_sync`
- `wechat.archive.cancel_sync`

写入工具只接受注册过的 `profile_id`，不接受任意输出路径。将会话设置为 `included` 前必须先获得用户明确确认。
