# 小花绘开发约定

## 项目信息与当前范围

- 展示名称：小花绘；仓库与目录名：`LittleFlowerDraw`。
- 说明：让小花帮你画。
- 用户称呼的版本为 `0.1`，清单使用框架要求的三段式版本 `0.1.0`。
- 稳定插件 ID：`suitun.little-flower-draw`，不要因展示名称变化而改 ID。
- 仓库：<https://github.com/suitun/LittleFlowerDraw>，主要维护 `main` 分支。
- 当前面向真实 QQ + SnowLuma 开发。离线单元测试可以使用测试事件与 SDK 替身，但不能宣称完成真实 QQ 联调。
- 现阶段实现角色图片登记、同名覆盖和按名查看；绘图替换、等级、限额等后续功能不在此次角色库实现范围内。
- 角色库在当前机器人内全局共享：所有群和用户使用同一份角色记录，任何人都能登记、查看或覆盖同名角色。不要按群或用户拆库，也不要擅自增加覆盖权限限制。
- 默认登记模板为 `小花小花这是 {角色名}`，查看模板为 `小花小花我要看{角色名}`。触发模板、成功/覆盖/查看/错误回复通过 WebUI 配置并热更新。
- 登记优先保存本条消息附带的所有图片；没有附图时再读取引用消息图片。同名覆盖完整替换该角色的图片组。模板中的空格可省略，角色名按完整文本匹配。
- 画图服务、模型、互动规则与等级限额细节后续再明确，不擅自加入付费请求或未要求的功能。

## 修改范围和实体目录

仅修改本插件仓库中的相关文件。遵循当前目录上级适用的 `AGENTS.md`，不要改主程序的 `src/`、`dashboard/`、根 `.gitignore` 或实际 Bot/模型配置来绕过插件问题。确实需要修改主程序时，先解释原因、影响范围与替代方案，再按用户授权处理。

插件仓库必须实体安装在**主程序目录（本仓库所在工作区的根目录）**的 `plugins/LittleFlowerDraw` 下。不要把插件放到主程序目录之外。不使用指向目录外的目录联接或符号链接，不维护两份相同 ID 的插件。

`plugin.py` 是入口，`_manifest.json` 是元信息，`create_plugin()` 返回插件实例。配置模型在插件源码中声明，`config.toml` 由 Runner 生成并保持忽略。模块增长后按职责拆分，不预建无用途的目录或抽象。

角色库模块职责：`settings.py` 声明 WebUI 配置，`role_patterns.py` 处理模板与角色名，`role_messages.py` 处理图片及引用，`role_store.py` 负责事务持久化。主入口只负责 SDK 生命周期、消息分发与回复。

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
| 能力代理 | `PluginsDoc/plugin/api-reference.md` | <https://docs.maibot.chat/plugin/api-reference> |
| 发布 | `PluginsDoc/plugin/submission.md` | <https://docs.maibot.chat/plugin/submission> |

`PluginsDoc/README.md` 是完整文档索引，消息网关、插件 API、模型提供商、回复扩展、HomeCard 与 WebUI 页面等按需从这里进入。

本地主程序的 `plugins/hello_world_plugin` 可用于核对配置分组、`__ui_label__` / `__ui_icon__` / `__ui_order__`、生命周期、命令注册，以及未来需要时的 HomeCard、API 和 WebUI 页面写法。它是参考模板，不复制无关命令、演示页面、宽泛版本区间或能力列表。

文档和模板可能与已安装 SDK 有版本差异，以当前宿主与 SDK 的校验和实测为依据。当前已核对的环境是 MaiBot `1.4.3`、SDK `2.22.1`；不要照抄文档示例中的旧版本上限，也不要用强制兼容掩盖真实问题。

## SDK、组件和清单

- 业务代码只依赖标准库、清单声明的第三方包和 `maibot_sdk`，不依赖主程序的 `src.*` 内部实现。
- 插件继承 `MaiBotPlugin`，实现 `on_load()`、`on_unload()`、`on_config_update()` 与顶层 `create_plugin()`。
- 用户命令用 `@Command`；供模型主动调用的能力用 `@Tool`；流程干预、事件观察分别按需使用 `@HookHandler`、`@EventHandler`。新代码不使用旧 `@Action`。
- 用 `self.ctx` 访问能力；`capabilities` 精确声明实际调用，目前包括 `send.text`、`send.image`、`chat.open_session`、`message.get_by_id`、`api.get`、`api.call`。
- 图库关键词通过 `chat.receive.before_process` 阻塞 Hook 识别，匹配后交给有界后台队列并中止主聊天处理，避免图片预处理后丢失内容及模型重复回复。首条聊天需先通过 `chat.open_session` 建立真实聊天流。
- 引用优先查宿主历史（包含二进制图片）；旧消息缺失时使用官方 SnowLuma 公开消息/图片查询 API。不能只把引用文字预览当成图片，也不能依赖适配器内部模块。
- Python 包依赖写进清单 `dependencies`，不为插件修改宿主依赖文件，也不依赖未声明的系统工具。
- 保持 manifest v2 的必填字段、有效 URL、严格三段式版本和真实兼容区间，不填虚构的宽泛上限。
- 延续现有类型注解和导入顺序；同目录模块使用相对导入。可确认类型时直接访问属性，不添加没有依据的 `getattr`、兜底或静默异常处理。

## 配置、消息与运行数据

- 配置使用 `PluginConfigBase` 与 `Field`，通过 `config_model` 绑定，默认禁用。
- 保留 `[plugin]` 分组的 `enabled`、`config_version`；配置结构变化时才调整配置版本并验证旧配置补齐。
- 优先用 `self.config` 读取强类型配置，配置热更新放在 `on_config_update()`，不手写或提交用户的 `config.toml`。
- 用户可见文本使用简体中文，展示名称统一为「小花绘」。保留 `/小花绘`、`/小花画图`、`/lfdraw` 的现有状态入口，调整命令时明确兼容影响。
- 图库触发模板使用 `{角色名}`；回复模板只支持 `{角色名}`、`{图片数量}`，拒绝未知变量和格式化指令。每次请求采用接收时的配置快照，新请求立即使用热更新后的配置。
- 使用真实 `stream_id` 发送消息，不自行计算会话 ID；回复路由按发起聊天隔离，但角色数据全局共享。
- 检查发送与能力调用的返回结果，向用户给出可读错误，并记录便于排查的日志，不把失败伪装成成功。
- 网络请求设置超时并处理失败。绘图、批量文件操作和其他耗时同步工作不得阻塞事件循环；后台任务、连接和句柄在卸载时清理。
- 持久数据放在 `self.ctx.paths.data_dir`，临时图片与缓存放在 `runtime_dir`；固定素材放在 `assets/`。角色元信息和图片 BLOB 存在 `roles.sqlite3`，同名覆盖在同一事务内完成，失败不能破坏旧图。
- 图片需校验格式、数量、字节和像素限制；保存实际收到的图片，不承诺绕过主程序图片压缩。角色名只作为数据库键，不作为文件路径。队列和后台任务必须有界，并在卸载时清理。
- 使用受控文件名并检查输出路径边界，不将用户输入直接当路径。不硬编码密钥、个人 QQ 号、群号或机器绝对路径。
- 不提交本地配置、日志、数据库、临时脚本、虚拟环境或凭据；保留当前自用许可，未经用户要求不变更许可。

## 验证要求

按实际改动选择有意义的检查，避免为纯文案修改增加机械重复的测试。加载或目录问题必须验证运行中的主程序目录与 WebUI 插件索引，不能仅凭 Python 导入成功声称 WebUI 已显示。

1. 检查清单 JSON、Python 语法与 `git diff --check`。
2. 修改清单、组件或配置时，使用宿主 Python 环境验证实际 ManifestValidator、SDK 组件收集和配置 Schema。
3. 路径或安装方式改变时，确认插件实体目录处于主程序目录的 `plugins` 内，且目录索引可读取；在运行中的 WebUI 插件列表核对名称与状态。
4. 行为改变时，验证相关命令匹配、成功/失败返回、配置更新与资源清理。角色库必须验证跨群共享、同名覆盖、附图/引用、多图、重启持久化及失败事务；未来绘图任务的回复路由仍需验证不会串群。
5. 模拟 RPC 验证不等于 QQ 实测。发送测试群消息须遵循当前会话授权；报告实际完成的检查以及尚未验证的部分。

Windows 沙箱可能限制 `asyncio` 内部本地套接字。遇到这种测试环境问题应定位并说明，不修改插件业务逻辑来掩盖。

## 开发与同步

完整流程见 README 的「日常开发流程」和「更新到另一台运行机器」。开始前检查工作区，不覆盖用户已有改动；只整理本次涉及的代码，不重构整个宿主。

开发和运行使用同一份主程序目录源码。按当前会话授权提交、同步仓库；提交前检查暂存差异，仅纳入相关文件。拉取使用 `git pull --ff-only`，推送使用 `git push origin main`，不默认强制推送、重写历史、创建 Release 或发布插件市场。

Git 提交消息使用简体中文，简洁说明本次改动的内容和目的；代码标识符、插件名和必要的技术术语可保留原文。

对于文案、注释、开发约定等小修改，可以自行决定立即提交或同步，也可以保留本地改动，等下一次相关修改一起处理；本地提交与远程推送可以分别安排。如果提交时机有取舍或用户偏好不明确，也可以询问用户，不必每次小改都单独提交和推送。用户明确要求立即提交或同步时，按要求执行。完成时简要说明本次是否已提交、是否已同步，以及仍待处理的改动。

完成后说明改动、检查结果、运行状态和需要用户执行的下一步，区分「已被索引」「已启用」「已通过 QQ 实测」。
