import json
import tempfile
from pathlib import Path

import local_ai_manager as lam


class _Log:
    def write_log(self, _text):
        pass


def test_write_opencode_config_really_writes_file():
    folder = Path(tempfile.mkdtemp())
    target = folder / "opencode.json"
    target.write_text("{}", encoding="utf-8")
    lam.LocalAIManager.write_opencode_config(_Log(), target, {"a": 1})
    raw = target.read_bytes().decode("utf-8")
    assert json.loads(raw) == {"a": 1}
    assert raw.endswith("}\n") and "\\n" not in raw
    assert len(list(folder.glob("opencode.json.bak-*"))) == 1
    assert not (folder / "opencode.json.tmp").exists()


def test_jsonc_escaped_quote_with_comma_inside_string():
    source = '{"a": "he said \\"hi, }\\"", "b": [1,],}'
    parsed = json.loads(lam.LocalAIManager.parse_jsonc(source))
    assert parsed["a"] == 'he said "hi, }"'
    assert parsed["b"] == [1]


def test_chat_placeholder_marker_uses_real_newlines():
    import inspect
    source = inspect.getsource(lam.LocalChatWindow)
    assert "\\\\n" not in source.replace("\\\\\\\\", "")


def test_merge_gpu_vram_prefers_nvidia_then_registry_and_keeps_igpu():
    gpus = [{"name": "NVIDIA GeForce RTX 4070", "vram": 4.0},
            {"name": "Intel(R) UHD Graphics", "vram": 1.0},
            {"name": "AMD Radeon RX 7800 XT", "vram": 4.0}]
    nvidia = [{"name": "NVIDIA GeForce RTX 4070", "total": "12282"}]
    registry = [("AMD Radeon RX 7800 XT", 16 * 1024 ** 3)]
    result = lam.merge_gpu_vram(gpus, nvidia, registry)
    assert [g["name"] for g in result][1] == "Intel(R) UHD Graphics"
    assert result[0]["vram"] == 12.0
    assert result[1]["vram"] == 1.0
    assert result[2]["vram"] == 16.0


def test_parse_registry_gpu_rows_skips_garbage():
    rows = lam._parse_registry_gpu_rows(["AMD GPU|17163091968", "bad line", "X|0", "Y|abc"])
    assert rows == [("AMD GPU", 17163091968)]


def test_empty_config_uses_v2_and_v1_only_stays_v1():
    assert lam.opencode_root_key({}) == "providers"
    assert lam.opencode_root_key({"provider": {"x": {}}}) == "provider"
    assert lam.opencode_root_key({"providers": {}}) == "providers"
    config = {}
    assert lam.upsert_opencode_local_provider(config, 8080, "m", "M", 4096) == "V2"
    assert config["providers"]["local"]["package"] == "@opencode/ai/providers/openai-compatible"
