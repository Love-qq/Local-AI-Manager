# Contributing

1. Open an issue before large changes so the scope can be discussed.
2. Keep changes focused and add regression tests for bug fixes.
3. Do not commit model weights, credentials, user configuration, logs, or machine-specific paths.
4. Run `python -m compileall -q local_ai_manager.py ai_bridge.py lamcore` and `python -m pytest -q` on Windows where possible.
5. Include Windows version, Python version, steps to reproduce, and redacted logs in bug reports.
6. Do not claim a feature is supported unless its engine path is implemented and tested.

By submitting a contribution, you confirm you have the right to submit it. Contributions are accepted under the project's [MIT License](LICENSE).
