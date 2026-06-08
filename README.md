# Sandbox Tool

Generic parent-agent runner with a Podman-backed Deep Agent sandbox.

## Included files

- `outputs/generic_parent_runner.py`: parent agent runner with tool-mediated HITL review.
- `outputs/podman_sandbox_backend.py`: short-lived Podman container backend for Deep Agents.
- `outputs/Containerfile.python-data-sandbox`: Python data-analysis sandbox image.
- `outputs/generic_parent_runner_usage.md`: usage and CLI reference.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env.local` with:

```text
OPENAI_API_KEY=...
```

Build the sandbox image:

```bash
podman build -t localhost/python-data-sandbox:latest -f outputs/Containerfile.python-data-sandbox outputs
```

## Linux / RedHat

```bash
python outputs/generic_parent_runner.py \
  --host-os linux \
  --podman-bin podman \
  --selinux-relabel \
  --prompt-file work/task_prompt.txt \
  --input /path/source.xlsx=/input/source.xlsx \
  --expected-artifact /outputs/result.xlsx \
  --output-dir outputs/run1
```

## Windows / WSL

```powershell
python outputs/generic_parent_runner.py `
  --host-os windows `
  --wsl-distro Ubuntu-22.04 `
  --prompt-file work/task_prompt.txt `
  --input C:\path\source.xlsx=/input/source.xlsx `
  --expected-artifact /outputs/result.xlsx `
  --output-dir outputs/run1
```

