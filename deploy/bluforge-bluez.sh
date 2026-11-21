#!/usr/bin/env bash
# bluforge-bluez - BlueZ lifecycle for the BluForge Bluetooth Test Appliance.
#
# BluForge is sold without BlueZ: BlueZ must be installed by the end-user. This
# one tool covers both halves of that:
#
#   remove --staging   at INITIAL STAGING ONLY (Candela imaging): uninstall BlueZ so the unit
#                      ships without it. Previews first, refuses to run twice, and refuses to
#                      remove a BlueZ the end-user installed.
#   install            end-user, after purchasing BluForge: shows a notice, then installs BlueZ
#                      from the operating system's own repositories (or a local package
#                      directory for air-gapped sites) and starts bluetooth.service.
#   status             what is present (add --json for the GUI/scripts).
#
# Usage:
#   bluforge-bluez status [--json]
#   sudo bluforge-bluez install [--accept] [--offline-dir DIR] [--dry-run]
#   sudo bluforge-bluez remove --staging [--yes] [--dry-run] [--force]
#
# State markers live in $BLUFORGE_STATE_DIR (default /var/lib/bluforge):
#   bluez-staged-removed   staging removal already happened (a second run is a no-op)
#   bluez-user-installed   the end-user installed BlueZ (staging removal now refuses)
set -uo pipefail

STATE_DIR="${BLUFORGE_STATE_DIR:-/var/lib/bluforge}"
LOG_FILE="${BLUFORGE_BLUEZ_LOG:-/var/log/bluforge-bluez.log}"
M_STAGED="$STATE_DIR/bluez-staged-removed"
M_USER="$STATE_DIR/bluez-user-installed"

c_grn="\033[32m"; c_yel="\033[33m"; c_red="\033[31m"; c_cyn="\033[36m"; c_rst="\033[0m"
log()  { echo -e "${c_cyn}[*]${c_rst} $*"; }
ok()   { echo -e "${c_grn}[+]${c_rst} $*"; }
warn() { echo -e "${c_yel}[!]${c_rst} $*" >&2; }
die()  { echo -e "${c_red}[x]${c_rst} $*" >&2; exit 1; }

# ---- package manager ---------------------------------------------------------------------
if   command -v dnf     >/dev/null 2>&1; then PM=dnf
elif command -v apt-get >/dev/null 2>&1; then PM=apt
else PM=""; fi

# What BluForge needs from BlueZ:
#   bluez             bluetoothd, bluetoothctl, btmgmt            (all features)
#   bluez-deprecated  hcitool, hciconfig, sdptool                 (Fedora/RHEL: split package; Debian
#                                                                  and Ubuntu ship them inside bluez)
#   libspa-0.2-bluetooth  PipeWire's Bluetooth audio plugin        (Debian/Ubuntu only; Fedora's
#                                                                  pipewire-libs already includes it)
install_pkgs() { case "$PM" in dnf) echo "bluez bluez-deprecated";; apt) echo "bluez libspa-0.2-bluetooth";; esac; }

find_bluetoothd() {
  for p in /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd /usr/sbin/bluetoothd; do
    [ -x "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}

bluez_present() { find_bluetoothd >/dev/null && command -v bluetoothctl >/dev/null 2>&1; }

# ---- status ------------------------------------------------------------------------------
cmd_status() {
  local json=0; [ "${1:-}" = "--json" ] && json=1
  local bd ver svc enabled
  bd="$(find_bluetoothd || true)"
  ver="$(bluetoothctl --version 2>/dev/null | awk '{print $NF}')"
  svc="$(systemctl is-active bluetooth.service 2>/dev/null || true)"
  enabled="$(systemctl is-enabled bluetooth.service 2>/dev/null || true)"
  local have_ctl=0 have_dep=0 have_sdp=0 have_mgmt=0
  command -v bluetoothctl >/dev/null 2>&1 && have_ctl=1
  command -v hcitool      >/dev/null 2>&1 && have_dep=1
  command -v sdptool      >/dev/null 2>&1 && have_sdp=1
  command -v btmgmt       >/dev/null 2>&1 && have_mgmt=1
  local installed=false; bluez_present && installed=true
  local staged=false user=false
  [ -e "$M_STAGED" ] && staged=true
  [ -e "$M_USER" ] && user=true
  if [ "$json" = 1 ]; then
    printf '{"installed":%s,"bluetoothd":"%s","version":"%s","service_active":"%s","service_enabled":"%s",' \
      "$installed" "$bd" "$ver" "$svc" "$enabled"
    printf '"tools":{"bluetoothctl":%s,"btmgmt":%s,"hcitool":%s,"sdptool":%s},' \
      "$( [ $have_ctl = 1 ] && echo true || echo false )" "$( [ $have_mgmt = 1 ] && echo true || echo false )" \
      "$( [ $have_dep = 1 ] && echo true || echo false )" "$( [ $have_sdp = 1 ] && echo true || echo false )"
    printf '"staged_removed":%s,"user_installed":%s,"package_manager":"%s"}\n' "$staged" "$user" "$PM"
  else
    echo "BlueZ installed      : $installed ${ver:+(version $ver)}"
    echo "bluetoothd           : ${bd:-missing}"
    echo "tools                : bluetoothctl=$have_ctl btmgmt=$have_mgmt hcitool=$have_dep sdptool=$have_sdp"
    echo "bluetooth.service    : ${svc:-unknown} / ${enabled:-unknown}"
    echo "staging removal done : $staged     end-user installed: $user"
    echo "package manager      : ${PM:-none found}"
  fi
}

need_root() { [ "$(id -u)" -eq 0 ] || die "run as root (sudo bluforge-bluez $*)"; }

record_log() { mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null; echo "$(date -Is) $*" >> "$LOG_FILE" 2>/dev/null || true; }

# ---- install (end-user) ------------------------------------------------------------------
NOTICE='
  BlueZ is third-party open-source software (the Linux Bluetooth stack, GPL/LGPL licensed).
  It is NOT supplied by Candela Technologies. This step downloads it from your operating
  system vendor'"'"'s package repository (or installs the packages you provide) and installs
  it on this machine under its own licence terms. BluForge needs it to run.
'

cmd_install() {
  local accept=0 offline="" dry=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --accept) accept=1;;
      --offline-dir) offline="${2:-}"; shift;;
      --dry-run) dry=1;;
      *) die "unknown option: $1";;
    esac; shift
  done
  [ -n "$PM" ] || die "no supported package manager found (need dnf or apt-get)"
  local pkgs; pkgs="$(install_pkgs)"
  echo -e "$NOTICE"
  log "packages to install : $pkgs  (via $PM${offline:+, from $offline})"
  if [ "$dry" = 1 ]; then
    ok "dry run - nothing installed. Would run:"
    if [ -n "$offline" ]; then
      [ "$PM" = dnf ] && echo "  dnf install -y --disablerepo='*' $offline/*.rpm" || echo "  apt-get install -y $offline/*.deb"
    else
      [ "$PM" = dnf ] && echo "  dnf install -y $pkgs" || echo "  apt-get update && apt-get install -y $pkgs"
    fi
    echo "  systemctl unmask bluetooth.service dbus-org.bluez.service; systemctl enable --now bluetooth.service"
    return 0
  fi
  need_root install
  if [ "$accept" != 1 ]; then
    [ -t 0 ] || die "not a terminal: re-run with --accept to confirm you have read the notice above"
    read -r -p "Install BlueZ now? [y/N] " ans
    case "$ans" in y|Y|yes|YES) ;; *) die "cancelled";; esac
  fi
  record_log "install start pm=$PM pkgs=$pkgs offline=${offline:-no}"
  local rc=0
  if [ -n "$offline" ]; then
    [ -d "$offline" ] || die "--offline-dir $offline is not a directory"
    if [ "$PM" = dnf ]; then
      ls "$offline"/*.rpm >/dev/null 2>&1 || die "no .rpm files in $offline"
      dnf install -y --disablerepo='*' "$offline"/*.rpm || rc=$?
    else
      ls "$offline"/*.deb >/dev/null 2>&1 || die "no .deb files in $offline"
      apt-get install -y "$offline"/*.deb || rc=$?
    fi
  elif [ "$PM" = dnf ]; then
    dnf install -y $pkgs || rc=$?
  else
    apt-get update || true
    apt-get install -y $pkgs || rc=$?
  fi
  [ $rc -eq 0 ] || { record_log "install FAILED rc=$rc"; die "package installation failed (exit $rc) - check network/repository access, or use --offline-dir"; }
  # an imaged unit may have bluetooth masked/disabled (lf_kinstall does this); undo that
  systemctl unmask bluetooth.service dbus-org.bluez.service 2>/dev/null || true
  systemctl enable --now bluetooth.service 2>/dev/null || warn "could not start bluetooth.service - check: systemctl status bluetooth"
  bluez_present || die "installation finished but bluetoothd/bluetoothctl were not found"
  mkdir -p "$STATE_DIR"
  printf 'installed %s by end-user via bluforge-bluez (%s)\n' "$(date -Is)" "$pkgs" > "$M_USER"
  record_log "install OK"
  ok "BlueZ installed: $(bluetoothctl --version 2>/dev/null)"
  ok "bluetooth.service: $(systemctl is-active bluetooth.service 2>/dev/null)"
  echo "Reload the BluForge page - its features are now available."
}

# ---- remove --staging (Candela imaging) ---------------------------------------------------
cmd_remove() {
  local staging=0 yes=0 dry=0 force=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --staging) staging=1;; --yes) yes=1;; --dry-run) dry=1;; --force) force=1;;
      *) die "unknown option: $1";;
    esac; shift
  done
  [ "$staging" = 1 ] || die "refusing: 'remove' is for initial staging only - pass --staging"
  [ -n "$PM" ] || die "no supported package manager found"
  if [ -e "$M_USER" ] && [ "$force" != 1 ]; then
    die "BlueZ was installed by the end-user ($M_USER) - not removing it. (--force overrides; do not do this on a customer unit)"
  fi
  if [ -e "$M_STAGED" ] && [ "$force" != 1 ]; then
    ok "staging removal already done on $(cut -d' ' -f2 "$M_STAGED" 2>/dev/null) - nothing to do"
    return 0
  fi
  local pkgs
  if [ "$PM" = dnf ]; then
    # The daemon + tools only. bluez-libs (libbluetooth) is a shared library that ffmpeg, chromium,
    # wireplumber and others link against - removing it takes those with it, so it stays.
    pkgs="$(rpm -qa --qf '%{NAME}\n' 2>/dev/null | grep -E '^bluez' | grep -v -E '^bluez-libs(-devel)?$' | sort -u | tr '\n' ' ')"
  else
    pkgs="bluez bluez-tools libspa-0.2-bluetooth"
  fi
  if [ -z "${pkgs// /}" ] || ! bluez_present; then
    log "BlueZ is not installed - recording staged state"
    [ "$dry" = 1 ] || { need_root remove; mkdir -p "$STATE_DIR"; printf 'staged %s (already absent)\n' "$(date -Is)" > "$M_STAGED"; }
    return 0
  fi
  log "BlueZ packages found: $pkgs"
  log "preview of the removal (the package manager also removes anything that depends on BlueZ):"
  if [ "$PM" = dnf ]; then
    dnf remove --assumeno --setopt=clean_requirements_on_remove=False $pkgs 2>&1 | sed -n '/Dependencies resolved/,/Transaction Summary/p' | sed 's/^/    /'
  else
    apt-get remove --purge -s $pkgs 2>&1 | grep '^Remv' | sed 's/^/    /'
  fi
  if [ "$dry" = 1 ]; then ok "dry run - nothing removed"; return 0; fi
  need_root remove
  [ "$yes" = 1 ] || die "preview only: re-run with --yes to actually remove these packages"
  record_log "staging removal start: $pkgs"
  local rc=0
  if [ "$PM" = dnf ]; then dnf remove -y --setopt=clean_requirements_on_remove=False $pkgs || rc=$?; else apt-get remove --purge -y $pkgs || rc=$?; fi
  [ $rc -eq 0 ] || { record_log "staging removal FAILED rc=$rc"; die "removal failed (exit $rc)"; }
  mkdir -p "$STATE_DIR"
  printf 'staged %s (%s)\n' "$(date -Is)" "$pkgs" > "$M_STAGED"
  record_log "staging removal OK"
  ok "BlueZ removed for staging. The end-user installs it with: sudo bluforge-bluez install"
}

case "${1:-}" in
  status)  shift; cmd_status "$@";;
  install) shift; cmd_install "$@";;
  remove)  shift; cmd_remove "$@";;
  *) sed -n '2,24p' "$0"; exit 2;;
esac
