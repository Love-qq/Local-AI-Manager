# Security policy

## Supported versions

Security fixes are applied to the latest source version and future releases. Older packaged builds may not receive fixes; update to the newest published release.

## Reporting a vulnerability

Please do not report exploitable vulnerabilities in a public GitHub Issue.

Use GitHub's **Report a vulnerability** feature (Security → Advisories) if private vulnerability reporting is enabled for this repository. Otherwise contact the repository maintainer through a private channel listed in the GitHub profile. Include:

- affected version/commit and operating system;
- steps to reproduce and expected impact;
- a minimal proof of concept that does not access other people's data;
- any suggested mitigation.

Do not include real access tokens, private model files, personal documents, or unredacted logs.

## Current security notes

- The optional automation bridge is disabled by default.
- The bridge binds to `127.0.0.1`, uses a random token, and has a separate opt-in for arbitrary Python evaluation.
- Downloads use HTTPS endpoints and integrity checks where supported by the upstream metadata. Not every upstream engine release currently has a locally verified publisher signature or checksum.
- The application can launch downloaded engine installers/binaries. Only use trusted upstream sources and review prompts before confirming installation.
- Hugging Face tokens are credentials. Keep them out of source control and public diagnostics.

## Scope

This policy covers the Local AI Manager source and official release artifacts. It does not cover vulnerabilities in downloaded models, llama.cpp, Ollama, OpenCode, Python, Windows, or other third-party components.
