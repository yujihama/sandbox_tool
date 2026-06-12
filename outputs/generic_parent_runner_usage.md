# Generic Parent Runner Usage

`generic_parent_runner.py` is task-agnostic. It does not contain PDF, Excel, HTML, or workbook-specific logic.

## Responsibilities

The runner does:

- Stage host input files under sandbox `/input`.
- Pass the prompt to a parent agent.
- Ask the parent agent to call `run_deep_agent_task`, or the most appropriate
  profile-specific Deep Agent tool when profiles are configured.
- Run the Deep Agent in the Podman sandbox.
- Give the Deep Agent a `request_parent_review` HITL tool.
- Require the Deep Agent to create and run task-specific self-check artifacts
  before requesting parent review.
- Give the parent output-gate tools for final artifacts and clean export
  inspection.
- Optionally give the parent raw file-inspection tools for `/input` and
  `/outputs` when `--allow-raw-parent-inspection` is set.
- Give the parent and Deep Agent a `read_sandbox_file` tool that uses one
  entrypoint for text, CSV, JSON, Excel, PDF, image metadata, and optional
  image vision reads.
- Give the parent and Deep Agent a generic `inspect_sandbox_image` tool for
  focused vision review of images under `/input` and `/outputs`.
- Let the parent run the deterministic output gate after the Deep Agent requests
  review, then inspect clean `/exports` artifacts.
- Let the parent request bounded repair attempts from the Deep Agent until
  findings are resolved or the attempt limit is reached.
- Check expected artifact existence.
- Map `/outputs/...` sandbox paths to `raw_outputs/` and `/exports/...` to
  `clean_exports/` in the host run directory.
- Confirm sandbox cleanup.
- Save traces and evaluation JSON.

The runner does not:

- Repair generated artifacts.
- Edit generated artifacts itself.
- Re-run task-specific scripts.
- Perform HTML, PDF, Excel, or domain-specific validation.
- Tune the prompt after looking at the result.
- Contain task-specific acceptance criteria.

The runner uses a three-zone artifact layout under `--output-dir`:

- `input/`: staged input files.
- `raw_outputs/`: raw Deep Agent output mapped from sandbox `/outputs`.
- `clean_exports/`: files that passed or were sanitized by the output gate,
  exposed to the parent as `/exports`.
- `quarantine/`: rejected raw files.
- `gate_logs/`: deterministic gate manifests.
- `runner_logs/`: parent/deep traces and evaluation metadata.

The Deep Agent is instructed to call `request_parent_review` after it has produced
or updated the expected artifacts and completed self-check. The required
self-check artifacts are:

- `/outputs/self_check_plan.md`
- `/outputs/self_check_report.md`

The Deep Agent designs and executes checks itself for the task, but executable
self-check scripts are not final export artifacts. The parent calls
`run_output_gate` for declared final artifacts, then reads generic file facts
from `/exports`. It decides whether the artifact still materially fails the user
task and, if so, sends corrective instructions back to the Deep Agent. The
parent is still not allowed to edit files directly.

For image-understanding tasks, the Deep Agent can call `read_sandbox_file` with
a `question` on an image path, or call `inspect_sandbox_image` directly after it
creates crops, contact sheets, plots, or screenshots. This keeps file processing
inside the sandbox while making visual reading explicit and traceable as a tool
call. The parent can use the same tools during review.

## Command Shape

```powershell
python outputs/generic_parent_runner.py `
  --host-os windows `
  --prompt-file work/task_prompt.txt `
  --input "C:\path\source.pdf=/input/source.pdf" `
  --skill-source outputs/skills=/input/skills `
  --deep-agent-profile-dir outputs/deep_agent_profiles `
  --expected-artifact /outputs/result.xlsx `
  --output-dir outputs/my_generic_run `
  --max-review-rounds 2 `
  --parent-recursion-limit 12 `
  --xlsx-dangerous-formula-action reject
```

Linux / RedHat native Podman:

```bash
python outputs/generic_parent_runner.py \
  --sandbox-backend podman \
  --host-os linux \
  --podman-bin podman \
  --selinux-relabel \
  --prompt-file work/task_prompt.txt \
  --input /path/source.pdf=/input/source.pdf \
  --skill-source outputs/skills=/input/skills \
  --deep-agent-profile-dir outputs/deep_agent_profiles \
  --expected-artifact /outputs/result.xlsx \
  --output-dir outputs/my_generic_run \
  --max-review-rounds 2 \
  --parent-recursion-limit 12 \
  --xlsx-dangerous-formula-action reject
```

Docker Compose controller backend:

```bash
docker compose --env-file .env.local exec agent-app \
  python outputs/generic_parent_runner.py \
    --sandbox-backend controller \
    --sandbox-controller-url http://sandbox-controller:8080 \
    --prompt-file /inputs/task_prompt.txt \
    --input /inputs/source.xlsx=/input/source.xlsx \
    --expected-artifact /outputs/result.xlsx \
    --output-dir run1 \
    --max-review-rounds 2 \
    --parent-recursion-limit 12
```

## Arguments

- `--prompt-file` or `--prompt`: task instruction passed to the parent agent.
- `--input`: repeatable host-to-sandbox mapping. Use `HOST_PATH=/input/name`.
- `--skill-source`: repeatable host-to-sandbox mapping for Deep Agent skills.
  The host directory should contain `skill-name/SKILL.md` directories. Example:
  `--skill-source outputs/skills=/input/skills`, then the Deep Agent is created
  with `skills=["/input/skills"]`.
- `--deep-agent-profile`: repeatable YAML/JSON profile file. When any profile is
  supplied, the parent receives one Deep Agent tool per profile instead of the
  default `run_deep_agent_task`.
- `--deep-agent-profile-dir`: repeatable directory containing `.json`, `.yaml`,
  or `.yml` profile files. Files are loaded in sorted order.
- `--expected-artifact`: repeatable expected artifact path under `/outputs`.
- `--output-dir`: host output directory under this workspace's `outputs/`.
  This is now the run root; raw output is stored in `raw_outputs/`, clean output
  in `clean_exports/`, and logs in `runner_logs/` and `gate_logs/`.
- `--image`: sandbox image. Default is `localhost/python-data-sandbox:latest`.
- `--sandbox-backend`: `podman` for direct host runs, `controller` for Docker
  Compose runs where agent-app calls sandbox-controller over HTTP.
- `--sandbox-controller-url`: controller base URL for `--sandbox-backend controller`.
- `--sandbox-controller-token`: optional bearer token for controller API.
- `--host-os`: `auto`, `windows`, or `linux`. `windows` uses WSL Podman;
  `linux` uses native Podman directly.
- `--wsl-distro`: WSL distribution name for `--host-os windows`.
- `--wsl-no-sudo`: call WSL Podman without `sudo`.
- `--podman-bin`: native Podman executable for `--host-os linux`.
- `--selinux-relabel`: add Podman bind mount `relabel=private`; useful on
  SELinux-enabled RedHat hosts.
- `--max-review-rounds`: maximum Deep Agent attempts the parent may request.
- `--parent-recursion-limit`: parent agent graph recursion limit. Use a small value
  such as `6` for generate+inspect+final only; use a larger value such as `10-12`
  if one repair round should fit.
- `--allow-raw-parent-inspection`: expose raw `/input` and `/outputs` inspection
  tools to the parent. Omit this for production-style runs where final artifacts
  must pass through the output gate first.
- `--xlsx-dangerous-formula-action`: `reject` keeps formulas but rejects
  dangerous/external ones; `stringify` prefixes only dangerous formulas with an
  apostrophe while preserving ordinary formulas.

## Evaluation Contract

The generic machine-readable pass/fail result is still intentionally narrow:

```text
ok =
  all expected artifacts exist
  and Deep Agent did not raise an exception
  and backend cleanup succeeded
```

The parent final response now includes a separate evidence-based review summary
based on the generic file inspections. That review is model judgment, not a
hard-coded domain validator.

## Deep Agent Profiles

Profiles are a generic way to expose multiple Deep Agent tools with different
capabilities without copying the runner. The parent still decides which tool to
call, and all tools use the same sandbox, HITL review, output gate, cleanup, and
trace/evaluation machinery.

Example profile:

```yaml
id: heavy_data_analysis
tool_name: run_heavy_data_analysis_agent
description: Sandboxed agent for larger statistical analysis, sampling, modeling, and visual/report artifacts.
toolsets:
  - review
  - file_read
  - image_inspect
system_prompt: |
  Use reproducible analysis scripts for nontrivial computations. Explain
  methods, assumptions, uncertainty, and limitations in the final report.
skill_sources:
  - ../skills=/input/skills
include_global_skills: false
image: localhost/python-data-sandbox:latest
deep_model: openai:gpt-5.2
deep_recursion_limit: 120
max_review_rounds: 2
```

Field behavior:

- `tool_name` is the name exposed to the parent agent.
- `description` is the routing hint the parent sees.
- `toolsets` is required for profile YAML files and controls the actual custom
  tools passed to the Deep Agent. Unknown toolsets or profiles without `review`
  fail at load time.
- `system_prompt` and `system_prompt_file` add worker-only instructions.
- `skill_sources` stage profile-specific skills; relative host paths are
  resolved from the profile file directory.
- `include_global_skills: true` also passes global `--skill-source` entries to
  that profile.
- `image`, `deep_model`, `deep_recursion_limit`, and `max_review_rounds`
  override the runner defaults only for that profile.
- `expose_to_parent: false` hides a profile when loading a profile directory.
  The profile can still be used by passing it explicitly with
  `--deep-agent-profile`.
- Deep Agent attempts also use a graceful-finalize middleware derived from
  `deep_recursion_limit`. Before the hard graph recursion limit is reached, the
  middleware warns the worker, then removes broad exploration tools and leaves
  completion tools such as file writing, `execute`, and `request_parent_review`.
  The worker is instructed to produce supported partial artifacts, self-check,
  and request parent review instead of failing with `GraphRecursionError`.

Supported toolsets:

- `review`: `request_parent_review`; required for every profile.
- `file_read`: `read_sandbox_file`; type-aware reads of `/input` and `/outputs`.
- `image_inspect`: `inspect_sandbox_image`; focused vision reads for images.
- `site_crawl`: `crawl_allowed_site`, `extract_allowed_site_links`,
  `crawl_allowed_urls`, `search_site_crawl`, `read_crawled_page`,
  `list_site_crawls`.
- `browser`: `run_playwright_task`.

The bundled `quick_eval`, `document_artifact`, and `heavy_data_analysis`
profiles include `image_inspect` so they can inspect plots, screenshots,
extracted figures, or other generated images during self-checks.

The bundled examples live under:

```text
outputs/deep_agent_profiles/
```

For public web research, the default parent-facing profile is `web_research`.
It receives both `site_crawl` and `browser`, but its prompt is crawler-first:
use listing/link extraction and controlled crawls when possible, then fall back
to Playwright only for forms, JavaScript-rendered content, clicks, dynamic
pagination, or other interactive page state. This keeps the parent-facing tool
surface simple while preserving both retrieval modes inside one worker.

The specialized `site_research` and `browser_research` profiles remain in the
profile directory with `expose_to_parent: false`. They are not exposed when the
directory is loaded, but can be passed explicitly with `--deep-agent-profile`
for isolation/debugging:

- `site_research`: crawler-only; no Playwright.
- `browser_research`: Playwright-only; no site crawler.

The `browser_validation` profile uses `localhost/python-browser-sandbox:latest`
and provides Playwright/Chromium for local HTML, DOM, JavaScript, and screenshot
smoke checks. The default sandbox security policy disables network access, so
this browser profile is for offline/local artifact validation. It does not
enable external web search or internet browsing by itself.

The `web_research` profile stages the `houjin-bangou-browser-search` skill for
Corporate Number Publication Site tasks. The skill contains known-good
Playwright step recipes and a parser for saved Playwright `result.json` files,
which helps the Deep Agent reuse successful paths and extract corporate
numbers/detail URLs without re-reading large browser traces by hand.

Crawler tools only fetch allowed http(s) URLs, reject local/private hosts,
respect robots.txt by default, and store extracted text plus an index under
`/outputs/_site_crawl/<crawl_id>/`. Use `extract_allowed_site_links` first when
a listing/index page controls coverage; it supports `required_year`,
`required_month`, `date_from`, `date_to`, text/URL include and exclude regexes,
`css_selector`, `url_contains`, `allowed_extensions`, and `max_links`. Then pass
the returned URLs to `crawl_allowed_urls`.

Browser calls must provide `allowed_domains`; the tool saves JSON/Markdown
traces under `/outputs/_playwright/<run_id>/`. Browser input is egress guarded:
long values, secret/path-like strings, high-entropy encoded payloads, and
structured payload-like values are rejected. File-upload actions are not
implemented.

Example:

```powershell
$env:PYTHONIOENCODING='utf-8'
python outputs/generic_parent_runner.py `
  --deep-agent-profile-dir outputs/deep_agent_profiles `
  --prompt "Research https://ondankataisaku.env.go.jp/carbon_neutral/ only, collect up to five sourced articles about regulatory changes, and write /outputs/web_research_report.md." `
  --expected-artifact /outputs/web_research_report.md `
  --output-dir outputs/web_research_example `
  --max-review-rounds 2
```

## Skill Example

The included seal-reading skill lives at:

```text
outputs/skills/seal-surname-identification/SKILL.md
```

Use it for Japanese red seal / hanko / inkan surname identification:

```powershell
$env:PYTHONIOENCODING='utf-8'
python outputs/generic_parent_runner.py `
  --host-os auto `
  --prompt-file work/generic_runner_prompts/seal_surname_identification.txt `
  --input "C:\Users\nyham\Downloads\test01.png=/input/test01.png" `
  --input "C:\Users\nyham\Downloads\test02.png=/input/test02.png" `
  --input "C:\Users\nyham\Downloads\test03.png=/input/test03.png" `
  --skill-source outputs/skills=/input/skills `
  --expected-artifact /outputs/seal_surname_identification_report.md `
  --expected-artifact /outputs/seal_surname_identification_summary.csv `
  --output-dir outputs/seal_surname_identification_with_skill `
  --max-review-rounds 2
```

Deep Agents loads only the skill metadata at startup. When the task mentions
`印鑑`, `はんこ`, `判子`, `印影`, `hanko`, `inkan`, or red seal images, it should
read `/input/skills/seal-surname-identification/SKILL.md` and can execute the
helper script under `/input/skills/seal-surname-identification/scripts/`.
Generated contact sheets or crops may still be helper files under `/outputs`,
but they are not reviewed/exported artifacts unless raw parent inspection is
explicitly enabled.
