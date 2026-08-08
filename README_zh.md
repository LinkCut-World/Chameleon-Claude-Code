# Chameleon Claude Code（ccc）

English: [README.md](README.md)

本仓库是 [spideynolove 的 Claude Code 多提供商配置指南](https://gist.github.com/spideynolove/13785891385ed6916619ebb991b490b9) 的 Windows 版。

---

一个命令，让 Claude Code 在任意 Anthropic 兼容提供商之间切换。

- 提供商用 `ccc providers` 管理
- **Method 1** — `ccc <名字>`：用指定提供商直接启动 claude
- **Method 2** — `ccc server` + `ccc all`：本地代理，会话内用 `/model` 切换提供商

## 安装

前置：Claude Code、Python 3.10+。

仅 Method 2 需要——执行：

```cmd
pip install fastapi "uvicorn[standard]" httpx pydantic
```

把 `bin` 目录加入用户 PATH。

## 管理提供商

```
ccc providers
```

列出所有提供商：`A` 新增、`D` 删除、输入序号进入编辑。

提供商的 `env` 里必须包含这些键（ccc 只读取这 5 个）：

- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_DEFAULT_OPUS_MODEL`
- `ANTHROPIC_DEFAULT_SONNET_MODEL`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`

其他键会被 ccc 忽略。

新增提供商时，可以手动逐个输入这 5 个键的值，或直接粘贴提供商建议添加到
`C:\Users\<用户名>\.claude\settings.json` 的 JSON 文本（它本身就带 `env` 键）：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://example.com",
    "ANTHROPIC_AUTH_TOKEN": "您的 API key",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "ANTHROPIC_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES": "thinking,adaptive_thinking,temperature,effort,max_effort",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES": "thinking,adaptive_thinking,temperature,effort,max_effort",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES": "thinking,adaptive_thinking,temperature,effort,max_effort",
    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-5",
    "CLAUDE_CODE_EFFORT_LEVEL": "max"
  },
  "hasCompletedOnboarding": true
}
```

粘贴的文本中，除了这 5 个键会被读取，其他键都会被忽略。

## .env（可选）

可在仓库根目录创建 `.env` 文件，放一些通用额外环境变量，
每次启动 claude 时都会注入。例如：

```
CLAUDE_CODE_ATTRIBUTION_HEADER=0
```

## Method 1：直接用某提供商

读取本地配置里该提供商的 5 个环境变量，直接启动 claude。用 `ccc <名字>` 启动；
`ccc clear` 清掉提供商配置、回默认 Anthropic。

```cmd
ccc deepseek
ccc kimi -m some-model
```

## Method 2：会话内切换

本地代理按请求 model 字段里的提供商名路由；claude 走代理，所以会话内用 `/model` 切换。

在一个终端启动代理，在另一个终端通过代理启动 claude：

```cmd
ccc server
ccc all
```

在 claude 会话里，通用格式是 `/model <提供商名>` 或 `/model <提供商>/<模型>`：

```
/model kimi
/model kimi/moonshot-v1-8k
```

- `/model <提供商名>` 用的是该提供商的默认 opus 模型，所以提供商必须设置了 `ANTHROPIC_DEFAULT_OPUS_MODEL`。
- `/model <提供商>/<模型>` 里的模型必须是该提供商配置的 `ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL` 之一。

## 提供商要求

提供 Anthropic 兼容的 `/v1/messages` 接口即可（如 DeepSeek、GLM、Kimi）。
只有 OpenAI 兼容接口的提供商需要中间转换层，本工具暂不支持。

## 安全

- 代理只监听 `127.0.0.1`。
- 您的 provider 数据都存在仓库根目录的 `providers.json` 文件里，该文件已被 `.gitignore` 忽略，不会上传。
