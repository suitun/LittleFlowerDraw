# LittleFlowerDraw

用于 MaiBot 的 QQ 群互动绘画插件，按个人自用项目维护。

当前版本为 `0.1.0`，已搭好插件加载、声明式配置和状态检查命令。具体绘画与群互动功能尚未实现。

## 运行环境

- MaiBot：`1.4.3`，清单兼容范围为 `1.4.3` 至 `1.4.99`。
- `maibot-plugin-sdk`：`2.22.1`，清单兼容范围为 `2.22.1` 至 `2.22.99`。
- Python：使用 MaiBot 自身的 Python 环境。
- 已配置并连通的 QQ 适配器。

插件使用宿主提供的 SDK，目前没有额外 Python 包依赖。

## 安装与启用

在 MaiBot 主程序目录执行：

```powershell
git clone https://github.com/suitun/LittleFlowerDraw.git plugins/LittleFlowerDraw
```

启动 MaiBot，在 WebUI 插件管理中启用 LittleFlowerDraw。Runner 根据 `plugin.py` 中的配置模型生成插件目录下的 `config.toml`；插件默认禁用。

在配置中确认：

```toml
[plugin]
enabled = true
config_version = "0.1.0"
```

## 本地开发目录

推荐把插件作为独立 Git 仓库维护，并通过 Windows 目录联接接入本地 MaiBot：

```text
工作目录/
├── LittleFlowerDraw/         # 本仓库
│   ├── _manifest.json
│   ├── plugin.py
│   ├── README.md
│   ├── LICENSE
│   ├── .gitignore
│   ├── .gitattributes
│   └── assets/
└── MaiBot/                   # 主程序
    └── plugins/
        └── LittleFlowerDraw/ # 联接到本仓库
```

当 `MaiBot/plugins/LittleFlowerDraw` 尚不存在时，在上面的工作目录执行：

```powershell
New-Item -ItemType Junction -Path ".\MaiBot\plugins\LittleFlowerDraw" -Target (Resolve-Path ".\LittleFlowerDraw").Path
```

修改本仓库即修改 MaiBot 读取的插件源码。更改后可在 WebUI 重载插件；不要再创建第二份相同 ID 的插件。

`assets/` 保存需要随源码维护的固定素材。运行时持久数据通过 `self.ctx.paths.data_dir` 保存，临时图片和缓存通过 `self.ctx.paths.runtime_dir` 保存。

## 命令与验证

| 命令 | 作用 |
| --- | --- |
| `/小花画图` | 查看插件状态 |
| `/lfdraw` | 同上 |

首次验证：

1. 在 WebUI 确认插件已加载且已启用。
2. 在已放行的测试群发送 `/小花画图` 或 `/lfdraw`。
3. 应收到「LittleFlowerDraw 已就绪」及当前功能说明。

没有回复时，检查插件是否启用、QQ 适配器是否正常、测试群是否放行，以及命令权限和插件日志。加载失败时检查清单版本范围与当前宿主、SDK 版本是否一致。

## 配置与能力

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `false` | 是否启用插件 |
| `plugin.config_version` | `0.1.0` | 配置结构版本 |

当前仅申请 `send.text` 能力，用于回复状态命令。`config.toml`、环境变量文件、缓存和数据库不会提交到仓库。

## 开发约定

- 功能与配置优先在本插件目录内实现，通过 `maibot_sdk` 调用宿主能力。
- 后续功能增多后，再按需要拆分绘图服务、互动流程等模块。
- 开发接口参考 [MaiBot 插件文档](https://docs.maibot.chat/plugin/)。

## 许可

保留所有权利，见 [LICENSE](LICENSE)。
