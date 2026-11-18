#!/usr/bin/env python3
import argparse
import copy
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

__version__ = "0.2.0"

APP_DIR = Path.home() / ".config" / "cpa-sync"
SETTINGS_PATH = APP_DIR / "config.json"
CACHE_PATH = APP_DIR / "cache" / "capabilities.json"
OPENCODE_PATH = Path.home() / ".config" / "opencode" / "opencode.jsonc"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_settings():
    if not SETTINGS_PATH.exists():
        raise RuntimeError("CPA is not configured; run cpa-sync setup <target>")
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_user_env(name):
    if name in os.environ:
        return os.environ[name]
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            pass
    return None


def set_user_env(name, value):
    os.environ[name] = value
    if os.name == "nt":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def parse_jsonc(text):
    chars = list(text)
    in_string = False
    escaped = False
    i = 0
    while i < len(chars):
        c = chars[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "/" and i + 1 < len(chars) and chars[i + 1] == "/":
            chars[i] = chars[i + 1] = " "
            i += 2
            while i < len(chars) and chars[i] not in "\r\n":
                chars[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < len(chars) and chars[i + 1] == "*":
            chars[i] = chars[i + 1] = " "
            i += 2
            while i + 1 < len(chars) and not (chars[i] == "*" and chars[i + 1] == "/"):
                if chars[i] not in "\r\n":
                    chars[i] = " "
                i += 1
            if i + 1 < len(chars):
                chars[i] = chars[i + 1] = " "
            i += 2
            continue
        i += 1
    clean = "".join(chars)
    chars = list(clean)
    in_string = False
    escaped = False
    for i, c in enumerate(chars):
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == ",":
            j = i + 1
            while j < len(chars) and chars[j].isspace():
                j += 1
            if j < len(chars) and chars[j] in "}]":
                chars[i] = " "
    return json.loads("".join(chars))


def skip_space(text, i):
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise RuntimeError("Unterminated JSONC comment")
            i = end + 2
        else:
            break
    return i


def string_end(text, i):
    i += 1
    escaped = False
    while i < len(text):
        if escaped:
            escaped = False
        elif text[i] == "\\":
            escaped = True
        elif text[i] == '"':
            return i + 1
        i += 1
    raise RuntimeError("Unterminated JSON string")


def value_end(text, i):
    i = skip_space(text, i)
    if text[i] == '"':
        return string_end(text, i)
    if text[i] in "[{":
        stack = [text[i]]
        i += 1
        while i < len(text):
            if text[i] == '"':
                i = string_end(text, i)
                continue
            if text.startswith("//", i) or text.startswith("/*", i):
                i = skip_space(text, i)
                continue
            if text[i] in "[{":
                stack.append(text[i])
            elif text[i] in "]}":
                stack.pop()
                if not stack:
                    return i + 1
            i += 1
        raise RuntimeError("Unterminated JSON value")
    while i < len(text) and text[i] not in ",}] \t\r\n":
        i += 1
    return i


def property_span(text, path):
    def descend(pos, parts):
        pos = skip_space(text, pos)
        if pos >= len(text) or text[pos] != "{":
            raise RuntimeError(f"Missing object for {'.'.join(parts)}")
        pos += 1
        while True:
            pos = skip_space(text, pos)
            if pos >= len(text):
                break
            if text[pos] == "}":
                raise RuntimeError(f"Missing configuration key: {'.'.join(path)}")
            if text[pos] != '"':
                raise RuntimeError("Invalid JSONC object key")
            end = string_end(text, pos)
            key = json.loads(text[pos:end])
            pos = skip_space(text, end)
            if pos >= len(text) or text[pos] != ":":
                raise RuntimeError("Invalid JSONC property")
            start = skip_space(text, pos + 1)
            end = value_end(text, start)
            if key == parts[0]:
                if len(parts) == 1:
                    return start, end
                return descend(start, parts[1:])
            pos = skip_space(text, end)
            if pos < len(text) and text[pos] == ",":
                pos += 1
        raise RuntimeError(f"Missing configuration key: {'.'.join(path)}")
    return descend(0, path)


def replace_span(text, span, value):
    start, end = span
    indent = start - text.rfind("\n", 0, start) - 1
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    rendered = rendered.replace("\n", "\n" + " " * indent)
    return text[:start] + rendered + text[end:]


def rename_property(text, path, new_name):
    start, _ = property_span(text, path)
    match = re.search(r'("(?:\\.|[^"\\])*")\s*:\s*$', text[:start])
    if not match:
        raise RuntimeError(f"Cannot rename configuration key: {'.'.join(path)}")
    return text[:match.start(1)] + json.dumps(new_name) + text[match.end(1):]


def modernize_opencode(text):
    document = parse_jsonc(text)
    if "providers" in document or "provider" not in document:
        return text
    providers = document["provider"]
    text = rename_property(text, ["provider"], "providers")
    for provider_id, provider in providers.items():
        if "npm" in provider:
            text = rename_property(text, ["providers", provider_id, "npm"], "package")
            text = replace_span(text, property_span(text, ["providers", provider_id, "package"]), "@opencode/ai/providers/openai-compatible")
        if "options" in provider:
            text = rename_property(text, ["providers", provider_id, "options"], "settings")
    return text


def request_json(base_url, api_key, endpoint):
    request = urllib.request.Request(
        base_url.rstrip("/") + endpoint,
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"CPA request failed: HTTP {exc.code} {detail}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"CPA connection failed: {exc}") from None


def validate_capabilities(document):
    if document.get("schema_version") != 1:
        raise RuntimeError("Unsupported capabilities schema_version")
    models = document.get("models")
    if not isinstance(models, list):
        raise RuntimeError("Capabilities response has no models list")
    seen = set()
    for model in models:
        mid = model.get("id")
        if not isinstance(mid, str) or not mid or mid in seen:
            raise RuntimeError("Model IDs must be non-empty and unique")
        seen.add(mid)
        reasoning = model.get("reasoning") or {}
        efforts = reasoning.get("efforts") or []
        if not isinstance(efforts, list) or not all(isinstance(v, str) and v for v in efforts):
            raise RuntimeError(f"Invalid reasoning efforts for {mid}")
        default = reasoning.get("default")
        if default is not None and default not in efforts:
            raise RuntimeError(f"Default reasoning effort is unsupported for {mid}")
        limits = model.get("limits") or {}
        for field in ("context", "max_output"):
            value = limits.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeError(f"Invalid {field} limit for {mid}")
    return [m for m in models if m.get("enabled", True)]


def opencode_model(model):
    capabilities = model.get("capabilities") or {}
    reasoning = model.get("reasoning") or {}
    modalities = model.get("modalities") or {"input": ["text"], "output": ["text"]}
    limits = model["limits"]
    supported = bool(reasoning.get("supported"))
    efforts = [value for value in (reasoning.get("efforts") or []) if value != "none"]
    result = {
        "name": model.get("display_name") or model["id"],
        "capabilities": {
            "tools": bool(capabilities.get("tools")),
            "reasoning": {
                "supported": supported,
                "toggle": bool(reasoning.get("supports_disable")),
                "effort": efforts,
            },
            "input": modalities.get("input") or ["text"],
            "output": modalities.get("output") or ["text"],
        },
        "limit": {"context": limits["context"], "output": limits["max_output"]},
    }
    if supported and reasoning.get("default") in efforts:
        result["settings"] = {"reasoningEffort": reasoning["default"]}
    return result


def backup(path):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(path.name + f".bak-cpa-sync-{stamp}")
    shutil.copy2(path, target)
    return target


def atomic_write(path, text):
    temporary = path.with_name(path.name + ".cpa-sync-tmp")
    temporary.write_text(text, encoding="utf-8")
    parse_jsonc(temporary.read_text(encoding="utf-8"))
    os.replace(temporary, path)


def update_opencode(base_url, models, key_env, setup, prune):
    original = modernize_opencode(OPENCODE_PATH.read_text(encoding="utf-8"))
    parse_jsonc(original)
    current = parse_jsonc(original)["providers"]["cpa"].get("models", {})
    generated = {model["id"]: opencode_model(model) for model in models}
    merged = copy.deepcopy(current)
    merged.update(generated)
    if prune:
        merged = {key: value for key, value in merged.items() if key in generated}
    updated = replace_span(original, property_span(original, ["providers", "cpa", "models"]), merged)
    if setup:
        updated = replace_span(updated, property_span(updated, ["providers", "cpa", "settings", "baseURL"]), base_url)
        updated = replace_span(updated, property_span(updated, ["providers", "cpa", "settings", "apiKey"]), "{env:" + key_env + "}")
    parse_jsonc(updated)
    if updated == original:
        return None, current, merged
    saved_backup = backup(OPENCODE_PATH)
    atomic_write(OPENCODE_PATH, updated)
    return saved_backup, current, merged


def dsh_model(model):
    import yaml
    reasoning = model.get("reasoning") or {}
    modalities = model.get("modalities") or {}
    inputs = [value for value in (modalities.get("input") or ["text"]) if value in ("text", "image")]
    if not inputs:
        raise RuntimeError(f"DSH has no supported input modality for {model['id']}")
    result = {
        "id": model["id"],
        "name": model.get("display_name") or model["id"],
        "contextWindow": model["limits"]["context"],
        "maxTokens": model["limits"]["max_output"],
        "input": inputs,
    }
    if reasoning.get("supported"):
        efforts = {}
        if reasoning.get("supports_disable"):
            efforts["off"] = "none" if "none" in (reasoning.get("efforts") or []) else None
        for effort in reasoning.get("efforts") or []:
            if effort != "none":
                efforts[effort] = effort
        result["reasoningEfforts"] = efforts
    else:
        result["reasoningEfforts"] = False
    return result


def update_dsh(base_url, models, key_env, setup, prune, profile):
    import yaml
    dsh_home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
    path = dsh_home / "profiles" / profile / "cordis.patch.yml"
    if not path.exists():
        raise RuntimeError(f"DSH profile does not exist: {profile}")
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    entry_start = next((i for i, line in enumerate(lines) if line.strip() == "- id: llm-pi-ai"), None)
    if entry_start is None:
        raise RuntimeError(f"DSH profile {profile} has no llm-pi-ai entry")
    entry_end = next((i for i in range(entry_start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((" ", "\t"))), len(lines))
    provider_start = next((i for i in range(entry_start, entry_end) if lines[i] == "      cpa:\n" or lines[i].rstrip("\r\n") == "      cpa:"), None)
    if provider_start is None:
        raise RuntimeError(f"DSH profile {profile} has no CPA provider")
    provider_end = next((i for i in range(provider_start + 1, entry_end) if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= 6), entry_end)
    provider_text = "".join(lines[provider_start:provider_end])
    provider = yaml.safe_load(provider_text)["cpa"]
    if not isinstance(provider, dict):
        raise RuntimeError("Invalid DSH CPA provider")
    current_models = {model.get("id"): model for model in provider.get("models", [])}
    generated = [dsh_model(model) for model in models]
    generated_by_id = {model["id"]: model for model in generated}
    merged = [generated_by_id.get(mid, model) for mid, model in current_models.items() if mid not in generated_by_id]
    merged = generated + merged
    if prune:
        merged = generated
    provider["models"] = merged
    provider["apiKeyEnv"] = key_env
    if setup:
        provider["baseURL"] = base_url
    rendered = yaml.safe_dump({"cpa": provider}, allow_unicode=True, sort_keys=False, default_flow_style=False)
    rendered = "\n".join(("      " + line if line else "") for line in rendered.splitlines()) + "\n"
    updated = "".join(lines[:provider_start]) + rendered + "".join(lines[provider_end:])
    yaml.safe_load(updated)
    if updated == original:
        saved_backup = None
    else:
        saved_backup = backup(path)
        temporary = path.with_name(path.name + ".cpa-sync-tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, path)
    return path, saved_backup, set(current_models), {model["id"] for model in merged}


def commit_text(path, original, updated):
    if updated == original:
        return None
    saved_backup = backup(path) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".cpa-sync-tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, path)
    return saved_backup


def pi_model(model, existing=None):
    reasoning = model.get("reasoning") or {}
    modalities = model.get("modalities") or {}
    inputs = [value for value in (modalities.get("input") or ["text"]) if value in ("text", "image")]
    if not inputs:
        raise RuntimeError(f"Pi has no supported input modality for {model['id']}")
    result = copy.deepcopy(existing or {})
    result.update({
        "id": model["id"],
        "name": model.get("display_name") or model["id"],
        "reasoning": bool(reasoning.get("supported")),
        "input": inputs,
        "contextWindow": model["limits"]["context"],
        "maxTokens": model["limits"]["max_output"],
    })
    if result["reasoning"]:
        levels = {level: None for level in ("off", "minimal", "low", "medium", "high", "xhigh", "max")}
        for effort in reasoning.get("efforts") or []:
            if effort == "none":
                continue
            if effort not in levels:
                raise RuntimeError(f"Pi does not support reasoning effort {effort} for {model['id']}")
            levels[effort] = effort
        if reasoning.get("supports_disable") or "none" in (reasoning.get("efforts") or []):
            levels["off"] = "none"
        result["thinkingLevelMap"] = levels
    else:
        result.pop("thinkingLevelMap", None)
    return result


def update_pi(base_url, models, key_env, setup, prune):
    pi_home = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))
    path = pi_home / "models.json"
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    document = json.loads(original) if original.strip() else {"$schema": "https://pi.dev/schemas/models.schema.json"}
    document.setdefault("$schema", "https://pi.dev/schemas/models.schema.json")
    providers = document.setdefault("providers", {})
    provider = copy.deepcopy(providers.get("cpa") or {})
    current_models = {}
    for item in provider.get("models") or []:
        if item.get("id") in current_models:
            raise RuntimeError(f"Pi CPA provider has duplicate model id: {item.get('id')}")
        current_models[item.get("id")] = item
    generated = [pi_model(model, current_models.get(model["id"])) for model in models]
    incoming = {model["id"] for model in generated}
    merged = generated + [item for mid, item in current_models.items() if not prune and mid not in incoming]
    provider.update({
        "name": "CPA",
        "api": "openai-completions",
        "apiKey": "$" + key_env,
        "authHeader": True,
        "models": merged,
    })
    if setup or not provider.get("baseUrl"):
        provider["baseUrl"] = base_url
    compat = provider.setdefault("compat", {})
    compat.update({"supportsReasoningEffort": True, "thinkingFormat": "openai", "supportsDeveloperRole": False})
    providers["cpa"] = provider
    updated = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    saved_backup = commit_text(path, original, updated)
    return path, saved_backup, set(current_models), {model["id"] for model in merged}


def toml_key(value):
    return json.dumps(str(value), ensure_ascii=False)


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return toml_key(value)
    if isinstance(value, list) and all(isinstance(item, (str, int, bool)) for item in value):
        return "[ " + ", ".join(toml_value(item) for item in value) + " ]"
    raise RuntimeError(f"Unsupported TOML value: {type(value).__name__}")


def split_toml_key(text):
    parts = []
    buffer = ""
    quote = None
    escaped = False
    for char in text:
        if quote == '"':
            buffer += char
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif quote == "'":
            buffer += char
            if char == "'":
                quote = None
        elif char in ('"', "'"):
            quote = char
            buffer += char
        elif char == ".":
            parts.append(buffer.strip())
            buffer = ""
        else:
            buffer += char
    if quote:
        raise RuntimeError("Unterminated TOML key string")
    parts.append(buffer.strip())
    result = []
    for part in parts:
        if not part:
            raise RuntimeError("Empty TOML key component")
        if part.startswith('"'):
            result.append(json.loads(part))
        elif part.startswith("'") and part.endswith("'"):
            result.append(part[1:-1])
        elif re.fullmatch(r"[A-Za-z0-9_-]+", part):
            result.append(part)
        else:
            raise RuntimeError(f"Invalid TOML key component: {part}")
    return tuple(result)


def toml_table_path(line):
    stripped = line.strip()
    array = stripped.startswith("[[")
    if not stripped.startswith("["):
        return None
    quote = None
    escaped = False
    end = None
    for index, char in enumerate(stripped):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif quote == "'":
            if char == "'":
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "]":
            end = index
            break
    if end is None:
        raise RuntimeError("Unterminated TOML table header")
    closing = "]]" if array else "]"
    if not stripped.startswith(closing, end) or stripped[end + len(closing):].strip() and not stripped[end + len(closing):].strip().startswith("#"):
        raise RuntimeError("Invalid TOML table header")
    start = 2 if array else 1
    path = split_toml_key(stripped[start:end])
    return ("[]",) + path if array else path


def render_toml_table(path, fields):
    lines = ["[" + ".".join(part if re.fullmatch(r"[A-Za-z0-9_-]+", part) else toml_key(part) for part in path) + "]"]
    for key, value in fields.items():
        if value is not None:
            lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n\n"


def replace_toml_tables(text, replacements):
    lines = text.splitlines(keepends=True)
    headers = []
    for index, line in enumerate(lines):
        path = toml_table_path(line)
        if path is not None:
            headers.append((index, path))
    spans = []
    seen = set()
    for position, (start, path) in enumerate(headers):
        if path not in replacements:
            continue
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        spans.append((start, end, path))
        seen.add(path)
    for start, end, path in reversed(spans):
        replacement = replacements[path]
        lines[start:end] = [replacement] if replacement is not None else []
    updated = "".join(lines)
    missing = [replacement for path, replacement in replacements.items() if replacement is not None and path not in seen]
    if missing:
        if updated and not updated.endswith("\n"):
            updated += "\n"
        if updated and not updated.endswith("\n\n"):
            updated += "\n"
        updated += "".join(missing)
    return updated


def set_toml_scalars(text, values):
    lines = text.splitlines(keepends=True)
    first_table = next((index for index, line in enumerate(lines) if toml_table_path(line) is not None), len(lines))
    top = lines[:first_table]
    rest = lines[first_table:]
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}\s*=")
        start = next((index for index, line in enumerate(top) if pattern.match(line)), None)
        end = start + 1 if start is not None else None
        if start is not None:
            while end < len(top) and not pattern.match(top[end]) and top[end].strip() and not top[end].lstrip().startswith("#") and "=" not in top[end]:
                end += 1
        if start is not None:
            top[start:end] = [f"{key} = {toml_value(value)}\n"] if value is not None else []
        elif value is not None:
            top.append(f"{key} = {toml_value(value)}\n")
    if rest and top and top[-1].strip():
        top.append("\n")
    return "".join(top + rest)


def kimi_model(model):
    capabilities = model.get("capabilities") or {}
    reasoning = model.get("reasoning") or {}
    modalities = model.get("modalities") or {}
    inputs = modalities.get("input") or ["text"]
    tags = []
    if reasoning.get("supported"):
        tags.append("thinking")
        if not reasoning.get("supports_disable"):
            tags.append("always_thinking")
    for modality, tag in (("image", "image_in"), ("video", "video_in"), ("audio", "audio_in")):
        if modality in inputs:
            tags.append(tag)
    if capabilities.get("tools"):
        tags.append("tool_use")
    efforts = [effort for effort in (reasoning.get("efforts") or []) if effort != "none"]
    result = {
        "provider": "cpa",
        "model": model["id"],
        "max_context_size": model["limits"]["context"],
        "max_output_size": model["limits"]["max_output"],
        "capabilities": tags,
        "display_name": model.get("display_name") or model["id"],
    }
    if reasoning.get("supported") and efforts:
        result["support_efforts"] = efforts
    if reasoning.get("default") in efforts:
        result["default_effort"] = reasoning["default"]
    if reasoning.get("supports_disable") or "none" in (reasoning.get("efforts") or []):
        result["off_effort"] = "none"
    return result


def update_kimi(base_url, models, key_env, setup, prune):
    import tomllib
    kimi_home = Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code")))
    path = kimi_home / "config.toml"
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    document = tomllib.loads(original) if original.strip() else {}
    existing_models = document.get("models") or {}
    current = {mid for mid, model in existing_models.items() if model.get("provider") == "cpa"}
    existing_provider = (document.get("providers") or {}).get("cpa") or {}
    provider_fields = {key: value for key, value in existing_provider.items() if key not in ("type", "base_url", "api_key", "api_key_env", "env") and isinstance(value, (str, int, bool))}
    provider_fields.update({"type": "openai", "api_key_env": key_env})
    if setup or not existing_provider.get("base_url"):
        provider_fields["base_url"] = base_url
    else:
        provider_fields["base_url"] = existing_provider["base_url"]
    replacements = {("providers", "cpa"): render_toml_table(("providers", "cpa"), provider_fields)}
    generated = {}
    managed_fields = {"provider", "model", "protocol", "beta_api", "base_url", "overrides", "upgrade", "max_context_size", "max_output_size", "capabilities", "display_name", "support_efforts", "default_effort", "off_effort"}
    for model in models:
        existing = existing_models.get(model["id"]) or {}
        generated_fields = kimi_model(model)
        fields = {key: value for key, value in existing.items() if key not in managed_fields and isinstance(value, (str, int, bool, list))}
        fields.update(generated_fields)
        replacements[("models", model["id"])] = render_toml_table(("models", model["id"]), fields)
        generated[model["id"]] = fields
    if prune:
        for line in original.splitlines():
            table = toml_table_path(line)
            if table and len(table) >= 2 and table[0] == "models" and table[1] in current and table[1] not in generated:
                replacements[table] = None
    updated = replace_toml_tables(original, replacements)
    remaining = tomllib.loads(updated).get("models") or {}
    default_model = document.get("default_model")
    if default_model not in remaining:
        default_model = "xiaoye/auto" if "xiaoye/auto" in generated else next(iter(generated), None)
    updated = set_toml_scalars(updated, {"default_model": default_model})
    tomllib.loads(updated)
    saved_backup = commit_text(path, original, updated)
    merged = current | set(generated) if not prune else set(generated)
    return path, saved_backup, current, merged


def codex_model(model, existing=None):
    reasoning = model.get("reasoning") or {}
    modalities = model.get("modalities") or {}
    inputs = [value for value in (modalities.get("input") or ["text"]) if value in ("text", "image", "audio")]
    if not inputs:
        raise RuntimeError(f"Codex has no supported input modality for {model['id']}")
    efforts = [effort for effort in (reasoning.get("efforts") or []) if effort != "none"]
    supported = [{"effort": effort, "description": effort.replace("_", " ").capitalize()} for effort in efforts] if reasoning.get("supported") else []
    if reasoning.get("supported") and (reasoning.get("supports_disable") or "none" in (reasoning.get("efforts") or [])):
        supported.insert(0, {"effort": "none", "description": "Disable reasoning"})
    default = reasoning.get("default") if reasoning.get("supported") else None
    result = copy.deepcopy(existing or {})
    result.update({
        "slug": model["id"],
        "display_name": model.get("display_name") or model["id"],
        "base_instructions": result.get("base_instructions") or "Help the user with their coding tasks.",
        "default_reasoning_level": default,
        "supported_reasoning_levels": supported,
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "support_verbosity": False,
        "supports_reasoning_summary_parameter": False,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "context_window": model["limits"]["context"],
        "max_context_window": model["limits"]["context"],
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": inputs,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_service_tier": None,
        "upgrade": None,
    })
    return result


def update_codex(base_url, models, key_env, setup, prune, profile):
    import tomllib
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    profile_path = codex_home / f"{profile}.config.toml"
    catalog_path = codex_home / "cpa-models.json"
    original_catalog = catalog_path.read_text(encoding="utf-8") if catalog_path.exists() else ""
    catalog = json.loads(original_catalog) if original_catalog.strip() else {"models": []}
    current_models = {}
    for item in catalog.get("models") or []:
        if item.get("slug") in current_models:
            raise RuntimeError(f"Codex catalog has duplicate model slug: {item.get('slug')}")
        current_models[item.get("slug")] = item
    generated = [codex_model(model, current_models.get(model["id"])) for model in models]
    incoming = {model["slug"] for model in generated}
    merged_models = generated + [item for mid, item in current_models.items() if not prune and mid not in incoming]
    catalog["models"] = merged_models
    updated_catalog = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"

    original_profile = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""
    profile_document = tomllib.loads(original_profile) if original_profile.strip() else {}
    selected = profile_document.get("model")
    if selected not in incoming | set(current_models) or prune and selected not in incoming:
        selected = "xiaoye/auto" if "xiaoye/auto" in incoming else next(iter(incoming), None)
    if selected is None:
        raise RuntimeError("No CPA models available for Codex")
    selected_model = next(model for model in merged_models if model["slug"] == selected)
    supported_efforts = [item["effort"] for item in selected_model["supported_reasoning_levels"]]
    effort = profile_document.get("model_reasoning_effort")
    if effort not in supported_efforts:
        effort = selected_model.get("default_reasoning_level")
        if effort not in supported_efforts:
            effort = supported_efforts[0] if supported_efforts else None
    updated_profile = set_toml_scalars(original_profile, {
        "model": selected,
        "model_provider": "cpa",
        "model_catalog_json": str(catalog_path.resolve()),
        "model_reasoning_effort": effort,
        "web_search": "disabled",
    })
    replacements = {("model_providers", "cpa"): render_toml_table(("model_providers", "cpa"), {
        "name": "CPA",
        "base_url": base_url,
        "wire_api": "responses",
        "env_key": key_env,
    })}
    for line in original_profile.splitlines():
        table = toml_table_path(line)
        if table and len(table) > 2 and table[:3] == ("model_providers", "cpa", "auth"):
            replacements[table] = None
    updated_profile = replace_toml_tables(updated_profile, replacements)
    tomllib.loads(updated_profile)
    catalog_backup = commit_text(catalog_path, original_catalog, updated_catalog)
    profile_backup = commit_text(profile_path, original_profile, updated_profile)
    backups = [str(item) for item in (catalog_backup, profile_backup) if item]
    return profile_path, "; ".join(backups) or None, set(current_models), {model["slug"] for model in merged_models}


def reload_opencode(verify, restart=False):
    executable = shutil.which("opencode.cmd") or shutil.which("opencode")
    if not executable:
        raise RuntimeError("opencode executable not found")
    command = [executable, "service", "restart"] if restart else [executable, "reload"]
    subprocess.run(command, check=True, timeout=30)
    if verify:
        subprocess.run([executable, "debug", "config"], check=True, timeout=30, stdout=subprocess.DEVNULL)


def run(args):
    setup = args.command == "setup"
    if setup:
        base_url = (args.base_url or get_user_env("CPA_BASE_URL") or "").rstrip("/")
        if not base_url:
            raise RuntimeError("Set CPA_BASE_URL or pass --base-url")
        key_env = args.key_env
        api_key = get_user_env(key_env)
        if not api_key:
            api_key = getpass.getpass(f"{key_env}: ").strip()
        if not api_key:
            raise RuntimeError("API key is required")
        set_user_env(key_env, api_key)
        set_user_env("CPA_BASE_URL", base_url)
        save_json(SETTINGS_PATH, {"base_url": base_url, "key_env": key_env, "target": args.target})
    else:
        settings = read_settings()
        base_url = settings["base_url"].rstrip("/")
        key_env = settings.get("key_env", "CPA_API_KEY")
        api_key = get_user_env(key_env)
        if not api_key:
            raise RuntimeError(f"Environment variable {key_env} is not set")
    os.environ[key_env] = api_key
    request_json(base_url, api_key, "/models")
    document = request_json(base_url, api_key, "/capabilities")
    models = validate_capabilities(document)
    save_json(CACHE_PATH, document)
    if args.target == "opencode":
        saved_backup, current, merged = update_opencode(base_url, models, key_env, setup, args.prune)
        target_name = "OpenCode"
    elif args.target == "dsh":
        profile = args.profile or "web"
        target_path, saved_backup, current, merged = update_dsh(base_url, models, key_env, setup, args.prune, profile)
        target_name = f"DSH profile {profile}"
    elif args.target == "pi":
        target_path, saved_backup, current, merged = update_pi(base_url, models, key_env, setup, args.prune)
        target_name = "Pi"
    elif args.target == "kimi":
        target_path, saved_backup, current, merged = update_kimi(base_url, models, key_env, setup, args.prune)
        target_name = "Kimi Code"
    else:
        profile = args.profile or "cpa"
        target_path, saved_backup, current, merged = update_codex(base_url, models, key_env, setup, args.prune, profile)
        target_name = f"Codex profile {profile}"
    incoming = {model["id"] for model in models}
    added = sorted(incoming - set(current))
    stale = sorted(set(current) - incoming)
    print(f"Synced {len(models)} CPA models for {target_name}")
    if added:
        print("Added: " + ", ".join(added))
    if stale:
        action = "removed" if args.prune else "kept (use --prune to remove)"
        print("Not returned by server, " + action + ": " + ", ".join(stale))
    print("Backup: " + (str(saved_backup) if saved_backup else "not needed; no changes"))
    if not args.no_reload:
        if args.target == "opencode":
            reload_opencode(args.verify, restart=setup)
        elif args.verify and args.target == "dsh":
            profile = args.profile or "web"
            if profile == "desktop":
                print("DSH desktop profile YAML valid; reload is managed by Electron")
            else:
                executable = shutil.which("dsh.cmd") or shutil.which("dsh")
                if not executable:
                    raise RuntimeError("dsh executable not found")
                subprocess.run([executable, "--profile", profile, "--dump-config"], check=True, timeout=60, stdout=subprocess.DEVNULL)
        elif args.verify and args.target == "pi":
            executable = shutil.which("pi")
            if not executable:
                raise RuntimeError("pi executable not found")
            subprocess.run([executable, "--offline", "--list-models", "cpa"], check=True, timeout=60, stdout=subprocess.DEVNULL)
        elif args.verify and args.target == "kimi":
            executable = shutil.which("kimi")
            if not executable:
                raise RuntimeError("kimi executable not found")
            subprocess.run([executable, "doctor", "config"], check=True, timeout=60, stdout=subprocess.DEVNULL)
        elif args.verify and args.target == "codex":
            executable = shutil.which("codex") or shutil.which("codex.cmd")
            if executable:
                subprocess.run([executable, "--profile", args.profile or "cpa", "debug", "models"], check=True, timeout=60, stdout=subprocess.DEVNULL)
            else:
                print("Codex CLI not found; TOML and catalog JSON validated. Restart Codex to load the profile.")
    if args.target == "codex":
        print(f"Use: codex --profile {args.profile or 'cpa'}")
    print(target_name + " configuration valid" if args.verify else "Sync complete")


def main():
    parser = argparse.ArgumentParser(prog="cpa-sync", description="Synchronize CPA model capabilities")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "sync"):
        sub = subparsers.add_parser(name)
        sub.add_argument("target", choices=["opencode", "dsh", "pi", "kimi", "codex"])
        sub.add_argument("--profile", help="DSH profile (default: web) or Codex profile (default: cpa)")
        sub.add_argument("--prune", action="store_true")
        sub.add_argument("--no-reload", action="store_true")
        sub.add_argument("--verify", action="store_true")
        if name == "setup":
            sub.add_argument("--base-url")
            sub.add_argument("--key-env", default="CPA_API_KEY")
    args = parser.parse_args()
    try:
        run(args)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"cpa-sync: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
