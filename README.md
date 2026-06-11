# Sandbox Tool

Generic parent-agent runner with a sandboxed Deep Agent worker and a deterministic output gate.

## Included files

- `outputs/generic_parent_runner.py`: parent agent runner with tool-mediated HITL review.
- `outputs/multi_deep_parent_runner.py`: parent runner that decomposes a vague task into multiple Deep Agent calls.
- `outputs/deep_agent_profiles/`: optional Deep Agent profile YAML files that expose multiple worker tools to the parent agent.
- `outputs/podman_sandbox_backend.py`: short-lived Podman container backend for Deep Agents.
- `outputs/Containerfile.python-data-sandbox`: Python data-analysis sandbox image.
- `outputs/Containerfile.browser-sandbox`: Playwright/Chromium sandbox image for local browser validation.
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
podman build -t localhost/python-browser-sandbox:latest -f outputs/Containerfile.browser-sandbox outputs
```

## Docker Compose

The Compose layout is intended for a Linux/RedHat host with Docker Compose. It
keeps the Docker socket only in `sandbox-controller`; the parent agent container
does not receive it.

```bash
docker compose build sandbox-controller agent-app
docker compose --profile sandbox-image build python-data-sandbox browser-sandbox
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

Sandbox containers are started with network disabled by default. The browser
sandbox can run Playwright/Chromium against local files and generated artifacts,
but it cannot perform external web search or browse internet sites unless the
controller/backend network policy is explicitly changed.

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

## Deep Agent Profiles

By default the parent receives one worker tool, `run_deep_agent_task`, which
uses the global `--image`, `--deep-model`, `--deep-recursion-limit`,
`--max-review-rounds`, and `--skill-source` settings.

You can expose multiple worker tools by passing profile files:

```bash
python outputs/generic_parent_runner.py \
  --deep-agent-profile-dir outputs/deep_agent_profiles \
  --prompt-file work/task_prompt.txt \
  --input /path/source.csv=/input/source.csv \
  --expected-artifact /outputs/report.md \
  --output-dir outputs/run1
```

Each profile can define:

- `tool_name`: the tool name visible to the parent agent.
- `description`: when the parent should choose this tool.
- `system_prompt` or `system_prompt_file`: profile-specific worker instructions.
- `skill_sources`: profile-only skill directories staged under `/input`.
- `include_global_skills`: also pass global `--skill-source` entries.
- `image`, `deep_model`, `deep_recursion_limit`, `max_review_rounds`: optional
  overrides for that worker profile.

The included examples cover quick checks, heavier statistical analysis,
document/artifact generation, seal-image reading, local browser validation,
controlled site-limited research, and interactive browser research.

The `site_research` profile is for tasks such as "crawl this specific public
website and summarize the relevant policy/support information." It does not
provide broad web search. The Deep Agent receives controlled tools that enforce
allowed domains, page/depth limits, response-size limits, and robots.txt before
writing a local crawl index under `/outputs/_site_crawl/<crawl_id>/`. Use this
profile when you want the agent to gather information from a specified site
without relying on a search API or a metasearch engine.

For listing pages, the agent can call `extract_allowed_site_links` before
crawling. The extractor supports reusable filters for `required_year`,
`required_month`, `date_from`, `date_to`, text/URL include/exclude regexes, CSS
selectors, URL substrings, allowed extensions, and maximum link count. Use
`crawl_allowed_urls` on that extracted explicit URL set when coverage matters.

The `browser_research` profile is for rendered and interactive public sites
that require JavaScript, form submission, clicks, or CSRF-managed browser flows.
It exposes the generic deterministic `run_playwright_task` tool with an explicit
`allowed_domains` allowlist and egress guard. The guard rejects long form/query
values, secret/path-like strings, high-entropy encoded payloads, and structured
payload-like values. The tool does not implement file upload actions. Results
are saved under `/outputs/_playwright/<run_id>/` for later inspection by the
Deep Agent and parent reviewer.

Only `browser_research` currently receives the
`houjin-bangou-browser-search` skill. That skill packages the browser workflow
for Japan's Corporate Number Publication Site while still using the generic
guarded Playwright tool rather than exposing a site-specific lookup tool. It
also includes known-good Playwright step recipes and a local result parser so
the agent can extract search rows and detail-page facts without manually
reading large browser traces.

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
