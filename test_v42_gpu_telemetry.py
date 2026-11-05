import subprocess
import local_ai_manager as appmod


def test_gpu_system_stats_parses_driver_and_telemetry(monkeypatch):
    monkeypatch.setattr(appmod, "find_nvidia_smi", lambda: "nvidia-smi")
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=a[0], returncode=0,
        stdout="NVIDIA GeForce RTX 3060, 560.94, 42, 6144, 12288, 63, 95.20\n"))
    stats = appmod.gpu_system_stats()
    assert stats[0]["name"] == "NVIDIA GeForce RTX 3060"
    assert stats[0]["driver"] == "560.94"
    assert stats[0]["util"] == "42"
    assert stats[0]["used"] == "6144"


def test_gpu_system_stats_returns_none_when_command_fails(monkeypatch):
    monkeypatch.setattr(appmod, "find_nvidia_smi", lambda: "nvidia-smi")
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=a[0], returncode=1, stdout="", stderr="unsupported"))
    assert appmod.gpu_system_stats() is None


def test_find_nvidia_smi_uses_path_first(monkeypatch):
    monkeypatch.setattr(appmod.shutil, "which", lambda name: r"C:\\NVIDIA\\nvidia-smi.exe")
    assert appmod.find_nvidia_smi().endswith("nvidia-smi.exe")
