# Generic Parent Runner Usage

`generic_parent_runner.py` is task-agnostic. It does not contain PDF, Excel, HTML, or workbook-specific logic.

## Responsibilities

The runner does:

- Stage host input files under sandbox `/input`.
- Pass the prompt to a parent agent.
- Ask the parent agent to call `run_deep_agent_task`.
- Run the Deep Agent in the Podman sandbox.
- Give the Deep Agent a `request_parent_review` HITL tool.
- Require the Deep Agent to create and run task-specific self-check artifacts
  before requesting parent review.
- Give the parent generic file-inspection tools for `/input` and `/outputs`.
- Give the parent and Deep Agent a `read_sandbox_file` tool that uses one
  entrypoint for text, CSV, JSON, Excel, PDF, image metadata, and optional
  image vision reads.
- Give the parent and Deep Agent a generic `inspect_sandbox_image` tool for
  focused vision review of images under `/input` and `/outputs`.
- Let the parent inspect artifacts after the Deep Agent requests review.
- Let the parent request bounded repair attempts from the Deep Agent until
  findings are resolved or the attempt limit is reached.
- Check expected artifact existence.
- Map `/outputs/...` sandbox paths to host output paths.
- Confirm sandbox cleanup.
- Save traces and evaluation JSON.

The runner does not:

- Repair generated artifacts.
- Edit generated artifacts itself.
- Re-run task-specific scripts.
- Perform HTML, PDF, Excel, or domain-specific validation.
- Tune the prompt after looking at the result.
- Contain task-specific acceptance criteria.

The Deep Agent is instructed to call `request_parent_review` after it has produced
or updated the expected artifacts and completed self-check. The required
self-check artifacts are:

- `/outputs/self_check_plan.md`
- `/outputs/self_check.py`, `/outputs/self_check.js`, or another `self_check.*`
  executable/check script
- `/outputs/self_check_report.md`

The Deep Agent designs these checks itself for the task. The parent then reads
generic file facts through `read_sandbox_file`, such as text previews, CSV
previews, JSON structure, workbook sheet previews, PDF page text previews, image
metadata, and the self-check report. It decides whether the artifact still
materially fails the user task and, if so, sends corrective instructions back to
the Deep Agent. The parent is still not allowed to edit files directly.

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
  --expected-artifact /outputs/result.xlsx `
  --output-dir outputs/my_generic_run `
  --max-review-rounds 2 `
  --parent-recursion-limit 12
```

Linux / RedHat native Podman:

```bash
python outputs/generic_parent_runner.py \
  --host-os linux \
  --podman-bin podman \
  --selinux-relabel \
  --prompt-file work/task_prompt.txt \
  --input /path/source.pdf=/input/source.pdf \
  --skill-source outputs/skills=/input/skills \
  --expected-artifact /outputs/result.xlsx \
  --output-dir outputs/my_generic_run \
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
- `--expected-artifact`: repeatable expected artifact path under `/outputs`.
- `--output-dir`: host output directory under this workspace's `outputs/`.
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

## Evaluation Contract

The generic machine-readable pass/fail result is still intentionally narrow:

```text
ok =
  all expected artifacts exist
  and Deep Agent did not raise an exception
  and sandbox workspace was cleaned up
```

The parent final response now includes a separate evidence-based review summary
based on the generic file inspections. That review is model judgment, not a
hard-coded domain validator.

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
  --expected-artifact /outputs/seal_surname_identification_summary.json `
  --expected-artifact /outputs/seal_surname_identification_contact_sheet.png `
  --output-dir outputs/seal_surname_identification_with_skill `
  --max-review-rounds 2
```

Deep Agents loads only the skill metadata at startup. When the task mentions
`印鑑`, `はんこ`, `判子`, `印影`, `hanko`, `inkan`, or red seal images, it should
read `/input/skills/seal-surname-identification/SKILL.md` and can execute the
helper script under `/input/skills/seal-surname-identification/scripts/`.
