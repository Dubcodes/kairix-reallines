#!/usr/bin/env bash
# V1.6D opt-in permanent dedicated tracking boot configuration.
set -euo pipefail

TRACKING_UNIT='kairix-reallines-tracking.service'
WEB_UNIT='kairix-reallines-dev.service'
ROOT='/home/reallines/kairix-reallines'
DROP_DIR='/etc/systemd/system/kairix-reallines-dev.service.d'
DROP="$DROP_DIR/95-dedicated-tracking.conf"
OWNER_UNIT='/etc/systemd/system/kairix-reallines-tracking.service'
LEGACY_DROP="$DROP_DIR/95-pico-owner.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    echo "Usage: sudo bash scripts/reallines_tracking_mode.sh install|rollback|status" >&2
    exit 2
}

owner_pids() {
    # A manually launched owner must be stopped by the operator, never killed
    # or silently disconnected by this installer.
    ps -eo pid=,args= | awk '$0 ~ /python(3)?([.][0-9]+)?[[:space:]]+-m[[:space:]]+app[.](tracking_owner|pico_owner)([[:space:]]|$)/ { print }'
}

status() {
    echo '--- tracking service ---'
    systemctl is-enabled "$TRACKING_UNIT" 2>/dev/null || true
    systemctl is-active "$TRACKING_UNIT" 2>/dev/null || true
    echo '--- web service ---'
    systemctl is-enabled "$WEB_UNIT" 2>/dev/null || true
    systemctl is-active "$WEB_UNIT" 2>/dev/null || true
    echo '--- active tracking configuration ---'
    if [[ -e "$DROP" ]]; then cat "$DROP"; else echo 'Legacy Uvicorn mode (no dedicated tracking drop-in)'; fi
    if [[ -e "$LEGACY_DROP" ]]; then echo "WARNING: earlier Pico owner override present: $LEGACY_DROP"; fi
    if [[ -e "$DROP" && $(systemctl is-active "$TRACKING_UNIT" || true) == active ]]; then
        "$ROOT/.venv/bin/python" -m app.tracking_service_check --configured --wait 1 || true
    fi
}

rollback() {
    # Remove the Upholds relationship before stopping web, so it cannot
    # immediately restart the web service during rollback.
    systemctl disable --now "$TRACKING_UNIT" || true
    systemctl stop "$WEB_UNIT" || true
    rm -f "$DROP"
    systemctl daemon-reload
    systemctl reset-failed "$WEB_UNIT" "$TRACKING_UNIT" || true
    systemctl enable "$WEB_UNIT"
    systemctl start "$WEB_UNIT"
    echo 'Legacy mode restored. Re-reference Pan, Tilt, Zoom in Engineering.'
    status
}

install() {
    [[ -f "$REPO/deployment/systemd/$TRACKING_UNIT" && -f "$REPO/deployment/systemd/95-dedicated-tracking.conf" ]] || {
        echo 'Missing deployment files: run from the patched repository.' >&2; exit 1;
    }
    [[ "$REPO" == "$ROOT" ]] || { echo "Run on Pi from $ROOT (found $REPO)" >&2; exit 1; }
    [[ -x "$ROOT/.venv/bin/python" ]] || { echo 'Pi Python environment not found.' >&2; exit 1; }
    if [[ -e "$LEGACY_DROP" ]]; then
        echo "Remove earlier Pico proxy drop-in first: $LEGACY_DROP" >&2
        exit 1
    fi
    if [[ -e "$DROP" ]] && ! cmp -s "$REPO/deployment/systemd/95-dedicated-tracking.conf" "$DROP"; then
        echo 'Existing dedicated web drop-in differs; inspect manually before replacing.' >&2
        exit 1
    fi
    if [[ -e "$OWNER_UNIT" ]] && ! cmp -s "$REPO/deployment/systemd/$TRACKING_UNIT" "$OWNER_UNIT"; then
        echo 'Existing tracking service differs; inspect manually before replacing.' >&2
        exit 1
    fi
    if [[ -e "$DROP" && $(systemctl is-active "$TRACKING_UNIT" || true) == active && $(systemctl is-active "$WEB_UNIT" || true) == active ]]; then
        echo 'Dedicated services already running; leaving operator references untouched.'
        status
        return
    fi
    local manual
    manual="$(owner_pids)"
    if [[ -n "$manual" ]]; then
        echo 'A manually launched Pico/tracking owner is still running:' >&2
        echo "$manual" >&2
        echo 'Stop it using Ctrl+C in its terminal before installing.' >&2
        exit 1
    fi
    # Preserve original web unit and pre-existing drop-ins. Service files only
    # are installed after all checks and when the legacy owner has stopped.
    systemctl stop "$WEB_UNIT"
    /usr/bin/install -m 0644 "$REPO/deployment/systemd/$TRACKING_UNIT" "$OWNER_UNIT"
    mkdir -p "$DROP_DIR"
    /usr/bin/install -m 0644 "$REPO/deployment/systemd/95-dedicated-tracking.conf" "$DROP"
    systemctl daemon-reload
    if ! systemd-analyze verify "$OWNER_UNIT" "$WEB_UNIT" 2>&1; then
        echo 'Unit validation failed. Rolling back.' >&2
        rollback
        exit 1
    fi
    systemctl enable "$TRACKING_UNIT" "$WEB_UNIT"
    if ! systemctl restart "$TRACKING_UNIT" || ! systemctl restart "$WEB_UNIT"; then
        echo 'Dedicated startup failed. Rolling back.' >&2
        rollback
        exit 1
    fi
    if [[ $(systemctl is-active "$TRACKING_UNIT") != active || $(systemctl is-active "$WEB_UNIT") != active ]]; then
        echo 'A required service is not active. Rolling back.' >&2
        rollback
        exit 1
    fi
    echo 'Dedicated boot mode installed and started.'
    echo 'References are session-only: REFERENCE Pan, Tilt and Zoom again in Engineering.'
    status
}

[[ $# -eq 1 ]] || usage
case "$1" in
    status) status ;;
    install|rollback)
        [[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'Run install/rollback with sudo.' >&2; exit 1; }
        "$1" ;;
    *) usage ;;
esac
