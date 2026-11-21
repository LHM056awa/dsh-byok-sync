# Changelog

本文件记录 `dsh-byok-sync` 的所有显著变更，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## 0.1.0 (2026-11-19)

### Added

- 初始版本：同步 DeepSeek Harness（DSH）第三方端点（`llm-pi-ai` 路由）的模型列表，
  数据来自各端点的模型列表接口（`GET {baseURL}/models` 等）。
- **两种目标文件形态自动判定**：profile 的 loader patch（`%DSH_PROFILE_DIR%\cordis.patch.yml`，
  根为列表，定位 `- id: llm-pi-ai` 行）与传统 `settings.yaml`（根为映射，定位顶层 `llm-pi-ai:`）；
  不传 `--config` 时按 `DSH_PROFILE_DIR` → `~/.dsh/profiles/<profile>/cordis.patch.yml` →
  `~/.dsh/settings.yaml` 顺序探测。配合 `patchReload: live`，写入后无需重启 DSH。
- **三种协议的端点发现**：`openai-completions` / `openai-responses`（`{baseURL}/models` + bearer）、
  `anthropic-messages`（`/v1/models?limit=1000` + `x-api-key` + `anthropic-version`）、
  `google-generative-ai`（`{baseURL}/models?pageSize=1000` + `x-goog-api-key`），
  响应统一归一 `id` / `name` / `contextWindow` / `maxTokens`。
- **URL 规范化**：`https://api.deepseek.com`、带 `/v1`、`/v1/chat/completions`、`/v1/responses`、
  带 query string、带尾斜杠等写法一律归一到列表 URL。
- **API key 自动解析**：`apiKeyEnv` → 进程环境变量 → `~/.dsh/.credentials.yaml` 的 `refs.<NAME>` →
  `records["llm-pi-ai/<route>"]` 的 `apiKey`/`key`/`token`/`secret`；解析不到时降级为无认证请求。
  密钥只存在于进程内存，不打印、不落盘。
- **新增模型字段**：`id`/`name`/`contextWindow`/`maxTokens`/`input`/`reasoningEfforts`，
  取值优先级为「远端 > 预设 > 省略」；省略的 `contextWindow`/`maxTokens` 交由路由
  `defaultContextWindow`（默认 1,000,000）与 `defaultMaxTokens`（默认 128,000）兜底。
- **内置模型预设** `src/dsh_byok_sync/presets.json`：DeepSeek 预设（id 含 `deepseek`，大小写不敏感）
  设置 1M 输入 token、384K 输出 token 与 `off`/`low`/`medium`/`high`/`max` 档期 reasoning effort；
  预设只作用于新增模型，多个命中取第一个，字段名与取值严格校验，损坏时报 `ModelSyncError` 而非静默降级。
- **非文本模型过滤**：按 `id`/`name` 的子串、独立 token 与中文词根匹配（`video`/`audio`/`whisper`/`tts`/
  `voice`/`dall`/`sora`/`imagen`/`embed`/`diffusion`/`图像`/`语音` 等），命中即不新增入库并在报告中列出；
  `vision` 不在过滤范围内，`name` 含「理解」时中文词根豁免。
- **删除保护**：仅当该路由请求成功、且远端返回至少一个有效**文本**模型 id 时才删除本地已下线模型
  （端点级文本信号授权）；无文本信号、请求失败、缺 `baseURL` 时一律保留并报告。
- **顺序语义**：默认保留手排顺序、新增追加末尾、无变更不写盘；`--sort` 按 id 字典序（大小写敏感）重排。
- **无效条目清理**：缺少非空字符串 `id`（或条目不是映射）在任何模式下都被丢弃并报告
  `discarded invalid entries (...)`；缺 `baseURL` 的不可同步路由同样会做这项清理。
- **`models` 形态前置校验**：非空流风格列表 `models: [a, b]`、以及 `models` 的非列表值
  （块状映射/块标量/标量）在加载阶段即被拒绝——该路由只报 `error`、完全不改写文件（退出码 1）；
  `models` 缺失、`models:` 空值、`models: []` 会自动补成块风格列表。
- **no-op 不触碰文件**：plan 与文件现状一致时根本不调用改写器，文件逐字节不变，
  报告为 `[unchanged]` + `kept N models`。
- **余额查询**：对已知厂商域名（`api.deepseek.com`、`openrouter.ai`、`api.moonshot.cn`、
  `api.moonshot.ai`、`api.stepfun.com`、`api.novita.ai`）自动查询余额并追加 `credits:` 行，
  失败时显示 `unavailable`；`--no-credits` 可跳过。
- **报告自动着色**：stdout 为交互式 TTY 时上色，`NO_COLOR` 或 `--no-color` 关闭，
  stdout 非 TTY 时输出纯文本并把完整报告镜像到 stderr。
- **报告结构**：标题行 `[changes]` / `[unchanged]` / `[error]` 三种，`Summary` 中
  `changed` / `unchanged` / `failed` 分开计数，末尾固定输出 `config: <实际读写的文件绝对路径>`
  以便在自动探测路径时确认改的是哪个文件。退出码：全部成功 `0`、任一路由 `error` 为 `1`、
  参数或配置错误（含格式被拒）为 `2`。
- **只改 `models`**：provider 其他字段与其他插件行保持字节不变，既有模型条目连同注释/空行/缩进原样复用，
  新增条目按固定字段顺序渲染，写盘采用同目录临时文件 + `os.replace` 的原子替换。
- **路径探测**：`DSH_HOME` / `DSH_PROFILE_HOME` 可覆盖 DSH 主目录；profile 目录不存在时继续探测
  `~/.dsh/settings.yaml`，全部候选都不存在才报错并列出尝试过的路径。
- 命令行参数：`--config`、`--profile`、`--entry`、`--all`、`--provider`、`--dry-run`、`--no-delete`、
  `--sort`、`--no-credits`、`--timeout`、`--credentials`、`--list-routes`、`--no-color`、`--version`。
- 工作区配套：`.vscode/tasks.json` 提供 `Sync DSH Models` / `Sync DSH Models (Console)` 及两者的
  `--dry-run` 变体；`sync-dsh-models.bat` 支持双击运行，**跑一次即直接写盘**（无预览、无 Y/N 确认），
  窗口 `pause` 保留报告。
- 仅依赖 Python 标准库（>= 3.10）；测试使用 `unittest`，不访问真实网络、不读取真实凭据。

### Fixed

- **传统 cmd / conhost 窗口不再显示裸 ANSI 转义**（此前会看到 `[32m[changes] ... [0m`）：
  新增 `cli.supports_ansi()`，**逐行移植 `copilot-byok-sync` 的 `_windows_vt100_ready()`**——
  用 `GetStdHandle(STD_OUTPUT_HANDLE)` + `GetConsoleMode`/`SetConsoleMode` 为当前控制台打开
  `ENABLE_VIRTUAL_TERMINAL_PROCESSING`，并且**必须显式声明 `argtypes`/`restype`**（漏掉时 64 位
  handle 会被截断成 `int`，`GetConsoleMode` 失败，探针就会错误地"信任"一个其实不能渲染转义的控制台）。
  **关键修复**：`main()` 的着色判定改为调用 `color_enabled()`——之前把判定内联成
  `tty and not --no-color`，导致探针在真实运行路径上从未被调用（只有测试/探针脚本调用过），
  VT 因此始终没打开。新增回归测试 `MainColourWiringTest` 锁住"main 必须询问控制台"。
  真实 conhost 验证：清空 `NO_COLOR` 后跑真实 CLI，屏幕缓冲区里**没有字面 ESC**，
  `[changes]` 行属性为绿色（`FOREGROUND_GREEN`）——即颜色确实被渲染；老系统或探测失败时退回纯文本。
- **`sync-dsh-models.bat` 的提示行被劈开**（症状：`'Y = ...' is not recognized as an internal or
  external command, operable program or batch file.`）：根因是 UTF-8 批处理里 `chcp 65001` 导致 cmd.exe
  的字节偏移错位。现在批处理改为**纯 ASCII 英文提示且不修改代码页**（中文报告由 Python 以 Unicode
  直接写控制台），并保持 CRLF 行尾。
- **着色粒度改为"只给标签上色"**（与 `copilot-byok-sync` 一致）：之前整行染色，几百个模型 id
  的长列表全是红/绿色；现在只有 `[changes]`/`[unchanged]`/`[error]`、路由名（加粗）、`added`/`removed`/
  `kept`/`filtered non-text`/`no text signal`/`deletion skipped`/`discarded invalid entries`/`credits:`/
  `error:`/`Summary:` 这些标签带颜色，id、数量、错误消息、路径一律保持终端默认色。
  实现改为 segment 列表（`cli.report_segments()`），`render*(..., color=...)` 只包裹标签片段。
- **灰色按终端自动选写法**（此前一律用 `\x1b[2;90m`，在 Windows Terminal 里比参考项目深）：
  `cli.grey_code()` 在 faint 可用的终端（`WT_SESSION` / `TERM_PROGRAM` / `COLORTERM` / `ANSICON` /
  `ConEmuANSI` / `TERM` 任一存在）返回 **`\x1b[2m`**，与 `copilot-byok-sync` 逐字节一致；
  传统 conhost 实测**忽略 faint**（单元格属性仍是默认 `0x07`），那里返回 **`\x1b[2;90m`**，
  渲染为 `0x08` 灰。两种分支都有测试（`GreyCodeTest`）。
- `test_report_shows_no_text_signal` 一类报告断言改为容忍详情行的两空格缩进。
