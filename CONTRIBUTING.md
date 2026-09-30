# Contributing to PromptPulse

Thanks for helping improve PromptPulse.

## Simple workflow

1. Create a fork on GitHub.
2. Clone your fork and create a branch for your change:

   ```bash
   git clone <YOUR-FORK-URL> PromptPulse
   cd PromptPulse
   git switch -c describe-your-change
   ```

3. Make a small, focused change.
4. Run the tests:

   ```bash
   python3 -B -m unittest discover -s tests -v
   ```

5. Commit your change and push the branch.
6. Open a pull request and explain what changed and how you tested it.

## Before opening a pull request

- Do not commit `config.json`, `data/`, `monitor.db`, credentials, private IP inventories, or installation reports containing machine details.
- Keep the default network behavior private. Do not add wildcard or public binds.
- Keep NVIDIA support optional and report unavailable hardware as unavailable rather than zero.
- Preserve the standard-library-only design unless a dependency is clearly necessary.
- Update the README or [SETUP_PROMPT.md](SETUP_PROMPT.md) when configuration behavior changes.

For a bug report, include the operating system, Python version, configuration mode (`tailscale`, private IPv4, or loopback), and relevant sanitized logs. Do not include secrets or public links to a private dashboard.
