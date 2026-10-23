#!/usr/bin/env bash
# Prepare only by default. --start is the explicit public-service activation.
set -euo pipefail
umask 077

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || fail 'Run this installer as root on the selected Ubuntu VM.'
[[ -f /etc/os-release ]] || fail 'Ubuntu 24.04 is required.'
# /etc/os-release is the operating-system-owned identity file.
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || fail 'This scaffold supports Ubuntu 24.04 only.'

public_hostname=''
release=''
activate=false
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --hostname) [[ "$#" -ge 2 ]] || fail 'Missing --hostname value.'; public_hostname="$2"; shift 2 ;;
        --release) [[ "$#" -ge 2 ]] || fail 'Missing --release value.'; release="$2"; shift 2 ;;
        --start) activate=true; shift ;;
        *) fail 'Usage: install.sh --hostname ACTUAL_DNS_NAME --release /opt/personal-options-trader/releases/NAME [--start]' ;;
    esac
done
[[ -n "$public_hostname" && -n "$release" ]] || fail 'An actual public hostname and staged release path are required.'
for executable in python3.12 systemctl getent useradd sha256sum ss runuser; do
    command -v "$executable" >/dev/null || fail "Missing prerequisite: $executable"
done
[[ -x /usr/bin/caddy ]] || fail 'Install Caddy from its official signed package repository before preparing this VM.'
python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python3.12 "$script_dir/configure.py" --hostname "$public_hostname" --release "$release"
[[ "$script_dir" == "$release/deploy/vm" ]] || fail 'Run the installer inside the selected staged release.'

# Require operator-owned source. The app cannot replace its code or environment.
[[ "$(stat -c %u "$release")" == 0 ]] || fail 'The staged release must be owned by root.'
[[ ! -L /var/lib/personal-options-trader ]] || fail 'The persistent data directory must not be a symlink.'
if ! getent passwd personal-options-trader >/dev/null; then
    useradd --system --user-group --home-dir /var/lib/personal-options-trader --no-create-home --shell /usr/sbin/nologin personal-options-trader
fi
[[ "$(id -u personal-options-trader)" -ne 0 ]] || fail 'Refusing a privileged application user.'
getent group personal-options-trader >/dev/null || fail 'The dedicated application group is missing.'
getent passwd caddy >/dev/null || fail 'The Caddy package must provide its unprivileged caddy user.'
install -d -o personal-options-trader -g personal-options-trader -m 0700 /var/lib/personal-options-trader
for subdirectory in reports backups; do
    [[ ! -L "/var/lib/personal-options-trader/$subdirectory" ]] || fail 'A persistent subdirectory must not be a symlink.'
    install -d -o personal-options-trader -g personal-options-trader -m 0700 "/var/lib/personal-options-trader/$subdirectory"
done

# Each staged release has its own environment. Repeating a completed setup does
# not reinstall or upgrade packages in a running environment.
requirements_hash="$(sha256sum "$release/backend/requirements.txt" | cut -d ' ' -f 1)"
managed="$release/.venv/.vm-managed"
installed="$release/.venv/.vm-requirements-sha256"
[[ ! -L "$release/.venv" ]] || fail 'The release virtual environment must not be a symlink.'
if [[ -d "$release/.venv" && ! -f "$managed" ]]; then
    fail 'An unmanaged virtual environment exists. Use a fresh staged release.'
fi
if [[ ! -d "$release/.venv" ]]; then
    python3.12 -m venv "$release/.venv"
    printf '%s\n' 'personal-options-trader-vm-v1' > "$managed"
fi
[[ -f "$managed" && "$(cat "$managed")" == personal-options-trader-vm-v1 ]] || fail 'The environment ownership marker is invalid.'
if [[ -f "$installed" ]]; then
    [[ "$(cat "$installed")" == "$requirements_hash" ]] || fail 'Dependencies changed in an existing release. Stage a new release for review.'
else
    if systemctl is-active --quiet personal-options-trader.service; then
        fail 'Stop the application before preparing an incomplete dependency environment.'
    fi
    "$release/.venv/bin/python" -m pip install -r "$release/backend/requirements.txt"
    printf '%s\n' "$requirements_hash" > "$installed"
fi
"$release/.venv/bin/python" -m pip check
python3.12 "$script_dir/configure.py" --hostname "$public_hostname" --release "$release" --apply
runuser -u personal-options-trader -- test -r "$release/backend/app/main.py" || fail 'The unprivileged app user cannot read the staged source; review directory/file permissions.'
runuser -u personal-options-trader -- test -x "$release/.venv/bin/python" || fail 'The unprivileged app user cannot execute the staged Python environment.'
/usr/bin/caddy validate --config /etc/personal-options-trader/Caddyfile --adapter caddyfile
systemctl daemon-reload

if [[ "$activate" == true ]]; then
    # This dedicated proxy must not replace another site's web server.
    if systemctl is-active --quiet caddy.service || systemctl is-enabled --quiet caddy.service 2>/dev/null; then
        fail 'The packaged caddy.service is active or enabled. Review and disable that default service before activating the dedicated proxy.'
    fi
    if ! systemctl is-active --quiet personal-options-trader-proxy.service; then
        [[ -z "$(ss -Hltn '( sport = :80 or sport = :443 )')" ]] || fail 'Ports 80/443 are already in use; existing services were not changed.'
    fi
    systemctl enable --now personal-options-trader.service
    systemctl enable --now personal-options-trader-proxy.service
    systemctl enable --now personal-options-trader-backup.timer
    printf '%s\n' 'Services enabled. Verify database readiness, HTTPS, sign-in and phone access before switching Vercel.'
else
    printf '%s\n' 'Prepared only. No application/proxy service was started or enabled by this installer.'
    printf '%s\n' 'Review private config, DNS, firewall and migration first; repeat the same command with --start to activate.'
fi
printf '%s\n' 'The private setup token is stored in /etc/personal-options-trader/app.env and was not printed.'
