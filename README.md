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
- `sandbox_tool/egress_proxy.py`: allowlist HTTP(S) proxy used as the network boundary for website/browser tools in Compose.
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
does not receive it. Website and browser tools are executed by the parent runner
process, so Compose puts `agent-app` on an internal network and routes external
HTTP(S) only through `egress-proxy`.

```bash
docker compose build
docker compose up -d egress-proxy sandbox-controller agent-app
```

`docker compose build` builds both the parent/controller/proxy image and the
short-lived sandbox images. Keep `up` scoped to `egress-proxy`,
`sandbox-controller`, and `agent-app`; the sandbox image services are build
helpers, not long-running services.

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
- Egress proxy: `egress-proxy:8888` is the only Compose service with public
  network egress.

Set `EGRESS_PROXY_SIGNING_SECRET` in production. The default is for local
development only. `EGRESS_PROXY_DEFAULT_ALLOWLIST` defaults to `api.openai.com`
so the parent agent can call the OpenAI API through the proxy. Browser/crawler
tools generate short-lived signed proxy tokens for the explicit `allowed_domains`
provided to each tool call.

For Docker-out-of-Docker bind mounts, keep `RUNS_ROOT_HOST` as an absolute host
path. The easiest RedHat layout is to use `/srv/sandbox-tool/runs` on both the
host and containers.

For SELinux-enabled RedHat hosts, create the run directory with a suitable
container label policy before starting Compose, or set `RUNS_ROOT_HOST` to a
pre-labeled path.

Sandbox containers are started with network disabled by default. The browser and
site-research tools are not sandbox-container network access; they are controlled
runner tools. In Compose, their network path is still forced through the
allowlist proxy by Docker network topology:

- `agent-app` and `sandbox-controller` are attached only to an internal network.
- `egress-proxy` is attached to both the internal network and a public egress
  network.
- OpenAI API calls are allowed by the proxy default allowlist.
- Browser/crawler calls use per-call signed proxy tokens scoped to the declared
  public domains.
- The proxy rejects private/non-global DNS resolutions to reduce DNS-rebinding
  and SSRF risk.

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
- `input_access`: `all`, `skills_only`, or `none`. Network-enabled profiles
  should use `skills_only` or `none` so user `/input` files are not mounted into
  a profile that can use browser/crawler tools.
- `result_mode`: `artifact` for reviewed output files, or `inline` for direct
  answers that skip output gate and parent review.
- `self_check_policy`: `script`, `checklist`, or `none`. Use `checklist` for
  lighter profile runs that still leave a reviewable plan/report in artifact
  mode.
- `image`, `deep_model`, `deep_recursion_limit`, `max_review_rounds`: optional
  overrides for that worker profile.
- `graceful_finalize`: optional per-profile overrides for early finalization
  thresholds. Supported keys are `warning_model_calls`,
  `finalize_model_calls`, `warning_tool_calls`, `finalize_tool_calls`,
  `warning_message_count`, `finalize_message_count`, and
  `strict_finalize_after_model_calls`. This is useful for heavy artifact tasks
  where LangGraph graph steps can hit the hard recursion limit before visible
  model/tool counts look high.

The included examples cover quick checks, heavier statistical analysis,
document/artifact generation, seal-image reading, local browser validation,
controlled site-limited research, and interactive browser research.

The `web_research` profile is for tasks such as "crawl this specific public
website and summarize the relevant policy/support information" or "use this
public form and report the observed result." It does not provide broad web
search. The Deep Agent receives controlled crawler/browser tools that enforce
allowed domains, page/depth limits, response-size limits, robots.txt, browser
egress guards, and the Compose network proxy before writing local evidence under
`/outputs/_site_crawl/<crawl_id>/` or `/outputs/_playwright/<run_id>/`.

For listing pages, the agent can call `extract_allowed_site_links` before
crawling. The extractor supports reusable filters for `required_year`,
`required_month`, `date_from`, `date_to`, text/URL include/exclude regexes, CSS
selectors, URL substrings, allowed extensions, and maximum link count. Use
`crawl_allowed_urls` on that extracted explicit URL set when coverage matters.

The older `site_research` and `browser_research` profiles are retained as
explicit opt-in compatibility profiles with `expose_to_parent: false`; the
default profile directory exposes the unified `web_research` profile.

`run_playwright_task` requires an explicit `allowed_domains` allowlist and has
an application-layer egress guard. The guard rejects long form/query values,
secret/path-like strings, high-entropy encoded payloads, and structured
payload-like values. The tool does not implement file upload actions. Results
are saved under `/outputs/_playwright/<run_id>/` for later inspection by the
Deep Agent and parent reviewer.

Network research profiles receive the `company-info-search` skill. That skill
packages public company-information workflows while still using the generic
guarded Playwright tool rather than exposing a site-specific lookup tool. Its
current bundled recipe covers Japan's Corporate Number Publication Site, with
known-good Playwright steps and a local result parser so the agent can extract
search rows and detail-page facts without manually reading large browser
traces.

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
