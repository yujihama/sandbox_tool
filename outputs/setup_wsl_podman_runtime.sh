#!/usr/bin/env bash
set -euo pipefail

uid="$(id -u)"
runtime_dir="/run/user/${uid}"

sudo mkdir -p "${runtime_dir}"
sudo chown "${uid}:${uid}" "${runtime_dir}"
sudo chmod 700 "${runtime_dir}"

export XDG_RUNTIME_DIR="${runtime_dir}"

podman --version
podman info --format '{{.Host.OCIRuntime.Name}} {{.Store.GraphDriverName}}'
