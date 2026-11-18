# xiaoye CPA Sync

将 CPA 服务端的模型能力配置同步到本机 harness。目前支持 OpenCode、DeepSeek Harness（DSH）、Pi、Kimi Code 和 Codex。

同步内容包括模型显示名称、上下文与输出限制、输入输出模态、工具能力和每个模型实际支持的思考强度。密钥通过环境变量读取，不写入同步器配置或缓存。

## Windows 一键安装

```powershell
irm https://raw.githubusercontent.com/BakaXXXXXL/xiaoye_cap-sync/main/install.ps1 | iex
```

安装到 `%LOCALAPPDATA%\Programs\cpa-sync`，不需要管理员权限、Python 或 PyYAML。安装后如果找不到命令，请打开新终端。

## 首次配置

```powershell
$env:CPA_BASE_URL = "https://你的服务器/cpa/v1"
$env:CPA_API_KEY = "你的 Key"
cpa-sync setup opencode --verify
```

`setup` 会把 BaseURL 和 Key 保存到当前 Windows 用户环境变量中，并让 OpenCode 使用 `{env:CPA_API_KEY}`。如果指定 DSH，则使用对应的 profile：

```powershell
cpa-sync setup dsh --profile web --verify
```

其他 harness 使用同一套 BaseURL 和环境变量：

```powershell
cpa-sync setup pi --verify
cpa-sync setup kimi --verify
```

Pi 使用 `PI_CODING_AGENT_DIR/models.json`（默认 `~/.pi/agent/models.json`），打开 `/model` 会重新读取。Kimi Code 使用 `KIMI_CODE_HOME/config.toml`（默认 `~/.kimi-code/config.toml`），`--verify` 会运行 `kimi doctor config`。

Codex 使用独立 profile，不覆盖原有默认 provider：

```powershell
cpa-sync setup codex --verify
codex --profile cpa
```

这会生成 `CODEX_HOME/cpa.config.toml` 和 `CODEX_HOME/cpa-models.json`（默认 `~/.codex`）。Codex 只支持 Responses API，修改后需要重启 CLI 或桌面应用。

## 后续同步

```powershell
cpa-sync sync opencode --verify
cpa-sync sync dsh --profile web --verify
cpa-sync sync dsh --profile dsh-tui --verify
cpa-sync sync dsh --profile desktop --verify
cpa-sync sync pi --verify
cpa-sync sync kimi --verify
cpa-sync sync codex --verify
```

DSH 的 profile 名称以 `.dsh/profiles` 下的目录为准，不一定与启动子命令相同。`desktop` profile 由 Electron 独占管理，因此 `--verify` 只解析完整 YAML，不调用 CLI 重载。

Pi 使用 `thinkingLevelMap` 精确限制每个模型可选的强度，并保留已有价格等自定义字段。Kimi Code 写入 `support_efforts`、`default_effort` 和 `off_effort`。Codex 写入本地模型目录的 `supported_reasoning_levels` 和 `default_reasoning_level`。三端都不会把实际 API Key 写入配置。

服务端不再返回的模型默认保留；明确指定 `--prune` 才会删除：

```powershell
cpa-sync sync opencode --prune
```

## 配置安全与兼容性

- 只修改 OpenCode 的 `providers.cpa` 区域，保留其他供应商和用户设置。
- OpenCode 旧版 `provider/npm/options` 会自动迁移为 `providers/package/settings`。
- 只修改 DSH 指定 profile 中的 `llm-pi-ai.providers.cpa`，保留超时、重试、compat 和其他 provider。
- Pi 只合并 `providers.cpa`；Kimi Code 只合并 CPA provider 及其模型表，保留其他设置和模型 overrides。
- Codex 只使用指定的 profile 文件和 `cpa-models.json`，不修改主 `config.toml`。
- 写入前自动备份，使用临时文件和原子替换；新增配置文件首次创建时不需要备份。
- `--no-reload` 可跳过 OpenCode 重载或 harness 验证。
- 卸载不会删除同步器的配置、缓存或 harness 配置。

卸载：

```powershell
irm https://raw.githubusercontent.com/BakaXXXXXL/xiaoye_cap-sync/main/uninstall.ps1 | iex
```

## 服务端接口

```http
GET {base_url}/capabilities
Authorization: Bearer {api_key}
```

响应使用 `schema_version: 1`，包含 `config_version` 和 `models`。同步器会校验模型 ID、推理强度、默认强度和容量限制，校验失败时不会修改本地配置。

## 从源码构建

```powershell
python -m pip install pyinstaller pyyaml
python -m PyInstaller --onefile --name cpa-sync --clean cpa_sync.py
.\dist\cpa-sync.exe --version
```

打 `v*` 标签后，GitHub Actions 会构建并发布 Windows x64 Release。
