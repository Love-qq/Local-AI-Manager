import subprocess

import local_ai_manager as app


def test_gui_subprocess_never_inherits_stdin():
    kwargs = app.hidden_process_kwargs()
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_windows_subprocesses_are_hidden_without_console():
    if app.IS_WINDOWS:
        kwargs = app.hidden_process_kwargs()
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert "startupinfo" in kwargs


def test_powershell_and_nvidia_use_shared_safe_subprocess_defaults():
    source = open(app.__file__, encoding="utf-8").read()
    assert 'capture_output=True, text=True, timeout=timeout, **hidden_process_kwargs()' in source
    assert 'timeout=3, **hidden_process_kwargs()' in source
