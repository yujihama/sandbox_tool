# PodmanSandboxBackend notes

## Current local setup

This workspace now runs the sandbox through WSL Ubuntu:

```powershell
wsl -d Ubuntu-22.04 -- sudo podman --version
```

The WSL Podman package is installed and the analysis image has been built:

```powershell
wsl -d Ubuntu-22.04 -- sudo podman build `
  -t localhost/python-data-sandbox:latest `
  -f /mnt/c/Users/nyham/Documents/Codex/2026-06-07/https-www-langchain-com-blog-give/work/Containerfile.python-data-sandbox `
  /mnt/c/Users/nyham/Documents/Codex/2026-06-07/https-www-langchain-com-blog-give/work
```

## Run the backend smoke test

```powershell
python work\podman_sandbox_backend_demo.py
```

Verified result:

- runtime mode: `wsl_sudo_podman`
- dry run: `false`
- `/outputs/result.json` was written by the container and read back by the backend
- `../escape.txt` was rejected as `invalid_path`
- cleanup removed the host scratch workspace

## Run the data-analysis smoke test

```powershell
python work\podman_heavy_analysis_smoke.py
```

Verified result:

- `large_sales_pipeline.csv` was read from read-only `/input`
- pandas/numpy/matplotlib ran inside the container
- JSON and PNG artifacts were written under `/outputs`
- cleanup removed the host scratch workspace

## Run the Deep Agent example

```powershell
python work\podman_deepagent_usage_example.py
```

Verified result:

- Deep Agent used the Podman backend
- it wrote and executed `/outputs/analyze_channel_bayes.py`
- it produced `/outputs/channel_conversion_bayes_report.md`
- it produced `/outputs/channel_conversion_posterior_ci.png`
- cleanup removed the host scratch workspace

## Controls implemented

- `--network none`
- `--read-only` root filesystem
- writable `/workspace`, `/outputs`, `/tmp`, and `/run`
- read-only `/input`
- `--cap-drop=all`
- `--security-opt no-new-privileges`
- non-root container user
- command timeout
- WSL mode uses `--cgroups=disabled` because this WSL instance has no cgroup mount
- WSL mode adds shell `ulimit -t` and `ulimit -v` as a fallback
- invalid paths such as `../escape.txt` are rejected
- every `execute()` uses a short-lived container and also removes it in `finally`
- `cleanup()` removes known leftover containers and deletes the host scratch workspace

## Docker Compose note

Docker Compose is convenient if Docker Desktop or Docker Engine is already running,
but this backend does not need Compose. It only needs short-lived `run` calls.

In this machine Docker CLI exists, but the Docker Desktop daemon was not running.
WSL Podman was therefore the lower-friction path for an executable, no-cost local
sandbox in this session.
