# Agent / VM experiment context

Read this file when giving shell paths, `cd` commands, or Mininet instructions for the **Linux VM** (not the macOS dev machine).

## VM repository root

Use this as the canonical `cd` target on the VM:

```bash
VM_REPO_PATH=/home/mininet/Project/4D-MAP
```

- In copy-paste commands, replace generic `cd /path/to/qcurl-4dmap-experiment` with:
  - `cd /home/mininet/Project/4D-MAP`
- Logs and scripts assume experiments run from this directory (see historical `server_*.log` `wd:` lines).

If the repo on the VM was cloned or renamed elsewhere, update **`VM_REPO_PATH`** above so agents stay correct.

## macOS workspace (reference only)

Local Cursor workspace may differ, e.g. `~/Project/mpquic/qcurl-4dmap-experiment`. Do not use that path on the VM unless it matches the VM’s actual clone.
