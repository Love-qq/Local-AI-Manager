import json

from local_ai_manager import upsert_opencode_local_provider, remove_opencode_local_model


def test_stable_opencode_provider_uses_documented_v1_shape():
    config = {"provider": {"other": {"npm": "x"}}}
    schema = upsert_opencode_local_provider(config, 8080, "qwen-local", "Qwen 7B", 8192)
    assert schema == "stable"
    provider = config["provider"]["local"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert "package" not in provider
    assert provider["models"]["qwen-local"]["limit"]["output"] >= 4096
    assert "other" in config["provider"]


def test_v2_opencode_provider_preserves_v2_shape():
    config = {"providers": {"other": {"package": "x"}}}
    schema = upsert_opencode_local_provider(config, 9000, "model-a", "Model A", 16384)
    assert schema == "V2"
    provider = config["providers"]["local"]
    assert provider["package"] == "@opencode/ai/providers/openai-compatible"
    assert provider["settings"]["baseURL"] == "http://127.0.0.1:9000/v1"
    assert "npm" not in provider
    assert provider["models"]["model-a"]["modelID"] == "model-a"
    assert "other" in config["providers"]


def test_remove_model_uses_existing_schema_and_keeps_provider():
    config = {"provider": {"local": {"models": {"a": {}, "b": {}}}}}
    assert remove_opencode_local_model(config, "a")
    assert list(config["provider"]["local"]["models"]) == ["b"]


def test_jsonc_trailing_comma_removal_does_not_change_strings():
    source = '{"text": "comma,}", "arr": [1, 2,], // comment\n "obj": {"x": 1,},}'
    parsed = json.loads(__import__("local_ai_manager").LocalAIManager.parse_jsonc(source))
    assert parsed["text"] == "comma,}"
    assert parsed["arr"] == [1, 2]
    assert parsed["obj"] == {"x": 1}


def test_chat_history_trimming_starts_with_user():
    from local_ai_manager import LocalChatWindow
    history = [
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    trimmed = LocalChatWindow._trim_history(history, max_messages=3)
    assert trimmed[0]["role"] == "user"
