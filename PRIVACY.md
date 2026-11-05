# Privacy notice

Last reviewed: 2026-11-04

This notice describes the behavior visible in the public source code. It is not a guarantee about third-party services, downloaded model files, or future versions. Review the source and release notes for the exact version you install.

## Data stored on your device

The application stores its configuration and local application data under `%APPDATA%\\LocalAIManager` on Windows. Depending on how you use it, this can include:

- selected model paths and library metadata;
- interface and launch settings;
- locally created agent prompts and skills;
- logs and temporary download files;
- downloaded models and engine files.

These files can reveal local usernames, directory names, model choices, prompts, or hardware details. Do not publish your application data folder or diagnostic logs without reviewing them.

## Network connections

Network access is used for user-requested or user-visible features:

| Destination | Purpose | Data that may be sent |
|---|---|---|
| `huggingface.co` | Search repositories and download selected files | Search terms, repository/file identifiers, ordinary request metadata; an HF access token is sent only when the user supplies one for gated/private content |
| `api.github.com` / GitHub release pages | Check/download supported engine releases | Release metadata requests; download requests for selected engine assets |
| `pypi.org` | Check package version metadata used by engine/dependency checks | Package names and ordinary request metadata |
| Official Ollama download host | Optional Ollama installer download | Download request metadata |
| `download.pytorch.org` | Optional CPU-only PyTorch wheel installation path | Package download requests
| Local configured inference endpoint | Chat and agent inference | The prompt, conversation context and generation parameters supplied by the user |

The application does not need to send chat history to the project maintainer. Chat requests are sent to the configured inference endpoint. The normal intended setup is a local endpoint on the user's own computer. If you configure a remote endpoint, its operator may receive prompts and other request data.

## Tokens and credentials

A Hugging Face token may be entered to access gated/private repositories. The current code removes legacy `hf_token` values from the application configuration during migration and does not intentionally save the token in the normal app config. Treat the token as a secret regardless. Do not paste it into Issues, logs, screenshots, or public repositories. Revoke it at Hugging Face if exposed.

## Telemetry

A source review for this release did not identify built-in advertising analytics, crash-report upload, or automatic transmission of chat history to the maintainer. This is a code-review finding, not a formal network-traffic certification. Third-party services contacted by explicit features have their own privacy policies.

## Optional local automation bridge

The local TCP bridge is disabled by default. If deliberately enabled with `LAM_AI_BRIDGE=1`, it listens on loopback and writes a state file containing its port, process ID, and random access token to the user's home directory. The `eval` command is separately disabled unless `LAM_AI_BRIDGE_ALLOW_EVAL=1` is set. Enabling this bridge grants local automation capabilities; do not enable it on shared or untrusted machines.

## Third-party software and models

Models and engine binaries are obtained from their respective publishers. Their licenses, security, behavior, and privacy terms are separate from this application. Verify model and engine sources before downloading or running them.
