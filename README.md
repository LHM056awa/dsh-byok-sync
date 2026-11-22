# dsh-byok-sync — DSH 第三方端点模型列表同步工具

同步 [DeepSeek Harness（DSH）](https://github.com/deepseek-ai) 里第三方端点（`llm-pi-ai` 插件路由）的模型列表：工具读取 DSH 配置中每个端点的 `baseURL` 与 `api` 协议，向该端点拉取 `/v1/models`（或对应协议的等价列表接口），再把结果合并回配置文件的 `models` 列表——新增远端发现的模型、删除远端已不再提供的模型、其余条目原样保留。

需要认证的端点会自动解析 DSH 凭据（`apiKeyEnv` → 进程环境变量 → `~/.dsh/.credentials.yaml` → DSH 凭据记录），按对应协议补上认证头。**密钥绝不被打印、记录或写入任何文件。**

与 `copilot-byok-sync`（同步 VS Code `chatLanguageModels.json`）同构，本项目把这套能力搬到 DSH 的 `cordis.patch.yml` / `settings.yaml`。

## 这是什么

* **目标**：DSH 配置里的 `llm-pi-ai` 插件条目——即"第三方 BYOK 端点"清单。每个端点是 `providers` 映射下的一个路由（路由名就是端点的名字，例如 `agnesai`、`iris`、`nvidia`），每个路由带 `baseURL`、`api`、可选的 `apiKeyEnv`、`headers`，以及一个 `models` 列表。
* **数据来源**：远端端点的模型列表接口（绝大多数是 `GET {baseURL}/models`）。
* **写入范围**：**只改 `models` 列表**。provider 的其他所有字段（`baseURL`、`apiKeyEnv`、`displayName`、`modelOverrides`、`compat`、`retryPolicy`、`defaultContextWindow` ……）以及配置文件里其他插件行一个字节都不动。

## 目标文件：两种形态

DSH 的第三方端点在当前版本里可能出现于两个位置，内容同构，本工具**靠解析后的根节点类型自动判定**，无需关心文件名：

### 1. profile 的 loader patch（当前形态，常见）

```
%DSH_PROFILE_DIR%\cordis.patch.yml
```

根节点是**列表**，每个元素是一个插件行：

```yaml
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      agnesai:
        displayName: AgnesAI
        apiKeyEnv: AGNESAI_API_KEY
        api: openai-completions
        baseURL: https://api.agnesai.example/v1
        models:
          - id: agnes-3.0-flash
            name: agnes-3.0-flash
            contextWindow: 512000
```

本工具定位 `id` 等于 `--entry`（默认 `llm-pi-ai`）的那一行，改写其 `config.providers.<route>.models`。

### 2. 传统 `settings.yaml`

```
~/.dsh/settings.yaml
```

根节点是**映射**，同样内容位于顶层键 `llm-pi-ai:` 之下：

```yaml
llm-pi-ai:
  providers:
    iris:
      apiKeyEnv: IRIS_API_KEY
      api: openai-completions
      baseURL: https://gateway.example.com/v1
      models:
        - id: deepseek-v4-flash
          name: deepseek-v4-flash
```

本工具定位顶层键等于 `--entry`（默认 `llm-pi-ai`）的映射，改写其 `providers.<route>.models`。

### 默认路径探测顺序

不传 `--config` 时按下列顺序找第一个存在的文件：

1. `%DSH_PROFILE_DIR%\cordis.patch.yml`；
2. `~/.dsh/profiles/<--profile 或 %DSH_PROFILE% 或 desktop>/cordis.patch.yml`；
3. `~/.dsh/settings.yaml`。

`--config PATH` 是**显式覆盖**，优先级高于上面全部（但它必须指向一个已存在的文件，否则直接报错）。

补充约定：

- DSH 主目录默认 `~/.dsh`，可用环境变量 `DSH_HOME`（或 `DSH_PROFILE_HOME`）整体改写；若显式设置了 `DSH_PROFILE_DIR`，它**直接**作为 profile 目录使用，优先级高于 `DSH_HOME`；
- 未设 `DSH_PROFILE_DIR` 时，profile 名字的优先级是 `--profile` > `%DSH_PROFILE%` > `desktop`，拼出 `~/.dsh/profiles/<名字>`；
- 若 profile 目录**不存在**（例如 DSH 从未启动过该 profile），工具**不会报错**，而是继续往后探测 `~/.dsh/settings.yaml`；只有所有候选都不存在时才报错，并在错误信息里列出尝试过的全部路径；
- 探测失败一律按配置错误处理（退出码 `2`），**不会新建文件**；目标文件不是 UTF-8 文本时同样报错。

### `patchReload: live`：改完即时生效

`profiles/<name>/package.json` 声明了 `patchReload: live` 时，DSH 会监视 loader patch 文件，同步写入后**无需重启 DSH**，新的模型列表立刻出现在模型选择器里。本工具以"同目录临时文件 + `os.replace`"的原子方式写盘，因此重载看到的一定是完整的新文件，不会是写到一半的半成品。

> 若你的 profile 没有开启 `patchReload: live`，改动会在 DSH 下次启动（或手动重载）时生效。

## 安装与使用

### 1. 安装

```bash
cd dsh-byok-sync
pip install -e .
```

要求 Python **>= 3.10**，且**只用标准库**（不依赖 PyYAML / requests / cryptography）。

不想安装也可以跑（把 `src` 加进 `PYTHONPATH`）：

```powershell
$env:PYTHONPATH = "$PWD\src"; python -m dsh_byok_sync --help
```

### 2. 使用方法

#### 同步所有路由（推荐）

```bash
python -m dsh_byok_sync --all
```

> 也可用安装后的 `dsh-byok-sync --all`，二者等价。默认按上文探测顺序定位配置文件。

#### 同步指定路由

```bash
python -m dsh_byok_sync --provider iris --provider nvidia
```

#### 先预览、不写盘

```bash
python -m dsh_byok_sync --all --dry-run
```

#### 仅列出现有路由（不联网）

```bash
python -m dsh_byok_sync --list-routes
```

### 3. 命令行参数详解

```
dsh-byok-sync --all [--config PATH] [--dry-run] [--no-delete] [--sort] [--no-credits] [--workers N]
dsh-byok-sync --provider ROUTE [--provider ROUTE ...] ...
```

| 参数 | 含义 |
| - | - |
| `--config PATH` | 目标配置文件；缺省按"默认路径探测顺序"自动判定 |
| `--profile NAME` | 指定 profile 名（用于探测 `~/.dsh/profiles/<NAME>/cordis.patch.yml`） |
| `--entry ID` | 插件行 id，默认 `llm-pi-ai` |
| `--all` | 同步**所有**路由（与 `--provider` 二选一，必须指定其一） |
| `--provider ROUTE` | 同步**指定单个**路由（可多次指定） |
| `--dry-run` | 只计算并打印报告，不写文件 |
| `--no-delete` | 只新增，不删除本地已有模型 |
| `--sort` | 把每个路由的模型列表按 id 字典序升序（大小写敏感）重排 |
| `--no-credits` | 跳过自动余额查询 |
| `--timeout SECONDS` | **单个**请求的超时（秒），默认 `15` |
| `--workers N` | 并发请求数；默认按请求数自动并发（上限 `8`），`--workers 1` 退回完全串行 |
| `--credentials PATH` | 覆盖凭据文件路径（默认 `~/.dsh/.credentials.yaml`） |
| `--list-routes` | 只列出可选路由后退出（不联网） |
| `--no-color` | 强制关闭报告着色（等价于设置 `NO_COLOR`） |
| `--version` | 显示版本信息 |

### 4. 并发请求

CLI 默认并发请求所有选定路由的模型列表端点与已知厂商的余额端点，把整轮网络耗时从"各端点串行耗时之和"降到"最慢单个请求"附近：

- **单阶段共享线程池**：模型发现与余额查询放进同一个线程池同时调度（模型任务优先排在队列前部），默认线程数等于待发请求数并封顶于 `8`；`--workers N` 可指定正整数并发度，传 `--workers 1` 则直接在当前线程串行执行、不创建线程池。
- **确定性结果**：所有请求并发执行，但结果严格按提交顺序回收；YAML 改写只在全部网络请求完成后，按路由选择顺序在主线程串行累积并由 CLI **一次性原子写盘**。因此不同 `--workers` 值产生的 stdout、stderr、退出码与写出的文件内容逐字节一致。
- **异常与删除保护隔离**：单个端点请求超时、非 2xx 或意外异常只影响该路由（报 `error:` 并保留该路由既有模型，不中断其他路由）；余额查询失败或异常降级为 `unavailable`，不影响模型同步与退出码；异常信息中的凭据头会自动脱敏为 `<redacted>`。
- **主线程凭据解析**：`credentials.resolve()` 与请求头构造全部在主线程、提交并发任务之前按路由各完成一次；工作线程只接收不可变参数与独立请求头副本。
- **超时语义**：`--timeout` 依然是**单个** HTTP 请求的超时时间，而不是整轮同步的总超时。
- **库调用默认串行**：直接调用 Python API [`sync_config()`](src/dsh_byok_sync/sync.py) 时 `max_workers` 默认为 `1`（保护外部传入的非线程安全 stub）；传入 `max_workers=None` 即可启用与 CLI 相同的自动并发。

## URL 规范

配置里的 `baseURL` 可以是下列任意一种写法，工具会把它归一到 `https://api.deepseek.com/v1/models` 这类列表 URL 再请求：

- `https://api.deepseek.com`
- `https://api.deepseek.com/v1`
- `https://api.deepseek.com/v1/chat/completions`（含尾斜杠、任意大小写）
- `https://api.deepseek.com/v1/responses`（Responses-API 网关）
- 以上任意一种带 query string（如 `?limit=100`，会被剥离）

`anthropic-messages` 的列表 URL 允许 API 根地址带或不带末尾 `/v1`（网关文档两种写法都会发布），工具只在**列表请求**里归一化这一段；`google-generative-ai` 的 `baseURL` 本身已带版本段（如 `/v1beta`），不做额外改写。

归一化的完整规则：

- 先去掉首尾空白、query string 与 fragment（列表接口没有分页参数可传，工具只读一页，与 DSH 自带发现一致）；
- 末尾如果是别的操作路径（`/chat/completions`、`/responses`、`/completions`、`/embeddings`，大小写不敏感）会被剥掉；
- 末尾的 `/models` 也会先剥掉，保证只追加一次（若 `baseURL` 本身就是列表地址，则原样使用）；
- 路径为空（纯域名）时补 `/v1/models`；**其它任何路径前缀原样保留**，只在其后追加 `/models`——`https://gw.example/openai` → `https://gw.example/openai/models`，绝不擅自插入 `/v1`；显式版本段（`/v2`、`/v1beta`）因此不会被改写；
- `baseURL` 为空、缺 scheme 或不是 `http(s)` 时视为"无端点"，报告 `warning: no endpoint url`：该路由不做新增/删除，但仍会清理无效条目（不会中断整个运行）。

## 三种协议的端点发现

端点的 `api` 字段决定列表 URL 与认证头，语义对齐 DSH 自带的模型发现：

| `api` | 列表 URL | 认证头 |
| - | - | - |
| `openai-completions` / `openai-responses`（或未写 `api`） | `GET {baseURL}/models` | `Authorization: Bearer <key>` |
| `anthropic-messages` | `GET <root>/v1/models?limit=1000` | `x-api-key: <key>`、`anthropic-version: 2023-06-01` |
| `google-generative-ai` | `GET {baseURL}/models?pageSize=1000` | `x-goog-api-key: <key>` |

**请求头的合并顺序**（显式配置始终优先）：

- 路由自己的 `headers` 全部原样带上（例如部署方自带的 `Authorization`、`HTTP-Referer`），工具绝不覆盖；
- 路由没配 `Accept` 时补 `Accept: application/json`；
- `anthropic-messages` 路由没配 `anthropic-version` 时补 `2023-06-01`（缺它该列表接口会拒绝请求）；
- 只有解析到可用密钥、且路由没有自己设置该认证头时，才补协议对应的认证头；解析不到密钥就发**无认证请求**，让端点的 401 成为最准确的诊断。

**响应解析**（三种协议的返回值统一归一）：

- 先找 `data` 数组；没有则找 `models`（映射或数组，Gemini 用这种）。
- `id` ← 映射键或 `id`（Gemini 的 `models/<名>` 去掉 `models/` 前缀）；
- `name`（显示名）← `name` / `display_name` / `displayName`，缺省回退到 `id`；
- `contextWindow` ← `contextWindow` / `context_window` / `context_length` / `max_input_tokens` / `limit.context` / `inputTokenLimit`；
- `maxTokens` ← `maxOutputTokens` / `max_output_tokens` / `maxTokens` / `max_tokens` / `limit.output` / `top_provider.max_completion_tokens` / `outputTokenLimit`。

非 200 响应、超时、网络错误、响应不是合法 JSON 或结构不可解析时，该路由记为 `error: ...` 并跳过删除（原因文本里绝不会出现密钥）。

## API key 自动解析

provider 的 `apiKeyEnv` 是一个**凭据引用名**（例如 `AGNESAI_API_KEY`），工具按下面顺序解析真实密钥：

1. **进程环境变量**：同名环境变量（`$env:AGNESAI_API_KEY`）；
2. **DSH 凭据文件** `~/.dsh/.credentials.yaml` 的 `refs.<NAME>`：该文件是 `version` / `records` / `refs` 三段结构，`refs` 是 `名字 -> 明文密钥`；
3. **DSH 凭据记录** `records["llm-pi-ai/<route>"]` 的 `payload` 里的 `apiKey` / `key` / `token` / `secret` 字段（DSH 登录流程写入的记录）。

**只有配置了 `apiKeyEnv` 的路由才会解析密钥。** 没写 `apiKeyEnv` 视为"这个路由不需要凭据"，工具不会替它去翻 `records`——否则会拿一把用户从未绑定到该路由的密钥去认证。

**行为矩阵：**

| 情况 | 请求是否带认证头 | 说明 |
| - | - | - |
| 未配置 `apiKeyEnv` | 否 | 视为无需凭据；也不查凭据记录 |
| 环境变量命中 | 是 | 直接使用 |
| 环境变量缺失、`refs.<NAME>` 命中 | 是 | 从凭据文件读取 |
| 前两者缺失、`records["llm-pi-ai/<route>"]` 命中 | 是 | 使用 DSH 记录里的密钥 |
| 都解析不到 | 否 | 降级为无认证请求，端点自行返回 401，不中断其他路由 |
| 凭据文件缺失或损坏 | 视情况 | 容忍缺失/损坏，按后续来源继续解析 |

解析出来的密钥**只存在于进程内存**，不打印、不写日志、不落盘；错误信息里即使异常文本包含密钥，也会被替换成 `<redacted>`。用 `--credentials PATH` 可指向另一份凭据文件。

## 新增模型的字段

新加入的条目按固定顺序写盘：`id → name → contextWindow → maxTokens → input → reasoningEfforts`（`compat` 保留但本工具不写）。

| 字段 | 取值优先级 | 说明 |
| - | - | - |
| `id` | 远端 | 必填，来自列表接口 |
| `name` | 远端 > 预设 > 用 `id` | 列表接口没给显示名时用 `id` |
| `contextWindow` | 远端 > 预设 > **省略** | 省略时由路由 `defaultContextWindow` 兜底（DSH 默认 **1,000,000**） |
| `maxTokens` | 远端 > 预设 > **省略** | 省略时由路由 `defaultMaxTokens` 兜底（DSH 默认 **128,000**） |
| `input` | 预设 > **省略** | 列表接口不提供模态信息；省略时用路由 `defaultInput` |
| `reasoningEfforts` | 预设 > **省略** | `false` 或 `级别 -> 线上拼写` 映射 |

远端的 `object`、`created`、`owned_by` 等协议字段不会写进配置。

**兜底语义值得单独强调**：`contextWindow` / `maxTokens` 在条目里缺省**不是"没有上限"**，而是交给路由级默认值——DSH 的 `defaultContextWindow` 默认 1,000,000、`defaultMaxTokens` 默认 128,000。因此当你对一个上下文远小于 1M 的端点省略这两个字段时，DSH 会按 1M 计算上下文预算。若该端点确实更小，建议在预设里显式给出，或让远端列表接口返回真实值。

写出的标量在有歧义时才加引号（例如 `[free]claude-opus-5` 写成 `"[free]claude-opus-5"`），列表用流风格 `[text, image]`，映射的键另行缩进。

已存在的条目**不做任何重写**：哪怕它命中了非文本关键词、或字段与远端不一致，只要远端仍然返回这个 id 就原样保留（注释、缩进、字段顺序、`compat` 全都不动）。预设同样只作用于**新增**模型，不会回头修改旧条目。

## 模型预设

包内置的 [`src/dsh_byok_sync/presets.json`](src/dsh_byok_sync/presets.json) 为特定模型提供非通用的默认值。当前 DeepSeek 预设匹配 id 中（大小写不敏感）的 `deepseek` 子串，并设置：

- `contextWindow: 1000000`（1M 输入 token）
- `maxTokens: 384000`（384K 输出 token）
- `reasoningEfforts: {off: null, low: low, medium: medium, high: high, max: max}`

预设文件格式为顶层 `presets` 数组，每项包含 `model_id_contains` 字符串数组和 `overrides` 对象：

```json
{"presets": [{"model_id_contains": ["deepseek"],
              "overrides": {"contextWindow": 1000000, "maxTokens": 384000,
                            "reasoningEfforts": {"off": null, "low": "low", "medium": "medium", "high": "high", "max": "max"}}}]}
```

规则：

- 允许覆盖的字段只有 `name`、`contextWindow`、`maxTokens`、`input`、`reasoningEfforts`；出现其他字段（含 `id`）会报 `ModelSyncError`。
- `contextWindow` / `maxTokens` 必须是正整数；`name` 必须是非空字符串；`input` 必须是非空列表。
- `reasoningEfforts` 可以是 `false`，也可以是"级别映射"；映射的键必须是 DSH 的思考级别 `off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`，值为字符串或 `null`；`input` 的元素必须是 `text` / `image`。
- 多个预设同时命中时**只用第一个**；预设只作用于新增模型。
- 文件是随包安装的普通 JSON，**可以直接编辑**；内置预设损坏或格式不合法时工具抛 `ModelSyncError` 并中止，**不会静默降级**成"没有预设"。

## 非文本模型过滤

拉取模型列表时自动忽略非文本模型：只看远端条目的 `id` 与 `name`（大小写不敏感），命中即**不新增入库**，并在报告里以 `filtered non-text (...)` 行列出。

- **子串命中**（长且独特，无碰撞风险）：`video`、`audio`、`rerank`（含 `re-rank`）、`whisper`、`speech`、`midjourney`
- **独立 token 命中**（前后不能是字母，数字后缀可以命中）：`tts`、`stt`、`asr`、`voice`、`dall`、`dalle`、`sora`、`imagen`、`music`、`image`、`embed`、`embedding`、`embeddings`、`diffusion`
  - 因此 `invoice-parser`（含 `voice`）、`medallion-7b`（含 `dall`）、`sorami-7b`（含 `sora`）、`imagenet-classifier`（含 `imagen`）、`musical-theory-llm`（含 `music`）、`imagery-chat`（含 `image`）、`embedded-reasoning`（含 `embed`）、`diffusiongemma-26b-a4b-it`（含 `diffusion`，离散扩散**文本**模型）**不会**被误杀
  - `tts1`、`asr1`、`sora2`、`imagen4`、`dalle3`、`diffusion3` 等数字后缀命名正常过滤
  - camelCase 命名会先按大小写边界切词再匹配：`OmniVoice`（`voice`）、`MultiAudioMix`（`audio`）命中；上面这些全小写/连字符的文本模型不受影响
- **中文词根**：`视频`、`图像`、`图片`、`音频`、`语音`、`音乐`；当 `name` 含「理解」时豁免（`图像理解` / `视频理解` / `图片理解` 是 VLM 对话模型，保留）
- `vision` 是文本对话模型的多模态能力标记，**不在**过滤范围内

在本机真实配置上 dry-run 的实测结果（样例）：被过滤的有 `agnes-video-2.5`、`agnes-image-2.0-flash`、`openai/gpt-audio`、`google/gemini-embedding-001`、`nvidia/nemotron-3.5-embed-8b`；`deepseek-v4.1-flash`、`invoice-parser` 等文本模型正常保留。

过滤只影响"新增"：本地已存在、且远端仍返回该 id 的条目会保留（见下节）。

## 删除规则

默认情况下，工具会删除本地存在、但远端列表接口已不再返回的模型。为免网络故障导致误删，删除必须同时满足三个条件：

- 未使用 `--no-delete`；
- 该路由的请求**成功**（`error` 为空）；
- 该路由远端返回了**至少一个有效的文本模型 id**。

第三条是**端点级文本信号授权**：一个端点如果只返回非文本模型（例如只挂了嵌入/语音服务），它没有资格证明"你的文本模型都消失了"，所以该路由下的本地模型全部保留，并在报告里以 `no text signal (deletion skipped)` 说明。

请求返回 `401`、超时、网络错误或响应格式无效时，该路由**不删除**任何模型，报告标题行为 `[error] <route>`。`baseURL` 缺失或无法构成合法 `http(s)` URL 时同理：报 `warning: no endpoint url`，不新增、不删除（无效条目仍会清理）。

**`--no-delete`**：无论远端结果如何，都只新增、不删除本地已有模型。

**顺序语义**：默认**保留你手排的模型顺序**，新增条目按远端返回顺序**追加到列表末尾**。当计算结果与文件现状完全一致（no-op）时，工具**根本不会调用改写器**——文件逐字节不变，报告是 `[unchanged]` 加一行 `kept N models`。加 `--sort` 则顺带把每个路由的模型列表按 id 字典序升序（大小写敏感，`Z` 排在 `a` 前）重排——首次排序会重写一次，之后顺序稳定，再跑 `--sort` 即 no-op。

**本地重复 id**：同一 id 在本地出现多条时，`--no-delete` 下**全部保留**；允许删除且远端已不下发该 id 时**全部删除**；远端仍下发该 id 时按出现次数一一对应保留。

**远端重复 id**：同一 id 在远端出现多条时，只要其中**有一条**是文本模型，该 id 即视为文本模型（保留/新增）；全部条目都是非文本时才过滤。

**无效条目始终清理**：模型条目必须有**非空字符串** `id` 才有意义。缺少 `id`、`id` 为空、`id` 不是字符串、乃至条目不是映射——这类条目在任何模式下都会被**直接丢弃**（与 `--no-delete` 无关），报告以 `discarded invalid entries (...)` 列出被丢弃的内容。丢弃本身算实质变更，会触发写盘。

**`models` 的写法要求（前置校验）**：出现下面两种情况时，该路由**完全不被改写**，只报 `error`（退出码 `1`）：

- `models` 是**非空**的流风格列表（`models: [a, b]`）——行内写法无法逐条保留原文，请先手动改成块风格再同步；
- `models` 不是列表（块状映射、块标量或标量值），报告为 `error: models 不是列表，无法安全同步`。

`models: []`（空流风格列表）不算问题：空列表没有内容需要保留，会被规范化为块风格列表并填入同步结果。

**完全没有 `baseURL`** 的路由**不新增、也不删除**任何模型（报告 `warning: no endpoint url`），但它**仍会清理无效条目**——无效条目与端点无关。

## 余额查询

同步过程中，工具会对**已知厂商域名**与模型列表端点在同一线程池中并发查询账户余额，并在报告对应路由下追加 `credits:` 行。匹配依据是 `baseURL` 的域名，与路由名无关。

| 域名 | 余额端点 | 货币 |
| - | - | - |
| `api.deepseek.com` | `/user/balance` | CNY |
| `openrouter.ai` | `/api/v1/credits` | USD |
| `api.moonshot.cn` | `/v1/users/me/balance` | CNY |
| `api.moonshot.ai` | `/v1/users/me/balance` | USD |
| `api.stepfun.com` | `/v1/accounts` | CNY |
| `api.novita.ai` | `/v3/user/balance` | USD |

行为规则：

- **未知域名不查询**（报告里不出现 `credits` 行）。
- **查询失败显示 `unavailable`**：已知域名的请求失败、或响应里没有可解析的字段时，`credits:` 行显示 `unavailable`。
- 余额是**纯展示信息**，不影响模型同步与删除保护逻辑，也不影响退出码。
- 需要鉴权的端点复用与模型同步同一把 API key。
- `--no-credits` 完全跳过余额查询。

## 报告着色

在**交互式终端**（Windows Terminal、系统终端等）中运行时，报告会自动着色。**只有"标签"上色，正文（模型 id、数字、错误消息）保持终端默认颜色**，这样几百个 id 的长列表仍然好读——与 `copilot-byok-sync` 的着色模型一致：

| 上色部分 | 颜色 | 含义 |
| - | - | - |
| `[changes]` | 绿 | 该路由有变更 |
| `[unchanged]` | 灰 | 无实质变化 |
| `[error]` | 红 | 该路由失败 |
| 路由名（`[xxx]` 之后） | 加粗 | 定位锚点，不带颜色 |
| `added` / `removed` 标签 | 绿 / 红 | 新增 / 删除的模型数量与 id 列表保持默认色 |
| `kept` 标签 | 灰 | `kept 163 models` 中只有 `kept` 上色 |
| `filtered non-text`、`no text signal`、`deletion skipped`、`discarded invalid entries` 标签 | 黄 | 需要注意的提示 |
| `warning: no endpoint url` 整行 | 黄 | 该路由没有端点，无法同步 |
| `error:` 标签 | 红 | 其后的错误消息保持默认色 |
| `credits:` 标签 | 青 | 余额数字保持默认色 |
| `Summary:` | 加粗 | 汇总内容保持默认色 |
| `config: <路径>` | 默认 | 不参与着色 |

**着色启用条件：**

- **灰色按终端自动选写法**：Windows Terminal / VS Code 集成终端 / 其他 xterm 类终端（存在 `WT_SESSION`、`TERM_PROGRAM`、`COLORTERM`、`TERM` 等标记）用 **`\x1b[2m`（faint）**，与参考项目 `copilot-byok-sync` **逐字节一致**，观感相同；传统 conhost **会忽略 faint**（实测单元格属性仍是默认 `0x07`），那里改用 **`\x1b[2;90m`**（faint + 亮黑，渲染为 `0x08` 灰），保证 `[unchanged]`/`kept` 仍然看得出是灰色。
- 仅当标准输出（stdout）是**交互式 TTY** 时输出 ANSI 颜色。
- Windows 上还要求终端**真的能渲染 ANSI**：工具会用 `GetConsoleMode`/`SetConsoleMode` 打开当前控制台的 `ENABLE_VIRTUAL_TERMINAL_PROCESSING`（VT100）位，这样**传统 conhost（双击 `.bat` 的那个窗口）会渲染成颜色，而不是把 `[32m` 原样打印出来**。若系统过旧、打开失败，自动退回纯文本；VS Code 集成终端、Windows Terminal 这类本身就能渲染 ANSI 的终端不受此步影响。
- stdout 被**重定向或管道**（`> log`、`| Tee-Object`）时自动输出纯文本。
- 设置环境变量 **`NO_COLOR`**（任意值，如 `NO_COLOR=1`）或加 `--no-color` 可强制关闭颜色。
- stdout **不是交互式终端**时，完整报告还会以**纯文本**（不带色码）**镜像到 stderr**，并附一行状态说明（`已写入文件` / `dry run: 未写入文件`），供脚本 / CI 抓取。

## 安全性说明

- **只改 `models`。** provider 的其余字段（`apiKeyEnv`、`baseURL`、`api`、`displayName`、`modelOverrides`、`compat`、`retryPolicy`、`defaultContextWindow`、`defaultMaxTokens`、`headers` ……）不会被动；配置文件里**其他插件行**同样一个字节都不动。
- **注释与格式尽可能保留。** 被保留的模型条目连同其前导注释、空行、缩进风格（2 空格 / 4 空格）、行内注释一起原样复用；`CRLF` / `LF`、末尾换行与文件有无 BOM 都按原文件保持。只有新增条目由工具按规范渲染。
- **原子写入。** 先写同目录临时文件再 `os.replace`，避免半截文件；no-op 时**根本不写盘**（连改写器都不会被调用）。
- **拒绝不安全输入。** 目标区域出现 Tab 缩进、作用于此块的 YAML 锚点/别名、`!!js` 标签等无法保真处理的情况时，工具报错退出（退出码 `2`）而不是冒险重写；`models` 的形态不合规（非空流风格、非列表值）则在加载阶段就被拒（该路由报 `error`、不改写、退出码 `1`）。
- **密钥不落盘、不打印、不记录。** 只读凭据，从不回写；错误信息中不会出现密钥文本。

## 工作区推荐配置

在 [.vscode/tasks.json](.vscode/tasks.json) 里提供四个任务（`Tasks: Run Task` 调用）：

- `Sync DSH Models`：在 VS Code 集成终端里对当前 profile 的 `cordis.patch.yml` 执行 `--all` 同步（未加 `--no-delete`，因此会清理远端已下线的模型）。
- `Sync DSH Models (Dry Run)`：同上但只预览，不写盘。
- `Sync DSH Models (Console)`：通过 `start cmd` 弹出独立控制台窗口运行，报告直接显示在窗口里并 `pause` 保留。
- `Sync DSH Models (Console) (Dry Run)`：独立窗口的预览版。

任务默认把 `PYTHONPATH` 指向 `${workspaceFolder}/src`，因此**未执行 `pip install -e .` 也能直接跑**；配置文件走默认探测顺序（不传 `--config`），DSH 的 `DSH_PROFILE_DIR` 未设置时会自动回退到 `~/.dsh/profiles/desktop/cordis.patch.yml`。任务行文里用的是 `python`，需要它在 PATH 中（批处理脚本则会自动回退到 `py -3`）。

**双击运行的批处理脚本。** 仓库根目录还提供 `sync-dsh-models.bat`：

- 双击即对当前 profile 的 `cordis.patch.yml` 同步：**跑一次、直接写盘、没有预览也没有 Y/N 确认**（与参考项目 `copilot-byok-sync` 的 `sync-global-models.bat` 一致）；同步完成后窗口 `pause` 保留，报告和报错都看得见；
- 如果你就是想先看看、不写盘，别用这个 bat，直接跑 `python -m dsh_byok_sync --all --dry-run`（或 `--no-delete`）；
- 优先用 `python`，找不到时自动回退 `py -3`，两者都没有则给出明确错误；
- **参数不扫描、不转发**：早期版本会 `echo %* | findstr /i "--provider"` 来决定是否补 `--all`，这行会让 cmd 重新解析用户参数，路径里只要有一个 `&`（如 `C:\x & y\c.yml`）或空格就会被拆开并执行错位命令——参考项目没有这段逻辑，本仓库也已删掉。需要按路由/按选项同步时请直接调用 CLI；
- **批处理本身是纯 ASCII 英文提示，并且刻意不调用 `chcp`**（见下），中文报告由 Python 直接以 Unicode 写控制台，任何代码页下都能正常显示。

> **为什么 `.bat` 是纯 ASCII 且不 `chcp`？** cmd.exe 读取批处理时以**字节偏移**跟踪行位置；一个 UTF-8 批处理在第 2 行切换代码页会让后续偏移错位，典型症状是提示行被劈开并报 `'xxx' is not recognized as an internal or external command`。同理，`.bat` 必须保存为 **CRLF** 行尾。中文由 Python 输出（Python 用 `WriteConsoleW` 直接写 Unicode），因此**不需要**也不应该改代码页。

## 开发

### 运行测试

```bash
python -m unittest discover -s tests -v
```

测试只写 `unittest.TestCase` 风格，因此也能被 `pytest` 收集。测试**不访问真实网络**（HTTP 通过注入的 `opener` 替身）也**不读取真实凭据**（自带临时凭据文件或临时环境变量）。

### 项目结构

```
dsh-byok-sync/
├── .vscode/
│   └── tasks.json            # 工作区级同步任务（含独立控制台窗口版与 dry-run 变体）
├── src/dsh_byok_sync/
│   ├── __init__.py           # 版本号与导出
│   ├── __main__.py           # python -m 入口
│   ├── cli.py                # 命令行入口（参数解析、报告、终端自动着色）
│   ├── sync.py               # 核心同步逻辑（并发请求调度、新增/保留/删除、预设、排序）
│   ├── configfile.py         # 文件形态判定、读取、原子写入、路径探测
│   ├── yamlpatch.py          # YAML 子集解析与"字节级保留"重写
│   ├── client.py             # HTTP 请求、URL 规范化、余额查询
│   ├── credentials.py        # DSH 凭据解析（env / refs / records）
│   ├── models.py             # 数据结构、非文本过滤、显示名
│   ├── presets.py            # 预设加载与校验
│   └── presets.json          # 内置模型预设（可编辑）
├── tests/
│   ├── _helpers.py           # 工作区内的临时目录助手
│   ├── test_yamlpatch.py
│   ├── test_configfile.py
│   ├── test_client.py
│   ├── test_credentials.py
│   ├── test_cli_color.py
│   ├── test_concurrency.py
│   └── test_sync.py
├── sync-dsh-models.bat       # 双击即同步（cmd 窗口，跑一次直接写盘）
├── CHANGELOG.md
└── README.md
```

### 后续计划

- 把"保留既有条目"升级为可选的小幅规范化（例如统一字段顺序），默认仍保持字节不变
- 支持在预设里按 `baseURL` 域名匹配，而不只是模型 id 子串
- 提供 `--prune-routes`：清理已从配置移除但仍残留在凭据记录里的路由条目

## 已知限制

- YAML 支持面向 DSH 配置文件的实际写法（列表/映射、块/流风格、引号、注释），**不是通用 YAML 实现**。出现下列情况时工具**直接拒绝并报错，绝不猜测**：Tab 缩进、多文档 `---`、作用在待重写区域上的锚点/别名/`!!js` 标签、provider 映射本身写成流风格（`iris: {api: ..., models: [...]}`）。这类拒绝发生在加载或改写阶段，退出码 `2`。
- **`models` 的形态要求**：**非空**流风格列表 `models: [a, b]`、以及 `models` 的**非列表值**（块状映射、块标量、标量）会在**加载阶段**被拒绝——该路由只报 `error`、**完全不改写文件**，退出码 `1`。遇到时请先手动把该块改成块风格再同步。
- `models` 缺失、`models:` 空值、`models: []`（空流风格列表）都是合法的：工具会自动补成块风格列表。
- 删除只能在**端点给出有效文本信号**时发生；远端接口若整体不可用，工具宁可留下陈旧模型也不误删。
- 非文本过滤是**关键词启发式**：命名奇特的模型可能漏网或误伤。camelCase 会在匹配前按大小写边界切词，因此 `OmniVoice`（`voice`）、`MultiAudioMix`（`audio`）能被正确识别；全小写/连字符命名的文本模型（`invoice-parser`、`medallion-7b`、`imagenet-classifier`）不受影响。误伤方向请参考上面的边界样例。反过来，你手工写进配置的条目只要远端仍返回该 id，工具都不会删它。
- 只读取凭据，不写入、不刷新、不加密；DSH 登录态变更后请让 DSH 自己更新凭据文件。
- `reasoningEfforts`、`input` 只能来自预设或手工编写；`/v1/models` 不提供这类能力信息。
- 每个路由只读**一页**模型列表（`limit`/`pageSize` 已放大到 1000），不跟随分页游标——与 DSH 自带发现一致。
- 余额查询只覆盖上表列出的域名，其余端点不查询也不显示 `credits` 行。
- 用自定义 `headers`（例如自带 `Authorization`）做认证的路由不需要 `apiKeyEnv`；此时工具不会再去解析密钥，只带上你配置的头。
- 一个配置文件里同一路由只会出现一次；`cordis.patch.yml` 与 `settings.yaml` 里的同名路由需要分别用 `--config` 同步。
