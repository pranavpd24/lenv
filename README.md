# LENV — Linux Environments for Developers

Project-scoped Linux environments with `venv`-like UX, for Windows developers.

`python -m venv` gives every Python project its own interpreter. LENV gives every
project its own Linux: a lightweight, disposable WSL2 instance that belongs to one
project directory, created and destroyed with a single command.

[![PyPI version](https://img.shields.io/pypi/v/lenv-manager.svg)](https://pypi.org/project/lenv-manager/)
[![Python](https://img.shields.io/pypi/pyversions/lenv-manager.svg)](https://pypi.org/project/lenv-manager/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Why LENV?

- **venv-like UX** — `lenv init`, `lenv activate`, `lenv destroy`. If you know venv, you know LENV.
- **Per-project isolation** — each project directory gets its own WSL2 instance, not a shared distro.
- **Lightweight** — Alpine rootfs is ~3 MB; instances start in seconds. No Docker Desktop required.
- **Zero dependencies** — pure Python 3.8+ standard library. Install it with `pip`, nothing else.

## Requirements

- Windows 10/11
- WSL2 (LENV detects it and can guide you through installation on first run)
- Python 3.8+

## Installation

```bash
pip install lenv-manager
```

From source, for development:

```bash
git clone https://github.com/pranavpd24/lenv.git
cd lenv
pip install -e .
```

## Quick Start

```powershell
cd your-project
lenv init                 # pick Alpine or Ubuntu, done
lenv activate             # you're in Linux, in your project directory
python3 --version         # Linux Python!
exit                      # back to Windows
lenv destroy              # remove the environment completely
```

## Commands

| Command | Description |
|---|---|
| `lenv init` | Create an environment for the current project |
| `lenv init --distro alpine` | Skip the prompt, use Alpine (or `ubuntu`) |
| `lenv init --rootfs PATH` | Use your own rootfs tarball (`.tar`, `.tar.gz`, `.tar.xz`) |
| `lenv activate` | Open an interactive Linux shell in the project directory |
| `lenv run <cmd>` | Run a single command inside the environment |
| `lenv status` | Show environment state for the current project |
| `lenv list` | List every lenv environment on this machine |
| `lenv destroy` | Remove the environment (instance + config) |
| `lenv --version` | Print the installed version |

## How it works

- **One WSL2 instance per project.** The instance name is derived from the project
  path (`lenv-<folder>-<hash>`), so the same directory always maps to the same
  environment, and different projects never collide.
- **Minimal rootfs, cached.** The Alpine (~3 MB) or Ubuntu (~50 MB) root filesystem
  is downloaded once to `~/.lenv/rootfs` and reused across projects.
- **Isolated networking.** Each instance gets its own virtual interface and a stable
  IP in `10.100.0.0/16`, attached to a shared `lenv-br0` bridge with NAT, so every
  environment can reach the internet and be reached from Windows by IP.
- **Project-local config.** Environment metadata lives in `.lenv/config.json` inside
  the project (auto-added to `.gitignore`), so a project is self-describing.

Note on isolation: filesystems and processes are fully per-instance. The network
bridge is shared between lenv instances (they can ping each other), which is what
makes cross-project tooling possible — separate IPs, not separate network namespaces.

## Contributing

Issues and pull requests are welcome. The codebase is intentionally small
(`lenv/core.py` is the whole engine) and standard-library-only — please keep it
that way unless a dependency is truly justified.

## License

MIT © Pranav Digraskar
