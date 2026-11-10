# 小花绘（LittleFlowerDraw）

让小花帮你画

版本：**0.1**。MaiBot 清单要求三段式版本号，因此 `_manifest.json` 中使用 `0.1.0`。

当前是个人自用的 QQ 群互动绘画插件开发骨架，已实现基础配置和状态检查命令，具体绘画与互动规则待开发。

## 环境与安装位置

- 当前验证环境：MaiBot `1.4.3`、`maibot-plugin-sdk` `2.22.1`。
- 清单兼容范围：宿主 `1.4.3` 至 `1.4.99`，SDK `2.22.1` 至 `2.22.99`。
- 使用 MaiBot 自身的 Python 环境，目前没有额外 Python 包依赖。
- 群聊联调需要正常工作的 QQ 适配器和已放行的测试群。

插件必须放进**正在运行的机器人实例**的 `plugins` 目录。多实例宿主的源码根目录不等于机器人实例目录：

```text
MaiBot 工作区/
├── host/                         # 宿主与机器人注册表
├── bots/
│   └── <机器人 ID>/
│       ├── config/
│       ├── data/
│       ├── plugins/
│       │   └── LittleFlowerDraw/  # 本插件的实体 Git 仓库
│       └── temp/
└── plugins/                      # 不作为上述机器人的插件安装位置
```

旧式单实例部署直接使用该实例根目录下的 `plugins/LittleFlowerDraw`。本项目使用实体目录，不使用目录联接或符号链接；WebUI 插件索引会拒绝解析到插件根目录之外的目录。

首次安装时，在目标机器人实例目录执行：

```powershell
git clone https://github.com/suitun/LittleFlowerDraw.git plugins/LittleFlowerDraw
```

已经存在本仓库时直接在当前目录开发，不再克隆第二份。仓库结构：

```text
LittleFlowerDraw/
├── _manifest.json     # 名称、版本、依赖、能力与兼容范围
├── plugin.py          # 入口、配置模型、生命周期与命令
├── AGENTS.md          # AI 协作开发约定
├── README.md
├── LICENSE
├── .gitignore
├── .gitattributes
└── assets/            # 随源码维护的固定素材
```

## 首次启用与验证

1. 打开 WebUI，选中正确的机器人实例，进入插件管理并刷新页面，查找「小花绘」。
2. 插件默认禁用，在插件管理中启用。Runner 根据配置模型生成本地 `config.toml`。
3. 在测试群发送 `/小花绘`，应收到版本、说明及当前功能状态。
4. 若新增目录未被运行中的实例发现，先查看该实例日志；必要时通过 WebUI 重启该机器人实例后再检查。

| 命令 | 作用 |
| --- | --- |
| `/小花绘` | 查看插件状态 |
| `/小花画图` | 兼容原状态命令 |
| `/lfdraw` | 英文别名 |

## 日常开发流程

1. 在**实例的 `plugins/LittleFlowerDraw` 仓库内**工作。先读 `AGENTS.md`、清单、配置模型和相关接口文档。
2. 查看工作区，已有改动先确认归属。工作区干净且需要获取远程更新时使用快进拉取：

   ```powershell
   git status --short --branch
   git pull --ff-only
   ```

3. 按需求修改插件源码。配置项在 `config_model` 中声明；依赖和宿主能力在清单中声明。
4. 做基础检查，再用宿主 SDK 验证插件导入、组件注册和配置 Schema。下面两条 Python 命令只检查 JSON 和语法，不代表插件已经在 MaiBot 中加载成功：

   ```powershell
   python -m json.tool _manifest.json
   python -m py_compile plugin.py
   git diff --check
   ```

5. 在 WebUI 查看实际加载状态和日志。运行时支持源码与配置热重载；需要时手动重载插件。验证此次修改涉及的命令或绘画交互，并确认没有影响其他群。
6. 检查差异，仅提交本次相关文件，再同步仓库。按实际改动调整下面 `git add` 的文件列表：

   ```powershell
   git diff
   git add -- plugin.py _manifest.json README.md AGENTS.md
   git diff --cached
   git commit -m "描述本次改动"
   git push origin main
   ```

本机在这个目录改代码，就是在修改运行实例读取的代码，不需要再次复制或从 GitHub 下载安装。

## 更新到另一台运行机器

1. 开发机完成检查、提交并推送。
2. 在运行机器对应的机器人实例插件目录执行 `git status --short --branch`。有本地源码改动时先处理，保留本地配置和数据。
3. 工作区干净后执行 `git pull --ff-only`，再在 WebUI 确认插件重新加载；必要时手动重载。
4. 检查配置能打开、命令能触发，以及本次更新涉及的功能和日志。

自用的 `main` 分支更新不需要创建 GitHub Release 或提交插件市场。`_manifest.json` 中的 `version` 在发布新插件版本时更新；`plugin.config_version` 只在配置结构发生变化时更新，两者不要求每次一起改。

如果需要撤销已经推送的某次改动，使用 `git revert <提交号>` 创建撤销提交，验证后再推送、重载。

## 配置、数据与权限

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `false` | 是否启用小花绘 |
| `plugin.config_version` | `0.1.0` | 配置结构版本 |

`config.toml` 由 Runner 生成，用户通过 WebUI 修改，不提交到 Git。当前仅申请 `send.text` 能力，用于回复状态命令。

- `assets/`：需要随代码版本维护的固定素材。
- `self.ctx.paths.data_dir`：需要跨重启保留的设置、绘画记录或状态。
- `self.ctx.paths.runtime_dir`：临时图片、下载缓存和可重建文件。

本地配置、环境变量文件、日志、数据库和 Python 缓存均由本仓库 `.gitignore` 排除。

## 排查未显示或未加载

按以下顺序检查：

1. WebUI 是否选中了插件实际安装的机器人实例。
2. 该实例的 `plugins/LittleFlowerDraw` 是否为实体目录，且清单和 `plugin.py` 直接位于其中。
3. 名称是否为「小花绘」、ID 是否为 `suitun.little-flower-draw`，清单版本是否使用 `0.1.0`。
4. 清单兼容范围、依赖与 `capabilities` 是否通过实际环境校验。
5. 插件是否只是处于禁用状态，或重载失败后回滚；以实例日志和 WebUI 实际状态为准。

## 开发参考

- [插件开发文档](https://docs.maibot.chat/plugin/)
- [AI 辅助开发指南](https://docs.maibot.chat/plugin/vibe-coding)
- 本地主程序的 `plugins/hello_world_plugin`：参考配置分组、生命周期、命令和按需扩展的 WebUI 示例；不要整套复制演示功能和能力声明。

## 许可

保留所有权利，见 [LICENSE](LICENSE)。
