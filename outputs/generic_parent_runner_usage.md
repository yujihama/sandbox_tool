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
- `system_prompt` and `system_prompt_file` add worker-only instructions.
- `skill_sources` stage profile-specific skills; relative host paths are
  resolved from the profile file directory.
- `include_global_skills: true` also passes global `--skill-source` entries to
  that profile.
- `image`, `deep_model`, `deep_recursion_limit`, and `max_review_rounds`
  override the runner defaults only for that profile.

The bundled examples live under:

```text
outputs/deep_agent_profiles/
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
