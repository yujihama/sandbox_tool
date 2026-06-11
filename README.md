# Sandbox Tool

Generic parent-agent runner with a sandboxed Deep Agent worker and a deterministic output gate.

## Included files

- `outputs/generic_parent_runner.py`: parent agent runner with tool-mediated HITL review.
- `outputs/multi_deep_parent_runner.py`: parent runner that decomposes a vague task into multiple Deep Agent calls.
- `outputs/podman_sandbox_backend.py`: short-lived Podman container backend for Deep Agents.
- `outputs/Containerfile.python-data-sandbox`: Python data-analysis sandbox image.
- `sandbox_tool/output_gate.py`: allowlist gate that validates and sanitizes final artifacts before the parent reads them.
- `sandbox_tool/sandbox_controller.py`: FastAPI controller that runs sandbox/gate containers through Docker Compose.
- `outputs/generic_parent_runner_usage.md`: usage and CLI reference.
- `docs/output_gate_implementation_plan.md`: implementation plan based on the reviewed sandbox requirements.

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

## Docker Compose

The Compose layout is intended for a Linux/RedHat host with Docker Compose. It
keeps the Docker socket only in `sandbox-controller`; the parent agent container
does not receive it.

```bash
docker compose build sandbox-controller agent-app
docker compose --profile sandbox-image build python-data-sandbox
docker compose up -d sandbox-controller agent-app
```

If you keep credentials in `.env.local`, pass them explicitly instead of baking
them into the image:

```bash
docker compose --env-file .env.local up -d sandbox-controller agent-app
```

Run the parent agent from inside the Compose app container:

```bash
docker compose --env-file .env.local exec agent-app \
  python outputs/generic_parent_runner.py \
    --sandbox-backend controller \
    --sandbox-controller-url http://sandbox-controller:8080 \
    --prompt-file /inputs/task_prompt.txt \
    --input /inputs/source.xlsx=/input/source.xlsx \
    --expected-artifact /outputs/result.xlsx \
    --output-dir run1
```

Default host paths:

- Run data: `/srv/sandbox-tool/runs` mounted to the same path in containers.
- Optional read-only inputs: `/srv/sandbox-tool/inputs` mounted to `/inputs`.
- Docker socket: `/var/run/docker.sock` mounted only into `sandbox-controller`.

For Docker-out-of-Docker bind mounts, keep `RUNS_ROOT_HOST` as an absolute host
path. The easiest RedHat layout is to use `/srv/sandbox-tool/runs` on both the
host and containers.

For SELinux-enabled RedHat hosts, create the run directory with a suitable
container label policy before starting Compose, or set `RUNS_ROOT_HOST` to a
pre-labeled path.

Stop runtime containers after use:

```bash
docker compose down
```

## Output Gate

Deep Agent writes raw artifacts under `/outputs`, mapped to
`raw_outputs/` in the run directory. The parent must call `run_output_gate`
before reading final artifacts. Clean artifacts are copied or sanitized into
`clean_exports/`; rejected raw artifacts are copied into `quarantine/`.
Text artifacts are rejected if they start with known binary magic bytes or if
their declared extension clearly conflicts with the document shape, such as an
HTML document saved as `.md` or `.csv`.

Allowed final artifact extensions are:

- `.md`
- `.csv`
- `.json`
- `.yaml`
- `.yml`
- `.xlsx`
- `.html`

For `.json`, the gate parses strict JSON and writes a canonicalized copy. For
`.yaml`/`.yml`, the gate uses PyYAML safe loading, rejects anchors, aliases,
duplicate keys, non-string mapping keys, and tags outside a JSON-compatible
safe subset, then writes a canonicalized YAML copy.

For `.xlsx`, normal formulas are preserved. Dangerous formula functions and
external references are rejected by default, or stringified if the runner is
called with `--xlsx-dangerous-formula-action stringify`.

For `.csv`, formula-like cells are apostrophe-prefixed, while valid numeric
literals such as `-100`, `+25.5`, and `-1.25e3` are preserved.

## Linux / RedHat

Direct native Podman mode remains available outside Compose:

```bash
python outputs/generic_parent_runner.py \
  --sandbox-backend podman \
  --host-os linux \
  --podman-bin podman \
  --selinux-relabel \
  --prompt-file work/task_prompt.txt \
  --input /path/source.xlsx=/input/source.xlsx \
  --expected-artifact /outputs/result.xlsx \
  --output-dir outputs/run1 \
  --xlsx-dangerous-formula-action reject
```

## Windows / WSL

```powershell
python outputs/generic_parent_runner.py `
  --sandbox-backend podman `
  --host-os windows `
  --wsl-distro Ubuntu-22.04 `
  --prompt-file work/task_prompt.txt `
  --input C:\path\source.xlsx=/input/source.xlsx `
  --expected-artifact /outputs/result.xlsx `
  --output-dir outputs/run1 `
  --xlsx-dangerous-formula-action reject
```
