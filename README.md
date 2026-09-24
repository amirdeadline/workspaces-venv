# Workspaces Venv Manager (`venv.py`)

Windows-focused tooling to manage **named Python workspaces** on a shared folder (local drive, mapped drive, or UNC). Each workspace gets:

- Its own `.venv` (Python virtual environment)
- A private `.env` file (secrets stay out of the JSON registry)
- Generated `activate.cmd` / `activate.ps1` (venv + env vars + optional `cd`)
- A **CMD shortcut** you can type from any new terminal (for example `palo`)

The `venv` command is installed per Windows user via `install.py` (launchers in `%USERPROFILE%\bin`, hooks in `%USERPROFILE%\.workspaces`).

---

## Repository layout

After you clone or copy this repo onto a share, the live layout should look like:

```text
<workspaces-root>/
  scripts/
    venv.py              # Main CLI (this project)
    install.py           # Per-user Windows installer
    litellm.py           # Optional LiteLLM helper CLI
    venv.config.json     # Optional defaults (copy from venv.config.json.example)
    venvs.json           # Workspace registry (copy from venvs.json.example)
    hooks/               # Stubs; real hooks are generated under ~/.workspaces
  virtual_envs/          # Default workspace parent directory (configurable)
    <name>/
      .venv/
      .env               # NOT in git — secrets
      requirements.txt
      activate.cmd
      activate.ps1
```

---

## Requirements

- **Windows 10/11** (CMD + PowerShell integration)
- **Python 3.10+** on each PC (`python` on PATH when you run `install.py`)
- A writable **workspaces root** (for example `E:\PC3_Shared\workspaces` or `Z:\workspaces`)

---

## First-time install (new PC)

1. Map or open the shared `<workspaces-root>` folder.
2. Copy config templates (once per share):

   ```cmd
   copy scripts\venvs.json.example scripts\venvs.json
   copy scripts\venv.config.json.example scripts\venv.config.json
   ```

3. Install CLI for **your Windows user** (no admin required):

   ```cmd
   python <workspaces-root>\scripts\install.py --path <workspaces-root>
   ```

4. Close the window, open a **new** CMD or PowerShell, then:

   ```cmd
   venv --list
   venv doctor
   ```

`install.py` sets user env `WORKSPACES_ROOT`, adds `%USERPROFILE%\bin` to PATH, registers `venv` / `ws` / `litellm`, updates CMD AutoRun and PowerShell profiles, and runs `venv install-shell` to publish workspace shortcuts.

---

## Configuration (`venv.config.json`)

Optional file: `<workspaces-root>\scripts\venv.config.json` (copy from `venv.config.json.example`).

| Key | Purpose |
|-----|---------|
| `virtual_envs_dir` | Subfolder under root for default workspace paths (default `virtual_envs`) |
| `venv_dir_name` | Virtualenv directory name (default `.venv`) |
| `env_file_name` | Env file name (default `.env`) |
| `requirements_file` | Requirements filename (default `requirements.txt`) |
| `default_python` | Python exe used when creating venvs (empty = current interpreter / DB default) |
| `default_cd_on_activate` | Default for `cd` on activate when using interactive add |
| `list_description_max_width` | Column width for `venv --list` |
| `export_include_venv` | Include `.venv` tree in exports |
| `export_include_env` | Include `.env` in exports (may contain secrets) |
| `export_include_claude_config` | Include `.claude-code/` in exports |
| `user_bin` | Override launcher directory (default `%USERPROFILE%\bin`) |

Edit the file, then run `venv sync`.

---

## Workspace commands

### List workspaces

```cmd
venv --list
venv list --json
```

### Create a workspace

```cmd
venv add --name palo --description "My project"
venv add --name demo --folder D:\custom\path\demo --requirements D:\pkg\requirements.txt
```

| Flag | Description |
|------|-------------|
| `--name` | Workspace name (also default shortcut) |
| `--description` | Free text |
| `--folder` | **Optional** override for workspace directory (default `<root>/virtual_envs/<name>`) |
| `--workdir` | Directory to `cd` into when activated |
| `--requirements` | `requirements.txt` to copy/install |
| `--env` | Copy an existing `.env` file |
| `--python` | Python executable used to create `.venv` |
| `--packages` | Extra pip packages |
| `--pythonpath` | Paths prepended to `PYTHONPATH` on activate |
| `--shortcut` | CMD/PowerShell shortcut name (default: workspace name) |
| `--no-cd` | Do not change directory on activate |
| `--skip-install` | Create venv only, skip pip |

### Environment variables (per workspace)

Secrets live in `<workspace>\.env`, **not** in `venvs.json`.

```cmd
venv var palo list
venv var palo add MY_VAR my-value
venv var palo remove MY_VAR
```

For long or JSON values, prefer a file + Python calling the CLI, or edit `.env` then run `venv sync`.

### Other workspace ops

```cmd
venv info palo
venv path palo
venv freeze palo
venv delete palo
venv doctor
venv sync
venv install-shell
```

---

## Aliases (`-A` / `--alias`)

Manage **CMD launchers** in `%USERPROFILE%\bin` and doskey entries (via `install-shell` / `sync`).

Workspace shortcuts (for example `palo`) appear automatically. You can add **custom** aliases:

### List (index, alias, command, detail)

```cmd
venv -A list
venv alias list
```

### Add custom alias

The `--command` value is written into the `.cmd` launcher **after** `@echo off` (one or more CMD lines).

```cmd
venv -A add --name kb --command "cd /d D:\Projects\KB && code ."
venv -A add --name gs --command "python C:\Tools\gsutil.py %*"
```

### Delete custom alias

```cmd
venv -A delete --name kb
venv -A delete 3
```

Index `3` refers to the row shown in `venv -A list`. Workspace shortcuts cannot be deleted with `-A delete`; use `venv delete <workspace>` instead.

---

## Export / import (move workspaces between PCs)

### Export

Creates a zip with:

- `workspace-export.json` — manifest + workspace record
- `workspace/` — `requirements.txt`, optional `.env`, activate scripts, etc.
- `dotvenv/` — optional `.venv` tree (see config)

```cmd
venv --export palo --file D:\backup\palo.zip
venv export palo --file D:\backup\palo.zip
```

### Import

```cmd
venv --import D:\backup\palo.zip --folder D:\workspaces\virtual_envs\palo
venv import D:\backup\palo.zip --folder D:\workspaces\virtual_envs\palo --recreate-venv
```

Use `--recreate-venv` on a new PC (recommended): copied `.venv` trees are often not portable across machines.

After import, open a **new** terminal and use the workspace shortcut from `venv -A list`.

---

## How activation works

Typing a shortcut (for example `palo`) runs `%USERPROFILE%\bin\palo.cmd`, which calls the workspace’s `activate.cmd`. That script:

1. Loads `env.cmd` (generated from `.env`)
2. Activates `.venv`
3. Sets `PYTHONPATH` if configured
4. `cd` to `workdir` if enabled

PowerShell uses `Enter-Workspace` / generated `profile.ps1` hooks (see `install.py`).

---

## Uninstall (current user only)

Does **not** delete shared workspaces or `.env` files:

```cmd
python <workspaces-root>\scripts\install.py --uninstall --path <workspaces-root>
```

---

## Security notes

- Never commit `venvs.json` with production data, `.env`, or `virtual_envs/` trees to public repos.
- `venv info` masks keys matching `TOKEN`, `SECRET`, `PASSWORD`, etc.; `venv var list` shows values — treat it as sensitive.
- Export zips can contain `.env` and private keys if `export_include_env` is true.

---

## Troubleshooting

| Symptom | Action |
|---------|--------|
| `'venv' is not recognized` | Re-run `install.py`, open a **new** terminal |
| Shortcut missing | `venv install-shell` or `venv sync` |
| Wrong drive letter after moving share | `WORKSPACES_ROOT` is set per PC; run `install.py --path` with the local path |
| `.venv` broken after copy | `venv import ... --recreate-venv` or recreate with `venv add` + requirements |
| JSON env var | Minify to one line; use file + `python -c` subprocess to `venv var ... add` |

---

## Development

This repository ships the **scripts only**. Runtime data (`venvs.json`, `virtual_envs/`, `.env`) stays on your share and is listed in `.gitignore`.

Schema version in `venvs.json` is **2** (aliases + custom folder support).

---

## License

MIT — see [LICENSE](LICENSE).
