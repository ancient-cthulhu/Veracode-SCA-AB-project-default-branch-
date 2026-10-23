Sets the default branch of Veracode SCA agent-based projects in bulk, or puts them back on **Use Last Scanned**.

The default branch decides which branch's issues, libraries and licenses the Platform shows, which branch feeds linked application profiles, and where issues for newly published vulnerabilities are created. New projects start on Use Last Scanned, so any feature-branch scan replaces what you see. This script pins a branch across many projects in one run instead of going through Project > Settings one by one.

> Veracode has no public API for this setting. The script uses the same internal endpoints as the Settings page, so it can break without notice. Run with `--dry-run` first.

## How it works

1. Authenticates with your HMAC API keys, then opens a UI session (`vsid` cookie and XSRF token).
2. Lists workspaces and projects with the official SCA REST API.
3. Reads each project's current default from the workspace repos report (`POST /srcclr/teams/{id}/reports/repos`).
4. Compares it with the target for that project:
   - already on target: `no_action`
   - target branch never scanned for the project: `skipped_no_branch`, since the Platform can only select scanned branches
   - `--only-last-scanned` and the project has an explicit or unreadable default: `skipped`
   - otherwise: updated
5. Writes the change with `POST /srcclr/repos/{id}/branches:default-branch`:
   - pin a branch: `{"branch": "main", "useLatest": false}`
   - Use Last Scanned: `{"branch": null, "useLatest": true}`
6. Saves a JSON report with every project's previous default, target and result.

## Requirements

```bash
python --version  # Python 3.9+
pip install requests veracode-api-py
```

API credentials from a **human user** with Security Lead, Workspace Administrator or Workspace Editor. API service accounts are rejected by these endpoints.

```ini
# ~/.veracode/credentials
[default]
veracode_api_key_id = YOUR_ID
veracode_api_key_secret = YOUR_SECRET
```

## Usage

```bash
# Preview: pin every project to main, change nothing
python set_default_branch.py --branch main --dry-run

# Pin every project to main
python set_default_branch.py --branch main

# Only projects still on Use Last Scanned, keep explicit defaults
python set_default_branch.py --branch main --only-last-scanned

# One workspace, or specific projects
python set_default_branch.py --branch develop --workspace "Payments"
python set_default_branch.py --branch develop --project org/api --project org/web

# Put projects back on Use Last Scanned
python set_default_branch.py --reset --workspace "Experiments"

# Different target per project
python set_default_branch.py --csv targets.csv

# EU tenant with its own credentials profile
python set_default_branch.py --branch main --instance eu --profile eu

# Retry skipped or failed projects from a previous report
{ echo "workspace,project,branch"
  jq -r '.projects[] | select(.action|IN("skipped_no_branch","failed","error")) | [.workspace,.project,.target] | @csv' report.json
} > retry.csv && python set_default_branch.py --csv retry.csv

# Undo a run: restore each project's previous default
{ echo "workspace,project,branch"
  jq -r '.projects[] | select(.action=="updated" and .current_default!="unknown") | [.workspace,.project,.current_default] | @csv' report.json
} > rollback.csv && python set_default_branch.py --csv rollback.csv
```

CSV format. An empty `workspace` matches the project in any workspace, and `Use Last Scanned` as the branch resets it:

```csv
workspace,project,branch
Payments,org/api,main
,org/legacy,master
,org/sandbox,Use Last Scanned
```

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--branch` | | Target branch for every selected project |
| `--reset` | | Set selected projects back to Use Last Scanned |
| `--csv` | | Per-project targets, header `workspace,project,branch` |
| `--workspace` | all | Only this workspace (repeatable) |
| `--project` | all | Only this project (repeatable, not with `--csv`) |
| `--only-last-scanned` | `false` | Change only projects on Use Last Scanned. Not with `--reset` |
| `--instance` | `com` | Platform region: `com`, `eu`, `us` |
| `--profile` | `default` | Section in `~/.veracode/credentials` |
| `--dry-run` | `false` | Report what would change, change nothing |
| `--output` | `default-branch-report-<UTC>.json` | Report path |
| `--debug` | `false` | Request-level logging |

Exactly one of `--branch`, `--reset` or `--csv` is required.

## Output

One entry per project:

```json
{"workspace": "Payments", "project": "org/api", "action": "updated",
 "current_default": "Use Last Scanned", "target": "main", "message": "Use Last Scanned -> 'main'"}
```

| Action | Meaning |
|---|---|
| `updated` / `would_update` | Changed, or would be in a dry run |
| `no_action` | Already on target |
| `skipped` | Kept by `--only-last-scanned` |
| `skipped_no_branch` | Target never scanned for this project. Scan that branch, then retry |
| `failed` / `error` | Request failed. Exit code is 1 |

CSV rows that match no project are listed under `unmatched_csv_rows`.

## Notes

- **Detection.** Use Last Scanned is read as an empty `defaultBranch` in the internal report. On your first dry run, confirm a project you know is on Use Last Scanned shows it as `current_default`.
- **`current_default: unknown`** means the project wasn't in the workspace report. It is still updated unless you use `--only-last-scanned`, and the undo recipe can't restore it.
- **`No token in principal response`**: the keys belong to an API service account.
- **XSRF error or 401/403**: wrong `--instance`, expired keys, or missing role.

