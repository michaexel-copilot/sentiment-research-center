#!/usr/bin/env bash
# Install or upgrade the Sentiment Research Center inside a Debian/Ubuntu machine (the Proxmox container).
#
# Not run by hand: scripts/deploy.sh streams the code into /opt/sentiment-research-center/repo.new and then runs
#
#   bash /opt/sentiment-research-center/repo.new/deploy/install.sh --revision <commit>
#
# as root inside the container. Running it again upgrades in place and keeps the data, the env file and the
# Claude Code login. See docs/DEPLOYMENT.md.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin   # pct exec starts with a minimal PATH

APP=sentiment-research-center
SERVICE_USER=srcenter
INSTALL_DIR=/opt/$APP
REPO_DIR=$INSTALL_DIR/repo
NEW_DIR=$INSTALL_DIR/repo.new
VENV_DIR=$INSTALL_DIR/venv
BROWSERS_DIR=$INSTALL_DIR/browsers
STATE_DIR=/var/lib/$APP          # SRC_HOME and the service user's home (data, reports, Claude Code login)
ENV_DIR=/etc/$APP
ENV_FILE=$ENV_DIR/env
CLAUDE_BIN=$STATE_DIR/.local/bin/claude
DASHBOARD_PORT=8501
HEALTH_TIMEOUT=90

REVISION=unknown
STEP="reading the options"

die() { echo "install.sh: $*" >&2; exit 1; }
step() { STEP=$1; printf '\n==> %s\n' "$1"; }
on_exit() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo "install.sh: failed while ${STEP,} (exit status $status)." >&2
    fi
    return $status
}
trap on_exit EXIT

as_service_user() {
    runuser -u "$SERVICE_USER" -- env HOME="$STATE_DIR" PATH="$STATE_DIR/.local/bin:$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin" "$@"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --revision) [[ $# -ge 2 ]] || die "--revision needs a value"; REVISION=$2; shift 2 ;;
        *) die "unknown option: $1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die "run as root"
[[ -d $NEW_DIR/src/src_center ]] || die "$NEW_DIR does not contain the code; run scripts/deploy.sh instead"

step "Creating the service user and directories"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o root -g root -m 0755 "$INSTALL_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE_DIR" "$STATE_DIR/data" "$STATE_DIR/reports"
install -d -o root -g "$SERVICE_USER" -m 0750 "$ENV_DIR"
[[ -f $ENV_FILE ]] || install -o root -g "$SERVICE_USER" -m 0640 /dev/null "$ENV_FILE"
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

step "Installing system packages"
missing=()
for pkg in curl ca-certificates; do dpkg -s "$pkg" &>/dev/null || missing+=("$pkg"); done
if [[ ${#missing[@]} -gt 0 ]]; then
    apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}"
fi
if ! command -v uv &>/dev/null; then
    curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi

step "Switching to revision $REVISION"
echo "$REVISION" > "$NEW_DIR/REVISION"
rm -rf "$REPO_DIR.old"
[[ -d $REPO_DIR ]] && mv "$REPO_DIR" "$REPO_DIR.old"
mv "$NEW_DIR" "$REPO_DIR"
rm -rf "$REPO_DIR.old"
chown -R root:root "$REPO_DIR"

step "Installing Python and the dependencies"
export UV_PROJECT_ENVIRONMENT=$VENV_DIR UV_PYTHON_INSTALL_DIR=$INSTALL_DIR/python UV_PYTHON_PREFERENCE=only-managed
uv sync --project "$REPO_DIR" --frozen --no-dev --no-editable --compile-bytecode -q
chmod -R a+rX "$INSTALL_DIR"

step "Installing Chromium for the PDF one-pagers"
export PLAYWRIGHT_BROWSERS_PATH=$BROWSERS_DIR
if [[ -f $BROWSERS_DIR/.deps-installed ]]; then
    "$VENV_DIR/bin/playwright" install chromium
else
    "$VENV_DIR/bin/playwright" install --with-deps chromium
    touch "$BROWSERS_DIR/.deps-installed"
fi
chmod -R a+rX "$BROWSERS_DIR"

step "Installing Claude Code for the service user"
if [[ ! -x $CLAUDE_BIN ]]; then
    as_service_user bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
fi

step "Linking config and keys into SRC_HOME"
# The config is part of the code and comes from git; edit it locally and commit (which deploys).
ln -sfn "$REPO_DIR/config" "$STATE_DIR/config"
ln -sfn "$ENV_FILE" "$STATE_DIR/.env"

step "Installing the command and the systemd units"
install -m 0755 "$REPO_DIR/deploy/src-center" /usr/local/bin/src-center
install -m 0644 "$REPO_DIR"/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q $APP-dashboard.service $APP-crypto.timer $APP-stocks.timer $APP-universe.timer
systemctl start $APP-crypto.timer $APP-stocks.timer $APP-universe.timer
systemctl restart $APP-dashboard.service

if [[ ! -f $STATE_DIR/data/src.duckdb ]]; then
    step "First installation: building the asset lists in the background"
    systemctl start --no-block $APP-universe.service
fi

step "Checking the dashboard"
for ((i = 0; i < HEALTH_TIMEOUT; i++)); do
    if curl -fs -o /dev/null "http://127.0.0.1:$DASHBOARD_PORT/_stcore/health"; then
        break
    fi
    systemctl -q is-active $APP-dashboard.service || { journalctl -u $APP-dashboard.service -n 30 --no-pager; die "dashboard stopped"; }
    sleep 1
done
((i < HEALTH_TIMEOUT)) || die "dashboard did not answer within ${HEALTH_TIMEOUT}s"

step "Done"
ip=$(hostname -I | awk '{print $1}')
echo "Revision:  $REVISION"
echo "Dashboard: http://$ip:$DASHBOARD_PORT"
if as_service_user claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    echo "Claude:    logged in"
else
    echo "Claude:    NOT logged in. LLM scoring fails until you run once: pct enter <id>, then src-center claude auth login"
fi
