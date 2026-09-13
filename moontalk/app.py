from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from wechat_bridge import WeChatBridge

ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT / "settings.json"
PLUGINS_DIR = ROOT / "plugins"
app = Flask(__name__, static_folder=None)
CORS(app)

_history: list[dict[str, str]] = []
_HISTORY_LIMIT = 30
_HISTORY_CHARS = 12000
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "kimi": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
    "ollama": "http://localhost:11434",
}


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"providers": {}, "models": {}, "roles": [], "wechat": {}}


def save_settings(data: dict):
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def trim_history():
    global _history
    _history = _history[-_HISTORY_LIMIT:]
    while sum(len(item["content"]) for item in _history) > _HISTORY_CHARS:
        _history.pop(0)


def resolve_model(settings: dict, capability: str) -> tuple[dict, dict]:
    binding = (settings.get("models") or {}).get(capability) or {}
    ref = binding.get("provider_ref") or ""
    provider = (settings.get("providers") or {}).get(ref) or {}
    model_id = binding.get("model") or ""
    return provider, {"id": model_id, "capability": capability}


def provider_endpoint(provider: dict, path: str = "/chat/completions") -> str:
    return (provider.get("base_url") or "").rstrip("/") + path


def call_openai_compatible(provider: dict, model: str, messages: list, images: list[str] | None = None) -> str:
    content: Any = messages[-1]["content"] if messages else ""
    if images:
        content = [{"type": "text", "text": str(content)}]
        content.extend({"type": "image_url", "image_url": {"url": image}} for image in images)
        messages = [*messages[:-1], {"role": "user", "content": content}]
    payload = {"model": model, "messages": messages, "max_tokens": 1200, "temperature": 0.7}
    headers = {"Content-Type": "application/json"}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    response = requests.post(provider_endpoint(provider), json=payload, headers=headers, timeout=120)
    response.raise_for_status()
    data = response.json()
    return str(data["choices"][0]["message"].get("content") or "").strip()


def call_anthropic(provider: dict, model: str, messages: list) -> str:
    system = ""
    cleaned = []
    for item in messages:
        if item["role"] == "system":
            system = item["content"]
        else:
            cleaned.append({"role": item["role"], "content": item["content"]})
    response = requests.post(
        provider_endpoint(provider, "/v1/messages"),
        headers={"x-api-key": provider.get("api_key", ""), "anthropic-version": "2023-06-01"},
        json={"model": model, "system": system, "max_tokens": 1200, "messages": cleaned},
        timeout=120,
    )
    response.raise_for_status()
    blocks = response.json().get("content") or []
    return "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text").strip()


def call_gemini(provider: dict, model: str, messages: list) -> str:
    parts = []
    for item in messages:
        parts.append({"text": f"{item['role']}: {item['content']}"})
    url = f"{provider.get('base_url', '').rstrip('/')}/{model}:generateContent"
    response = requests.post(url, params={"key": provider.get("api_key", "")}, json={"contents": [{"parts": parts}]}, timeout=120)
    response.raise_for_status()
    return str(response.json()["candidates"][0]["content"]["parts"][0].get("text", "")).strip()


def call_model(provider: dict, model: str, messages: list, images: list[str] | None = None) -> str:
    kind = (provider.get("provider") or "").lower()
    if kind == "anthropic":
        return call_anthropic(provider, model, messages)
    if kind == "gemini":
        return call_gemini(provider, model, messages)
    if kind == "ollama":
        payload = {"model": model, "messages": messages, "stream": False}
        response = requests.post(provider_endpoint(provider, "/api/chat"), json=payload, timeout=120)
        response.raise_for_status()
        return str(response.json().get("message", {}).get("content", "")).strip()
    return call_openai_compatible(provider, model, messages, images)


def describe_images(settings: dict, images: list[str], question: str) -> str:
    provider, model = resolve_model(settings, "vision")
    if not provider or not model.get("id"):
        return ""
    prompt = question or "请用中文简洁描述图片中的主要内容、文字和界面。"
    return call_model(provider, model["id"], [{"role": "user", "content": prompt}], images)


def role_prompt(settings: dict) -> str:
    selected = settings.get("selected_role") or "default"
    for role in settings.get("roles") or []:
        if role.get("id") == selected:
            return role.get("prompt") or ""
    return settings.get("system_prompt") or "你是一个可靠的中文助手。"


def discover_plugins() -> list[dict]:
    result = []
    PLUGINS_DIR.mkdir(exist_ok=True)
    for directory in sorted(PLUGINS_DIR.iterdir()):
        if not directory.is_dir() or directory.name.startswith("_"):
            continue
        meta = {}
        for filename in ("metadata.yaml", "plugin.json"):
            path = directory / filename
            if not path.exists():
                continue
            try:
                if filename.endswith(".json"):
                    meta = json.loads(path.read_text(encoding="utf-8"))
                else:
                    import yaml
                    meta = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                break
            except Exception as exc:
                meta = {"error": str(exc)}
        schema_path = directory / "_conf_schema.json"
        config_path = PLUGINS_DIR / "_configs" / f"{directory.name}.json"
        result.append({
            "id": directory.name,
            "name": meta.get("name", directory.name),
            "description": meta.get("desc", meta.get("description", "")),
            "version": meta.get("version", ""),
            "author": meta.get("author", ""),
            "enabled": meta.get("enabled", True),
            "has_schema": schema_path.exists(),
            "config": json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {},
        })
    return result


def ask_chat(message: str, images: list[str] | None = None) -> str:
    settings = load_settings()
    vision_text = describe_images(settings, images or [], message) if images else ""
    current = message
    if vision_text:
        current += f"\n\n[图片内容]\n{vision_text}"
    provider, model = resolve_model(settings, "chat")
    if not provider or not model["id"]:
        raise RuntimeError("请先在模型页绑定语言对话模型")
    messages = [{"role": "system", "content": role_prompt(settings)}]
    messages.extend(_history)
    messages.append({"role": "user", "content": current})
    reply = call_model(provider, model["id"], messages)
    _history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply}])
    trim_history()
    return reply


def describe_wechat_media(path: str, content_type: str, label: str) -> str:
    image_data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    image_url = f"data:{content_type};base64,{image_data}"
    prompt = f"请用中文简洁描述这个微信{label}的主体、动作、文字和表达的情绪，供语言模型理解并回复用户。"
    return describe_images(load_settings(), [image_url], prompt)


def answer_wechat_message(text: str, _event: dict) -> str:
    return ask_chat(text)


bridge = WeChatBridge(on_message=answer_wechat_message, on_vision=describe_wechat_media)


@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/api/settings")
def api_get_settings():
    return jsonify(load_settings())


@app.post("/api/settings")
def api_save_settings():
    data = request.get_json(silent=True) or {}
    save_settings(data)
    return jsonify({"ok": True, "settings": data})


@app.get("/api/providers")
def api_providers():
    return jsonify({"providers": load_settings().get("providers", {})})


@app.post("/api/providers")
def api_add_provider():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "提供商名称不能为空"}), 400
    settings = load_settings()
    provider_type = (payload.get("provider") or "openai").lower()
    settings.setdefault("providers", {})[name] = {
        "name": name,
        "provider": provider_type,
        "api_key": payload.get("api_key", ""),
        "base_url": payload.get("base_url") or PROVIDER_BASE_URLS.get(provider_type, ""),
        "models": payload.get("models", []),
    }
    save_settings(settings)
    return jsonify({"ok": True, "provider": settings["providers"][name]})


@app.delete("/api/providers/<name>")
def api_delete_provider(name: str):
    settings = load_settings()
    settings.get("providers", {}).pop(name, None)
    for binding in (settings.get("models") or {}).values():
        if binding.get("provider_ref") == name:
            binding.update({"provider_ref": "", "model": ""})
    save_settings(settings)
    return jsonify({"ok": True})


@app.post("/api/providers/<name>/models")
def api_fetch_models(name: str):
    settings = load_settings()
    provider = settings.get("providers", {}).get(name)
    if not provider:
        return jsonify({"ok": False, "error": "提供商不存在"}), 404
    kind = provider.get("provider", "")
    try:
        if kind == "ollama":
            response = requests.get(provider_endpoint(provider, "/api/tags"), timeout=30)
            models = [{"id": item["name"], "name": item["name"]} for item in response.json().get("models", [])]
        else:
            headers = {"Authorization": f"Bearer {provider.get('api_key', '')}"}
            response = requests.get(provider_endpoint(provider, "/models"), headers=headers, timeout=30)
            response.raise_for_status()
            models = [{"id": item.get("id", ""), "name": item.get("id", "")} for item in response.json().get("data", [])]
        provider["models"] = [model for model in models if model["id"]]
        save_settings(settings)
        return jsonify({"ok": True, "models": provider["models"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/api/models/bind")
def api_bind_model():
    payload = request.get_json(silent=True) or {}
    capability = payload.get("capability")
    if capability not in {"chat", "vision"}:
        return jsonify({"ok": False, "error": "未知模型能力"}), 400
    settings = load_settings()
    settings.setdefault("models", {})[capability] = {
        "provider_ref": payload.get("provider_ref", ""),
        "model": payload.get("model", ""),
    }
    save_settings(settings)
    return jsonify({"ok": True, "models": settings["models"]})


@app.get("/api/chat/history")
def api_history():
    return jsonify({"ok": True, "count": len(_history), "history": _history})


@app.delete("/api/chat/history")
def api_clear_history():
    _history.clear()
    return jsonify({"ok": True})


@app.get("/api/roles")
def api_roles():
    settings = load_settings()
    return jsonify({"roles": settings.get("roles", []), "selected_role": settings.get("selected_role", "default")})


@app.post("/api/roles")
def api_save_role():
    payload = request.get_json(silent=True) or {}
    role_id = re.sub(r"[^a-zA-Z0-9_-]", "-", str(payload.get("id") or payload.get("name") or "role").strip()).strip("-") or "role"
    role = {"id": role_id, "name": str(payload.get("name") or role_id).strip(), "prompt": str(payload.get("prompt") or "").strip()}
    if not role["prompt"]:
        return jsonify({"ok": False, "error": "角色设定不能为空"}), 400
    settings = load_settings()
    roles = settings.setdefault("roles", [])
    roles[:] = [item for item in roles if item.get("id") != role_id]
    roles.append(role)
    if payload.get("selected"):
        settings["selected_role"] = role_id
    save_settings(settings)
    return jsonify({"ok": True, "role": role, "selected_role": settings.get("selected_role", "default")})


@app.post("/api/roles/select")
def api_select_role():
    role_id = str((request.get_json(silent=True) or {}).get("id") or "")
    settings = load_settings()
    if not any(item.get("id") == role_id for item in settings.get("roles", [])):
        return jsonify({"ok": False, "error": "角色卡不存在"}), 404
    settings["selected_role"] = role_id
    save_settings(settings)
    return jsonify({"ok": True, "selected_role": role_id})


@app.delete("/api/roles/<role_id>")
def api_delete_role(role_id: str):
    settings = load_settings()
    roles = settings.setdefault("roles", [])
    if role_id == "default":
        return jsonify({"ok": False, "error": "默认角色不可删除"}), 400
    settings["roles"] = [item for item in roles if item.get("id") != role_id]
    if settings.get("selected_role") == role_id:
        settings["selected_role"] = "default"
    save_settings(settings)
    return jsonify({"ok": True, "selected_role": settings.get("selected_role", "default")})


@app.post("/api/chat")
def api_chat():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    images = payload.get("images") or []
    if not message and not images:
        return jsonify({"ok": False, "error": "请输入消息或上传图片"}), 400
    try:
        reply = ask_chat(message or "请描述这张图片", images)
        return jsonify({"ok": True, "reply": reply, "history_count": len(_history), "vision_used": bool(images)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/plugins")
def api_plugins():
    return jsonify({"ok": True, "plugins": discover_plugins()})


@app.post("/api/plugins/<plugin_id>/toggle")
def api_toggle_plugin(plugin_id: str):
    plugin_dir = PLUGINS_DIR / plugin_id
    if not plugin_dir.is_dir():
        return jsonify({"ok": False, "error": "插件不存在"}), 404
    manifest_path = plugin_dir / "plugin.json"
    meta = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"name": plugin_id}
    meta["enabled"] = bool((request.get_json(silent=True) or {}).get("enabled", True))
    manifest_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "plugin": meta})


@app.get("/api/plugins/<plugin_id>/schema")
def api_plugin_schema(plugin_id: str):
    path = PLUGINS_DIR / plugin_id / "_conf_schema.json"
    if not path.exists():
        return jsonify({"ok": True, "schema": {}})
    return jsonify({"ok": True, "schema": json.loads(path.read_text(encoding="utf-8"))})


@app.post("/api/plugins/<plugin_id>/config")
def api_plugin_config(plugin_id: str):
    config_dir = PLUGINS_DIR / "_configs"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / f"{plugin_id}.json"
    data = request.get_json(silent=True) or {}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/api/wechat/status")
def api_wechat_status():
    return jsonify(bridge.status())


@app.post("/api/wechat/config")
def api_wechat_config():
    settings = load_settings()
    settings["wechat"] = request.get_json(silent=True) or {}
    save_settings(settings)
    return jsonify({"ok": True, "wechat": settings["wechat"]})


@app.post("/api/wechat/start")
def api_wechat_start():
    return jsonify(bridge.start(load_settings().get("wechat") or {}))


@app.post("/api/wechat/stop")
def api_wechat_stop():
    return jsonify(bridge.stop())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8010, debug=False, threaded=True)
