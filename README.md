# xiaoye CPA Sync

将 CPA 服务端的模型能力配置同步到本机 harness。目前支持 OpenCode 和 DeepSeek Harness（DSH）。

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

## 后续同步

```powershell
cpa-sync sync opencode --verify
cpa-sync sync dsh --profile web --verify
cpa-sync sync dsh --profile dsh-tui --verify
cpa-sync sync dsh --profile desktop --verify
```

DSH 的 profile 名称以 `.dsh/profiles` 下的目录为准，不一定与启动子命令相同。服务端不再返回的模型默认保留；明确指定 `--prune` 才会删除：

```powershell
cpa-sync sync opencode --prune
```

## 配置安全与兼容性

- 只修改 OpenCode 的 `providers.cpa` 区域，保留其他供应商和用户设置。
- OpenCode 旧版 `provider/npm/options` 会自动迁移为 `providers/package/settings`。
- 只修改 DSH 指定 profile 中的 `llm-pi-ai.providers.cpa`，保留超时、重试、compat 和其他 provider。
- 写入前自动备份，使用临时文件和原子替换。
- `--no-reload` 可跳过 OpenCode 重载或 DSH 验证。
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
