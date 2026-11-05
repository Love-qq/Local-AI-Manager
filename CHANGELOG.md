# Changelog

## 48.1.0

### Fixed
- The native Windows title bar is now removed reliably. The window is borderless (`overrideredirect`) with its own taskbar button, native minimize/restore, work-area-aware maximize and invisible resize edges. The previous `WS_CAPTION` approach left a system title bar and black/duplicated redraw regions.
- Main monitor never updated after a model started: `MainMonitorPanel._refresh_body` contained a copy of the dashboard code that raised `AttributeError` on every tick. It now shows process/API state, RAM, VRAM, uptime and context usage.
- The header **Stop** button was never enabled; it now follows the server process state.
- Cards were clipped at the bottom (for example *OpenCode and agents*) when the window was shorter than the content; stretch panels now keep their natural minimum height.
- Dialogs (chat, Hugging Face, agents, dashboard) grow to fit their content instead of cutting off the last row at higher DPI/font scales.
- The Hugging Face dialog no longer shows an empty scrollbar when everything fits.
- Chat: the input area and hint line are no longer pushed out of the window.

### Improved
- Subtle accent gradient line and a thin accent border on all frameless windows.
- Rounded, colour-coded usage bars in the monitor (RAM turns amber/red under pressure); the launch estimate bar follows the verdict colour.
- Chat: empty-state hint, separate small role labels, clearer message hierarchy.
- Screenshots in the README; MIT license; CI test workflow.

## 48.0.0

- Local model library and Hugging Face repository/file browser.
- Streaming model downloads with resume and available integrity checks.
- Model metadata inspection and RAM/VRAM/context compatibility estimates.
- llama.cpp engine management and local server controls.
- OpenCode configuration export.
- Local chat, agents, and skills.
- Russian and English interface.
- Windows portable and installer build workflows.
- Security hardening: optional local automation bridge disabled by default; arbitrary Python evaluation requires a separate explicit opt-in.

This is a source-level changelog. Verify Windows artifacts and manual GUI behavior before declaring a stable release.
