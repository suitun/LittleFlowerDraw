# 小花绘开发约定

## 项目信息与当前范围

- 展示名称：小花绘；仓库与目录名：`LittleFlowerDraw`。
- 说明：让小花帮你画。
- 用户称呼的版本为 `0.9`，清单使用框架要求的三段式版本 `0.9.0`。
- 稳定插件 ID：`suitun.little-flower-draw`，不要因展示名称变化而改 ID。
- 仓库：<https://github.com/suitun/LittleFlowerDraw>，主要维护 `main` 分支。
- 当前面向真实 QQ + SnowLuma 开发。离线单元测试可以使用测试事件与 SDK 替身，但不能宣称完成真实 QQ 联调。
- 现阶段实现角色图片登记、同名覆盖、按名查看，WebUI 统一入口「小花绘」下的角色库与插件设置模块，单角色替换、双角色替换、多人替换三类绘图指令，OpenAI 兼容图片接口，持久任务队列、当前任务与任务历史（提示词模板预留入口）；角色图执行时读取最新版本，参考图提交时保存。
- 角色库在当前机器人内全局共享：所有群和用户使用同一份角色记录；在权限和白名单允许时，任何人都能登记、查看或覆盖同名角色。不要按群或用户拆库。
- 默认登记模板为 `小花小花这是 {角色名}`，查看模板为 `小花小花我要看{角色名}`。触发模板、成功/覆盖/查看/错误回复通过 WebUI 配置并热更新。
- 每个角色固定只有一张图片。登记优先保存本条消息的第一张附图；没有附图时再读取引用消息图片。一条消息有多个图片段时只取第一张，其余明确忽略；同名覆盖是整张替换。模板中的空格可省略，角色名按完整文本匹配。
- 提示词模板、绘图任务扩展和等级限额等细节后续再明确，不擅自加入付费请求或未要求的功能。

## 修改范围和实体目录

仅修改本插件仓库中的相关文件。遵循当前目录上级适用的 `AGENTS.md`，不要改主程序的 `src/`、`dashboard/`、根 `.gitignore` 或实际 Bot/模型配置来绕过插件问题。确实需要修改主程序时，先解释原因、影响范围与替代方案，再按用户授权处理。

插件仓库必须实体安装在**主程序目录（本仓库所在工作区的根目录）**的 `plugins/LittleFlowerDraw` 下。不要把插件放到主程序目录之外。不使用指向目录外的目录联接或符号链接，不维护两份相同 ID 的插件。

`plugin.py` 是入口，`_manifest.json` 是元信息，`create_plugin()` 返回插件实例。配置模型在插件源码中声明，`config.toml` 由 Runner 生成并保持忽略。模块增长后按职责拆分，不预建无用途的目录或抽象。

角色库模块职责：`settings.py` 声明 `config.toml` 中的启用状态和配置版本，`webui_settings.py` 管理 WebUI 设置（角色库和绘图触发词、角色回复、图片限制、绘图基础提示词、任务队列、权限、白名单、提示文案及后续模块），`drawing_api.py` 管理 OpenAI 兼容图片接口配置与多图请求，`drawing_tasks.py` 管理任务 ID、持久队列、状态、历史与清理，`command_registry.py` 维护聊天指令注册表，`command_guard.py` 处理白名单/权限/配额与退次数，`command_usage.py` 持久化每人每指令次数和幂等退款，`role_patterns.py` 处理角色模板与角色名，`drawing_patterns.py` 解析三类绘图触发词，`role_messages.py` 处理单张图片及引用，`role_store.py` 负责单图事务持久化与列表/统计/删除/重命名，`role_webui.py` 负责缩略图、详情预览和数据组装，`webui.json` 与 `webui/roles.html` 声明并实现「小花绘」统一入口。主入口只负责 SDK 生命周期、消息分发、WebUI API 与回复。

## 文档和示例阅读顺序

修改前先看清单、`plugin.py`、README 和相关接口。工作区中的 `PluginsDoc` 保存了官方文档快照，按需查阅：

| 内容 | 本地快照（相对工作区根目录） | 官网 |
| --- | --- | --- |
| 接入与边界 | `PluginsDoc/develop/plugin.md` | <https://docs.maibot.chat/develop/plugin> |
| AI 辅助开发 | `PluginsDoc/plugin/vibe-coding.md` | <https://docs.maibot.chat/plugin/vibe-coding> |
| 清单 | `PluginsDoc/plugin/manifest.md` | <https://docs.maibot.chat/plugin/manifest> |
| 生命周期 | `PluginsDoc/plugin/lifecycle.md` | <https://docs.maibot.chat/plugin/lifecycle> |
| 配置 | `PluginsDoc/plugin/config.md` | <https://docs.maibot.chat/plugin/config> |
| 命令与工具 | `PluginsDoc/plugin/commands.md`、`PluginsDoc/plugin/tools.md` | <https://docs.maibot.chat/plugin/commands> · <https://docs.maibot.chat/plugin/tools> |
| Hook 与事件 | `PluginsDoc/plugin/hooks.md`、`PluginsDoc/plugin/event-handlers.md` | <https://docs.maibot.chat/plugin/hooks> · <https://docs.maibot.chat/plugin/event-handlers> |
| 能力代理 | `PluginsDoc/plugin/api-reference.md`、`PluginsDoc/plugin/api-components.md` | <https://docs.maibot.chat/plugin/api-reference> · <https://docs.maibot.chat/plugin/api-components> |
| WebUI 页面 | `PluginsDoc/plugin/webui-pages.md` | <https://docs.maibot.chat/plugin/webui-pages> |
| 发布 | `PluginsDoc/plugin/submission.md` | <https://docs.maibot.chat/plugin/submission> |

`PluginsDoc/README.md` 是完整文档索引，消息网关、插件 API、模型提供商、回复扩展、HomeCard 与 WebUI 页面等按需从这里进入。

本地主程序的 `plugins/hello_world_plugin` 可用于核对配置分组、`__ui_label__` / `__ui_icon__` / `__ui_order__`、生命周期、命令注册，以及未来需要时的 HomeCard、API 和 WebUI 页面写法。它是参考模板，不复制无关命令、演示页面、宽泛版本区间或能力列表。

文档和模板可能与已安装 SDK 有版本差异，以当前宿主与 SDK 的校验和实测为依据。当前已核对的环境是 MaiBot `1.4.3`、SDK `2.22.1`；不要照抄文档示例中的旧版本上限，也不要用强制兼容掩盖真实问题。

## SDK、组件和清单

- 业务代码只依赖标准库、清单声明的第三方包和 `maibot_sdk`，不依赖主程序的 `src.*` 内部实现。
- 插件继承 `MaiBotPlugin`，实现 `on_load()`、`on_unload()`、`on_config_update()` 与顶层 `create_plugin()`。
- 本插件不使用 `@Command`，聊天入口统一走关键词 Hook；供模型主动调用的能力用 `@Tool`，流程干预、事件观察按需使用 `@HookHandler`、`@EventHandler`。新增聊天指令时必须先在 `command_registry.py` 登记，并接入同一个权限/配额守卫。
- 用 `self.ctx` 访问能力；`capabilities` 精确声明实际调用，目前包括 `send.text`、`send.image`、`chat.open_session`、`message.get_by_id`、`api.get`、`api.call`、`webui.claim_upload`。
- 图库关键词通过 `chat.receive.before_process` 阻塞 Hook 识别，匹配后交给有界后台队列并中止主聊天处理，避免图片预处理后丢失内容及模型重复回复。首条聊天需先通过 `chat.open_session` 建立真实聊天流。
- 引用优先查宿主历史（包含二进制图片）；旧消息缺失时使用官方 SnowLuma 公开消息/图片查询 API。不能只把引用文字预览当成图片，也不能依赖适配器内部模块。
- Python 包依赖写进清单 `dependencies`，不为插件修改宿主依赖文件，也不依赖未声明的系统工具。
- 保持 manifest v2 的必填字段、有效 URL、严格三段式版本和真实兼容区间，不填虚构的宽泛上限。
- 延续现有类型注解和导入顺序；同目录模块使用相对导入。可确认类型时直接访问属性，不添加没有依据的 `getattr`、兜底或静默异常处理。

## 配置、消息与运行数据

- 配置使用 `PluginConfigBase` 与 `Field`，通过 `config_model` 绑定，默认禁用。
- 保留 `[plugin]` 分组的 `enabled`、`config_version`；当前配置结构版本为 `0.6.0`，配置结构变化时才调整版本并验证旧配置补齐。
- `config.toml` 只保留启用状态和配置版本；角色库/绘图触发词、角色回复、图片限制、绘图基础提示词、任务队列、权限、白名单、提示文案都由 `webui_settings.py` 管理，保存在 `webui_settings.json`，通过「小花绘 → 插件设置」修改，不写回 `config.toml`。
- 优先用 `self.config` 读取强类型配置，配置热更新放在 `on_config_update()`，不手写或提交用户的 `config.toml`。
- 用户可见文本使用简体中文，展示名称统一为「小花绘」。聊天入口不再提供 `/小花绘` 等状态命令；调整触发模板时说明对现有用户的影响。
- 图库触发模板使用 `{角色名}`；角色回复模板只支持 `{角色名}`；绘图触发模板支持 `{角色名}`、`{角色名1}`、`{角色名2}`、`{角色名列表}` 和可选的 `{额外提示词}`；权限提示模板只支持 `{指令}`、`{重置时间}`，均拒绝未知变量和格式化指令。每次请求采用接收时的配置快照，新请求立即使用热更新后的配置。
- 白名单、权限和次数都通过 `CommandGuard` 统一检查：管理员绕过白名单与限额；白名单空列表在开启时表示该类型全部不可用；次数按每人每指令跨群统计。角色库指令成功才计数；绘图任务在入队时计数，任务失败或被取消时按任务 ID 幂等退还，到期日 0 点重置。这些设置和提示文案由「小花绘 → 插件设置」维护，存在 `webui_settings.json`，不写回 `config.toml`。
- 使用真实 `stream_id` 发送消息，不自行计算会话 ID；回复路由按发起聊天隔离，但角色数据全局共享。
- 检查发送与能力调用的返回结果，向用户给出可读错误，并记录便于排查的日志，不把失败伪装成成功。
- 网络请求设置超时并处理失败。绘图、批量文件操作和其他耗时同步工作不得阻塞事件循环；后台任务、连接和句柄在卸载时清理。
- 持久数据放在 `self.ctx.paths.data_dir`，临时图片与缓存放在 `runtime_dir`；固定素材放在 `assets/`。角色名、创建者、唯一图片和时间戳存在 `roles.sqlite3`，同名覆盖在同一事务内完成，失败不能破坏旧图。旧版多图库在初始化时迁移为第一张图片。次数窗口存在 `command_usage.sqlite3`，WebUI 设置存在 `webui_settings.json`，绘图 API 配置存在 `drawing_api.json`，任务队列与历史存在 `drawing_tasks.sqlite3`，任务参考图存在 `drawing_tasks/任务ID/`；生成图片按日期保存到插件目录的 `drawings/`，该目录已忽略。
- 单张图片需校验格式、字节和像素限制；保存实际收到的图片，不承诺绕过主程序图片压缩。角色名只作为数据库键，不作为文件路径。队列和后台任务必须有界，并在卸载时清理。
- 使用受控文件名并检查输出路径边界，不将用户输入直接当路径。不硬编码密钥、个人 QQ 号、群号或机器绝对路径。
- 不提交本地配置、日志、数据库、临时脚本、虚拟环境或凭据；保留当前自用许可，未经用户要求不变更许可。

## 验证要求

按实际改动选择有意义的检查，避免为纯文案修改增加机械重复的测试。加载或目录问题必须验证运行中的主程序目录与 WebUI 插件索引，不能仅凭 Python 导入成功声称 WebUI 已显示。

1. 检查清单 JSON、Python 语法与 `git diff --check`。
2. 修改清单、组件或配置时，使用宿主 Python 环境验证实际 ManifestValidator、SDK 组件收集和配置 Schema。
3. 路径或安装方式改变时，确认插件实体目录处于主程序目录的 `plugins` 内，且目录索引可读取；在运行中的 WebUI 插件列表核对名称与状态。
4. 行为改变时，验证相关命令匹配、成功/失败返回、配置更新与资源清理。角色库必须验证跨群共享、同名覆盖、附图/引用、单图截取、旧库迁移、重启持久化及失败事务；权限与配额必须验证管理员绕过、群聊/私聊白名单、成功计数、失败不计数、跨群共享和 0 点重置。绘图指令必须验证三类模板、参考图优先取附图/引用、角色图顺序、额外提示词可选、角色不存在、缺参考图、多人数量限制、API 未配置/调用失败和成功回图；任务必须验证 ID 从 0 递增、一人一任务、队列容量、失败/取消退次数、重启 running 标记失败、历史重试/删除和并发配置。真实 API 调用只在用户配置后手动验证，单元测试必须 mock 网络。
5. WebUI 改动必须用宿主 `WebUIExtension` 校验 `webui.json` 与 HTML，重载插件后确认「小花绘」入口出现在插件扩展中，并验证角色库、插件设置、当前任务、任务历史、模块切换、设置保存、任务取消/重试/批量删除、上传替换、改名、删除和错误提示。查询结果不得超过 512 KiB，图片只能以压缩预览返回。
6. 模拟 RPC 验证不等于 QQ 实测。发送测试群消息须遵循当前会话授权；报告实际完成的检查以及尚未验证的部分。

Windows 沙箱可能限制 `asyncio` 内部本地套接字。遇到这种测试环境问题应定位并说明，不修改插件业务逻辑来掩盖。

## 开发与同步

完整流程见 README 的「日常开发流程」和「更新到另一台运行机器」。开始前检查工作区，不覆盖用户已有改动；只整理本次涉及的代码，不重构整个宿主。

开发和运行使用同一份主程序目录源码。按当前会话授权提交、同步仓库；提交前检查暂存差异，仅纳入相关文件。拉取使用 `git pull --ff-only`，推送使用 `git push origin main`，不默认强制推送、重写历史、创建 Release 或发布插件市场。

Git 提交消息使用简体中文，简洁说明本次改动的内容和目的；代码标识符、插件名和必要的技术术语可保留原文。

对于文案、注释、开发约定等小修改，可以自行决定立即提交或同步，也可以保留本地改动，等下一次相关修改一起处理；本地提交与远程推送可以分别安排。如果提交时机有取舍或用户偏好不明确，也可以询问用户，不必每次小改都单独提交和推送。用户明确要求立即提交或同步时，按要求执行。完成时简要说明本次是否已提交、是否已同步，以及仍待处理的改动。

完成后说明改动、检查结果、运行状态和需要用户执行的下一步，区分「已被索引」「已启用」「已通过 QQ 实测」。
