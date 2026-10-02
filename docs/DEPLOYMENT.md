# Deployment (Proxmox container)

The Sentiment Research Center runs on the Proxmox host **192.168.178.10** in the LXC container **108** (`paper-trading`, Ubuntu 24.04, 4 GB RAM, also hosts the paper trading platform on port 8000). Every commit on `main` is deployed there first and only pushed to GitHub when the deployment succeeds.

| What | Where |
|---|---|
| Dashboard | **http://192.168.178.239:8501** (the container's DHCP address; `pct exec 108 -- hostname -I` shows the current one) |
| Code (replaced on every deploy) | `/opt/sentiment-research-center/repo`; the deployed commit is in `repo/REVISION` |
| Python environment, Chromium | `/opt/sentiment-research-center/{venv,python,browsers}` |
| Data, reports, Claude Code login (`SRC_HOME`) | `/var/lib/sentiment-research-center` (kept across deploys) |
| API keys (copy of your local `.env`) | `/etc/sentiment-research-center/env` |
| Service user | `srcenter` |
| CLI | `src-center` (runs `src` as the service user, e.g. `src-center watch list`) |

## Workflow: commit → deploy → push

```
git commit  ──►  .githooks/post-commit  ──►  scripts/deploy.sh  ──ok──►  git push origin main
                                                    │
                                                    └─failed──►  commit stays local, not pushed
```

1. Commit on `main` as usual.
2. The `post-commit` hook runs `scripts/deploy.sh`, which deploys the commit (HEAD) to container 108.
3. If the deployment succeeds (including the dashboard health check), the hook pushes to GitHub. If it fails, the commit stays local. Fix the problem, then run `scripts/deploy.sh && git push`.

The hook does nothing during a rebase or cherry-pick sequence, or on other branches. To skip it for one commit, use `SRC_SKIP_DEPLOY=1 git commit ...`.

**One-time setup per clone** (the hooks live in the repository but git doesn't enable them on its own):

```bash
git config core.hooksPath .githooks
```

You also need key-based SSH as root to the Proxmox host (`ssh root@192.168.178.10` must work without a password).

## What `scripts/deploy.sh` does

Run from Git Bash on Windows (or any Unix shell). Options:

| Command | Effect |
|---|---|
| `scripts/deploy.sh` | Deploy the committed HEAD (uncommitted changes are not included) |
| `scripts/deploy.sh --sync-env` | Also upload the local `.env` (do this after changing API keys) |
| `scripts/deploy.sh --worktree` | Deploy the working tree, including uncommitted files, to test deployment changes. The revision shows as `<commit>-dirty` |

Other targets: `SRC_DEPLOY_HOST` (default `root@192.168.178.10`) and `SRC_DEPLOY_CTID` (default `108`).

Steps:

1. It streams the code over SSH to the Proxmox host. `git archive HEAD` produces it, and `pct exec 108` unpacks it into `/opt/sentiment-research-center/repo.new`. The container needs no SSH access of its own, and the target is addressed by its container ID rather than its DHCP address.
2. On the first deploy, or with `--sync-env`, it uploads `.env` to `/etc/sentiment-research-center/env` (mode 0640, readable only by root and the service user).
3. It runs [`deploy/install.sh`](../deploy/install.sh) as root inside the container. The script is idempotent:
   - creates the `srcenter` user and the directories, and installs `uv` if it's missing;
   - replaces `repo` with `repo.new` and records the revision;
   - `uv sync --frozen --no-dev --no-editable` into `/opt/sentiment-research-center/venv` (a uv-managed Python 3.12);
   - installs Chromium for the PDF one-pagers (with its system packages on the first run);
   - installs Claude Code for the service user (first run only; it updates itself afterwards);
   - links `SRC_HOME/config` to `repo/config` and `SRC_HOME/.env` to the env file;
   - installs `/usr/local/bin/src-center` and the systemd units from [`deploy/systemd/`](../deploy/systemd), then restarts the dashboard;
   - on the first install, starts `src universe` in the background;
   - waits until the dashboard answers on `/_stcore/health`, and fails the deploy otherwise.

## Services and schedule

| Unit | What | When (Europe/Berlin) |
|---|---|---|
| `sentiment-research-center-dashboard.service` | Streamlit dashboard on port 8501, all interfaces | always (restarted on failure and at boot) |
| `sentiment-research-center-crypto.timer` | `src run --universe observed --class crypto` | daily 02:30 |
| `sentiment-research-center-stocks.timer` | `src run --universe observed --class stock` | Mon–Fri 23:45 |
| `sentiment-research-center-universe.timer` | `src universe` | Sunday 12:00 |

These are the same times as the Windows tasks from `scripts/register_tasks.ps1`. If those are registered on your PC as well, every run happens twice and uses the subscription twice. Remove them with `powershell -File scripts\register_tasks.ps1 -Remove`.

The services can only write to `/var/lib/sentiment-research-center` (`ProtectSystem=strict`). Logs go to the journal instead of `data/logs`.

## First-time setup after the first deploy: Claude login

The default LLM provider `claude` needs Claude Code logged in with your Claude subscription, as the service user. This is interactive, so do it once by hand:

```bash
ssh root@192.168.178.10
pct enter 108
src-center claude auth login      # open the printed URL, log in, paste the code back
src-center claude auth status     # "loggedIn": true, "authMethod": "claude.ai"
```

Until then, LLM scoring fails with "Claude Code is not logged in". Each deploy prints the login state at the end. The login is kept across deploys.

## Everyday operations

All commands run in the container (`ssh root@192.168.178.10`, then `pct enter 108`):

| Task | Command |
|---|---|
| Run the pipeline now | `systemctl start sentiment-research-center-crypto` (or `-stocks`, `-universe`), or `src-center run -a NVDA` |
| Follow a run's log | `journalctl -u sentiment-research-center-crypto -f` |
| Next scheduled runs | `systemctl list-timers 'sentiment-research-center-*'` |
| Dashboard status and log | `systemctl status sentiment-research-center-dashboard`, `journalctl -u sentiment-research-center-dashboard` |
| Deployed commit | `cat /opt/sentiment-research-center/repo/REVISION` |
| Change API keys | Edit `.env` locally and run `scripts/deploy.sh --sync-env`, or edit `/etc/sentiment-research-center/env` and `systemctl restart sentiment-research-center-dashboard` |
| Change settings or the watchlist | Edit `config/*.yaml` locally and commit (which deploys). `SRC_HOME/config` points to the deployed code, so `src-center watch add` doesn't work on the server |
| Start from scratch | `systemctl stop sentiment-research-center-dashboard`, delete `/var/lib/sentiment-research-center/{data,reports}`, `systemctl start sentiment-research-center-universe sentiment-research-center-dashboard` |

## Resources

The container has 2 cores, 4 GB RAM (raised from 2 GB on 2026-10-02 for the pipeline runs), 512 MB swap and an 8 GB disk, shared with the paper trading platform. After installation about 3.5 GB of disk is free. A pipeline run uses up to `llm.claude.concurrency` (4) Claude Code processes plus Python and Chromium. If runs are killed for lack of memory (`journalctl -k | grep -i oom`), lower `llm.claude.concurrency` in `config/settings.yaml`, or give the container more memory on the Proxmox host (`pct set 108 --memory <MB>`; this applies immediately, without a restart).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Permission denied (publickey)` from `deploy.sh` | Key-based SSH to the Proxmox host is missing. Run `ssh-copy-id root@192.168.178.10` |
| Commit was not pushed: "deploy failed" | The deploy output above it names the failed step (`install.sh: failed while ...`). Fix it, then `scripts/deploy.sh && git push` |
| `dashboard did not answer` | `journalctl -u sentiment-research-center-dashboard -n 50` in the container |
| `Claude Code is not logged in` in a run's log | See [Claude login](#first-time-setup-after-the-first-deploy-claude-login) |
| `$'\r': command not found` in the container | A shell script was checked out with CRLF. `.gitattributes` keeps `deploy/`, `*.sh` and `.githooks/` at LF; run `git add --renormalize .` and commit |
