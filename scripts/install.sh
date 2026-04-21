#!/usr/bin/env bash
# ==============================================================================
# install.sh – Production installer for NAP (Network Audio Player)
#
# Usage:
#   sudo bash scripts/install.sh [--no-apt] [--no-services] [--dev]
#
# Flags:
#   --no-apt        Skip apt package installation (useful when already done)
#   --no-services   Install files only; do not enable/start systemd units
#   --dev           Skip hardware-only packages (GPIO, LCD) for dev machines
#
# Idempotency:
#   Every step is guarded by an existence / state check so repeated runs are
#   safe and do not re-create or overwrite customised configuration.
#
# Prerequisites:
#   - Raspberry Pi OS (Bookworm / Bullseye) or compatible Debian derivative
#   - Run as root (sudo)
#   - Git repository already cloned to the target path
# ==============================================================================

set -euo pipefail
IFS=$'\n\t'

# ──────────────────────────────────────────────────────────────────────────────
# Resolver: the canonical repo root is the directory containing this script's
# parent directory, regardless of cwd.
# ──────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[NAP]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()     { echo -e "${RED}[ERR]${NC}  $*" >&2; exit 1; }

# ── Argument parsing ──────────────────────────────────────────────────────────
OPT_NO_APT=0
OPT_NO_SERVICES=0
OPT_DEV=0
for arg in "$@"; do
    case "$arg" in
        --no-apt)       OPT_NO_APT=1 ;;
        --no-services)  OPT_NO_SERVICES=1 ;;
        --dev)          OPT_DEV=1 ;;
        --help|-h)
            sed -n '2,/^#.*==/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0
            ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "This script must be run as root.  Use: sudo bash $0"

# ── Architecture / OS sanity check ───────────────────────────────────────────
if [[ ! -f /etc/os-release ]]; then
    die "/etc/os-release not found – unsupported OS."
fi
# shellcheck source=/dev/null
source /etc/os-release
if [[ "${ID:-}" != "raspbian" && "${ID:-}" != "debian" && "${ID_LIKE:-}" != *"debian"* ]]; then
    warn "OS '${PRETTY_NAME:-unknown}' is not Raspbian/Debian – proceeding anyway."
fi

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
NAP_USER="nap"
NAP_GROUP="nap"
INSTALL_DIR="/opt/nap"
CONFIG_DIR="/etc/nap"
LOG_DIR="/var/log/nap"
RUN_DIR="/var/run/nap"
LIB_DIR="/var/lib/nap"

PYTHON="python3"
PIP="pip3"
SYSTEMD_DIR="/etc/systemd/system"
POLKIT_RULES_FILE="/etc/polkit-1/rules.d/10-nap.rules"
LOGROTATE_FILE="/etc/logrotate.d/nap"
UDEV_RULES_FILE="/etc/udev/rules.d/99-nap.rules"
TMPFILES_CONF="/etc/tmpfiles.d/nap.conf"
SYSLOG_CONF="/etc/rsyslog.d/nap.conf"
BACKEND_SERVICE="nap-backend.service"

# ──────────────────────────────────────────────────────────────────────────────
# Helper: install system packages (idempotent – only those not yet installed)
# ──────────────────────────────────────────────────────────────────────────────
apt_install() {
    local pkgs=()
    for pkg in "$@"; do
        if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            pkgs+=("$pkg")
        fi
    done
    if [[ ${#pkgs[@]} -gt 0 ]]; then
        info "Installing packages: ${pkgs[*]}"
        apt-get install -y --no-install-recommends "${pkgs[@]}"
    else
        success "All requested apt packages already installed."
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# Helper: build and install BlueALSA from source.
#
# BlueALSA was removed from Raspberry Pi OS Bookworm's default repos. We build
# the latest release from GitHub, which supports Bookworm / BlueZ 5.66+ natively.
#
# Binary names changed in v4.4.0:
#   bluealsa  →  bluealsad   (daemon)
#   bluealsa-cli  →  bluealsactl  (control utility)
#   bluealsa-aplay is unchanged.
#
# After installation a compat symlink /usr/bin/bluealsa → bluealsad is created
# so any scripts that reference the old name continue to work.
#
# Idempotent: skips the entire build when bluealsad (or the legacy bluealsa)
# binary is already present in PATH.
# ──────────────────────────────────────────────────────────────────────────────
build_bluealsa() {
    if command -v bluealsad &>/dev/null || \
       { command -v bluealsa &>/dev/null && [[ ! -L "$(command -v bluealsa)" ]]; }; then
        success "BlueALSA already installed – skipping build."
        return 0
    fi

    info "BlueALSA not in apt repos for this OS – building from source."
    info "  Source : https://github.com/arkq/bluez-alsa (latest HEAD)"
    info "  This takes 2–5 minutes on a Raspberry Pi 4."

    local BUILD_DIR
    BUILD_DIR="$(mktemp -d /tmp/bluez-alsa-build.XXXXXX)"
    # Always clean up the temp build tree, even on error.
    # shellcheck disable=SC2064
    trap "rm -rf '$BUILD_DIR'" RETURN

    # ── Build-time dependencies ───────────────────────────────────────────────
    # build-essential/gcc are already in BASE_PACKAGES; listed here for clarity.
    apt_install \
        git automake libtool pkg-config python3-docutils \
        libasound2-dev libbluetooth-dev libdbus-1-dev \
        libglib2.0-dev libsbc-dev

    # ── Clone (shallow – we only need HEAD) ───────────────────────────────────
    info "Cloning bluez-alsa …"
    git clone --depth 1 https://github.com/arkq/bluez-alsa.git "$BUILD_DIR"

    pushd "$BUILD_DIR" > /dev/null

    autoreconf --install --force

    mkdir _build && cd _build
    # Configuration notes:
    #   --enable-faststream : royalty-free Google FastStream codec (better
    #                         latency than SBC, no AAC license needed)
    #   --enable-upower     : report battery level to connected BT devices
    #   No --enable-systemd : NAP owns its own service unit (bluetooth-audio.service)
    ../configure \
        --enable-faststream \
        --enable-upower \
        --with-alsaplugindir="$(pkg-config --variable=libdir alsa)/alsa-lib"

    make -j"$(nproc)" CFLAGS="-O2 -s"
    make install
    ldconfig

    popd > /dev/null

    # Create compat symlink so scripts referencing the old 'bluealsa' name work.
    if command -v bluealsad &>/dev/null && [[ ! -e /usr/bin/bluealsa ]]; then
        ln -sf /usr/bin/bluealsad /usr/bin/bluealsa
        info "Created compat symlink /usr/bin/bluealsa → bluealsad"
    fi

    success "BlueALSA built and installed."
}

# ──────────────────────────────────────────────────────────────────────────────
# Helper: create a system user if it doesn't exist
# ──────────────────────────────────────────────────────────────────────────────
ensure_user() {
    local user="$1" home="$2" groups="${3:-}"
    if id "$user" &>/dev/null; then
        success "User '$user' already exists."
    else
        info "Creating system user '$user' (home=$home)."
        useradd \
            --system \
            --home-dir "$home" \
            --create-home \
            --shell /usr/sbin/nologin \
            --comment "NAP service account" \
            "$user"
    fi
    # Idempotently add supplementary groups
    if [[ -n "$groups" ]]; then
        for grp in $(echo "$groups" | tr ',' ' '); do
            if getent group "$grp" &>/dev/null; then
                usermod -aG "$grp" "$user" 2>/dev/null || true
            else
                warn "Group '$grp' does not exist – skipping."
            fi
        done
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# Helper: create directory with owner + permissions (idempotent)
# ──────────────────────────────────────────────────────────────────────────────
ensure_dir() {
    local path="$1" owner="$2" mode="${3:-755}"
    if [[ ! -d "$path" ]]; then
        info "Creating directory $path"
        mkdir -p "$path"
    fi
    chown "$owner" "$path"
    chmod "$mode" "$path"
}

# ──────────────────────────────────────────────────────────────────────────────
# Helper: copy a file only if dest is missing or content differs
# ──────────────────────────────────────────────────────────────────────────────
install_file() {
    local src="$1" dst="$2" mode="${3:-644}"
    if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
        install -Dm "$mode" "$src" "$dst"
        success "Installed $dst"
    else
        success "Up-to-date: $dst"
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# Step 0 – Preflight
# ──────────────────────────────────────────────────────────────────────────────
info "======================================================"
info " NAP Installer"
info " Repo:    $REPO_ROOT"
info " Install: $INSTALL_DIR"
info "======================================================"

command -v systemctl &>/dev/null || die "systemd is required but not found."
command -v "$PYTHON"  &>/dev/null || die "$PYTHON is required but not found."
command -v git        &>/dev/null || die "git is required but not found."

# ── Detect existing installation ──────────────────────────────────────────────
# Show a clear banner when NAP is already installed so the user knows this is
# a repair/upgrade run, not a first install.  All steps remain idempotent.
if systemctl is-active --quiet "$BACKEND_SERVICE" 2>/dev/null && \
   [[ -f "$INSTALL_DIR/venv/bin/uvicorn" ]] && \
   [[ -f "$CONFIG_DIR/config.json" ]] && \
   [[ -f "$SYSTEMD_DIR/$BACKEND_SERVICE" ]]; then
    echo -e "${GREEN}[NAP]${NC} Existing installation detected – running in refresh/repair mode."
    echo -e "      Version : $(cat $INSTALL_DIR/VERSION 2>/dev/null || echo 'unknown')"
    echo -e "      Backend : $(systemctl show -p MainPID --value $BACKEND_SERVICE) (PID)"
    echo -e "      Config  : $CONFIG_DIR/config.json"
    echo
fi

# ──────────────────────────────────────────────────────────────────────────────
# Step 1 – System packages
# ──────────────────────────────────────────────────────────────────────────────
if [[ $OPT_NO_APT -eq 0 ]]; then
    info "--- Step 1: System packages ---"
    apt-get update -qq

    BASE_PACKAGES=(
        # Audio daemons
        mpd mpc
        shairport-sync
        bluez bluez-tools
        # System tools required by the backend
        git
        python3 python3-pip python3-venv python3-dev
        # I2C / GPIO
        i2c-tools
        # Build tools (for Python packages with C extensions)
        gcc build-essential libffi-dev libssl-dev
        # Avahi (required for AirPlay mDNS)
        avahi-daemon avahi-utils
        # For smbus2 / RPLCD
        python3-smbus
        # polkit – D-Bus authorisation for systemctl isolate without sudo
        policykit-1
        # Misc
        curl wget jq
    )

    # On Bookworm, raspi-gpio and libraspberrypi-bin were superseded by the
    # raspi-utils-* split packages.  Detect which set is available and use it.
    if apt-cache show raspi-utils-core &>/dev/null 2>&1; then
        # Bookworm / newer RPi OS
        HARDWARE_PACKAGES=(raspi-utils-core raspi-utils-dt)
    else
        # Bullseye / older RPi OS
        HARDWARE_PACKAGES=(raspi-gpio libraspberrypi-bin)
    fi

    apt_install "${BASE_PACKAGES[@]}"

    # BlueALSA was removed from Raspberry Pi OS Bookworm's default repos.
    # Build from source if not already installed.
    build_bluealsa

    if [[ $OPT_DEV -eq 0 ]]; then
        apt_install "${HARDWARE_PACKAGES[@]}"
    else
        info "  --dev mode: skipping hardware packages."
    fi

    success "System packages ready."
fi

# ──────────────────────────────────────────────────────────────────────────────
# Step 2 – Service account and groups
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 2: Service account ---"
ensure_user "$NAP_USER" "$INSTALL_DIR" "audio,video,gpio,i2c,bluetooth,input"

# Also ensure MPD and shairport-sync users exist (they're installed by apt
# but we check here for robustness)
if ! id mpd &>/dev/null; then
    useradd --system --home /var/lib/mpd --shell /usr/sbin/nologin --comment "MPD" mpd
fi
if ! id shairport-sync &>/dev/null; then
    useradd --system --home /var/run/shairport-sync --shell /usr/sbin/nologin \
        --comment "shairport-sync" shairport-sync
    groupadd -f shairport-sync
    usermod -aG audio shairport-sync
fi
if ! id plexamp &>/dev/null; then
    useradd --system --home /opt/plexamp --create-home \
        --shell /usr/sbin/nologin --comment "Plexamp" plexamp
    usermod -aG audio plexamp
fi
success "Service accounts ready."

# ──────────────────────────────────────────────────────────────────────────────
# Step 3 – Directory structure
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 3: Directory structure ---"
ensure_dir "$INSTALL_DIR"          "$NAP_USER:$NAP_GROUP"  "755"
ensure_dir "$CONFIG_DIR"           "$NAP_USER:$NAP_GROUP"  "750"
ensure_dir "$LOG_DIR"              "$NAP_USER:$NAP_GROUP"  "750"
ensure_dir "$LIB_DIR"              "$NAP_USER:$NAP_GROUP"  "755"
ensure_dir "$LIB_DIR/ota"          "$NAP_USER:$NAP_GROUP"  "755"
ensure_dir "/opt/plexamp"          "plexamp:audio"         "755"
success "Directories ready."

# /var/run/nap is a tmpfs mount on boot – use systemd-tmpfiles
info "Configuring tmpfiles for $RUN_DIR"
cat > "$TMPFILES_CONF" <<EOF
# NAP runtime directory (recreated on boot)
d $RUN_DIR          0755 $NAP_USER $NAP_GROUP -
f /var/run/audio.lock 0660 $NAP_USER audio       -
EOF
systemd-tmpfiles --create "$TMPFILES_CONF"
success "tmpfiles configured."

# ──────────────────────────────────────────────────────────────────────────────
# Step 4 – Clone / sync application code into INSTALL_DIR
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 4: Application code ---"
if [[ "$REPO_ROOT" != "$INSTALL_DIR" ]]; then
    # Running installer from a development checkout; sync to INSTALL_DIR.
    info "Syncing $REPO_ROOT → $INSTALL_DIR"
    # Exclude build/cache artefacts.  .git is intentionally NOT excluded so that
    # /opt/nap remains a valid git repository and OTA updates work.
    # On re-installs, preserve the existing .git to avoid overwriting OTA history.
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
            --exclude='.pytest_cache' --exclude='tests/' --exclude='*.egg-info' \
            "$REPO_ROOT/" "$INSTALL_DIR/"
        info "Existing .git preserved (re-install / repair run)."
    else
        rsync -a --exclude='__pycache__' --exclude='*.pyc' \
            --exclude='.pytest_cache' --exclude='tests/' --exclude='*.egg-info' \
            "$REPO_ROOT/" "$INSTALL_DIR/"
        info "Copied .git to $INSTALL_DIR (required for OTA)."
    fi
    chown -R "$NAP_USER:$NAP_GROUP" "$INSTALL_DIR"
    success "Code synced."
else
    info "Already running from $INSTALL_DIR – skipping sync."
fi

# Ensure git remote is set (needed by OTA updater)
if git -C "$INSTALL_DIR" remote get-url origin &>/dev/null; then
    success "git remote 'origin' already configured."
else
    warn "git remote 'origin' is not set.  OTA updates will not work."
    warn "Configure it with: git -C $INSTALL_DIR remote add origin <url>"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Step 5 – Python virtual environment + dependencies
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 5: Python environment ---"
VENV_DIR="$INSTALL_DIR/venv"

if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment at $VENV_DIR"
    "$PYTHON" -m venv --system-site-packages "$VENV_DIR"
fi

VENV_PIP="$VENV_DIR/bin/pip"
VENV_PYTHON="$VENV_DIR/bin/python"

info "Upgrading pip + wheel"
"$VENV_PIP" install --quiet --upgrade pip wheel

info "Installing Python dependencies"
"$VENV_PIP" install --quiet -r "$INSTALL_DIR/backend/requirements.txt"

# Hardware packages (RPLCD, smbus2, RPi.GPIO)
if [[ $OPT_DEV -eq 0 ]]; then
    "$VENV_PIP" install --quiet RPLCD smbus2 RPi.GPIO evdev
else
    # Install mock-friendly fallback packages only
    "$VENV_PIP" install --quiet RPLCD smbus2 evdev
    warn "--dev: RPi.GPIO not installed (MockGPIO will be used at runtime)."
fi

chown -R "$NAP_USER:$NAP_GROUP" "$VENV_DIR"
success "Python environment ready at $VENV_DIR"

# ──────────────────────────────────────────────────────────────────────────────
# Step 5b – Plexamp Headless
# Plexamp is not in apt; it must be downloaded from plexamp.plex.tv.
# The binary is extracted to /opt/plexamp.  A one-time interactive auth step
# (see docs/INSTALL.md §7) is still required before the service can start.
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 5b: Plexamp Headless ---"

# 1. Ensure Node.js is present (Plexamp is an Electron/Node app)
if ! command -v node &>/dev/null; then
    if [[ $OPT_NO_APT -eq 0 ]]; then
        info "Node.js not found – installing from NodeSource (LTS)..."
        # Fetch and run the NodeSource setup script for Node 20 LTS
        NODESOURCE_SCRIPT="$(mktemp)"
        curl -fsSL "https://deb.nodesource.com/setup_20.x" -o "$NODESOURCE_SCRIPT"
        bash "$NODESOURCE_SCRIPT"
        rm -f "$NODESOURCE_SCRIPT"
        apt-get install -y --no-install-recommends nodejs
        success "Node.js $(node --version) installed."
    else
        warn "Node.js not found and --no-apt is set.  Plexamp requires Node.js."
        warn "Install it manually: https://nodejs.org"
    fi
else
    success "Node.js $(node --version) already installed."
fi

# 2. Install Plexamp binaries if not already present
PLEXAMP_BIN="/opt/plexamp/js/index.js"
if [[ -f "$PLEXAMP_BIN" ]]; then
    success "Plexamp already installed at /opt/plexamp"
else
    info "Fetching Plexamp Headless download URL..."
    PLEXAMP_URL=""
    # Query the official Plex version manifest
    VERSION_JSON="$(curl -fsSL --max-time 10 \
        'https://plexamp.plex.tv/headless/version.json' 2>/dev/null || true)"
    if [[ -n "$VERSION_JSON" ]]; then
        PLEXAMP_URL="$(printf '%s' "$VERSION_JSON" | \
            "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('updateUrl',''))" \
            2>/dev/null || true)"
    fi

    if [[ -z "$PLEXAMP_URL" ]]; then
        warn "Could not automatically determine the Plexamp download URL."
        warn "Download manually from https://plexamp.com/headless/ and extract to /opt/plexamp"
        warn "Then run: sudo -u plexamp node /opt/plexamp/js/index.js"
        warn "Then start: sudo systemctl start plexamp.service"
    else
        info "Downloading Plexamp: $PLEXAMP_URL"
        PLEXAMP_TARBALL="$(mktemp /tmp/plexamp.XXXXXX.tar.bz2)"
        if curl -fsSL --max-time 120 --progress-bar -o "$PLEXAMP_TARBALL" "$PLEXAMP_URL"; then
            info "Extracting Plexamp to /opt/plexamp ..."
            # Extract as the plexamp user; strip the top-level directory from the tarball
            mkdir -p /opt/plexamp
            tar -xjf "$PLEXAMP_TARBALL" -C /opt/plexamp --strip-components=1
            rm -f "$PLEXAMP_TARBALL"
            chown -R plexamp:audio /opt/plexamp
            success "Plexamp extracted to /opt/plexamp"
            echo
            echo -e "${YELLOW}[NOTICE]${NC} Plexamp requires a one-time interactive auth step:"
            echo -e "         sudo -u plexamp node /opt/plexamp/js/index.js"
            echo -e "         Follow the on-screen claim URL, then Ctrl+C and:"
            echo -e "         sudo systemctl start plexamp.service"
            echo
        else
            rm -f "$PLEXAMP_TARBALL"
            warn "Plexamp download failed.  Install manually from https://plexamp.com/headless/"
        fi
    fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# Step 6 – Default configuration file (never overwrites existing config)
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 6: Configuration ---"
DEFAULT_CONFIG="$CONFIG_DIR/config.json"
if [[ ! -f "$DEFAULT_CONFIG" ]]; then
    info "Writing default config to $DEFAULT_CONFIG"
    cat > "$DEFAULT_CONFIG" <<'EOF'
{
  "default_source": "idle",
  "lock_timeout": 8.0,
  "systemd_verify_timeout": 10.0,
  "lcd_enabled": true,
  "lcd_backlight_timeout": 30,
  "ota_enabled": true,
  "ota_github_repo": "your-org/nap",
  "ota_schedule_cron": "0 3 * * *",
  "api_host": "0.0.0.0",
  "api_port": 8000,
  "log_level": "INFO",
  "log_max_lines": 500
}
EOF
    chown "$NAP_USER:$NAP_GROUP" "$DEFAULT_CONFIG"
    chmod 640 "$DEFAULT_CONFIG"
    success "Default config written."
else
    success "Config already exists – not overwriting: $DEFAULT_CONFIG"
fi

# Symlink so Python's fallback path (config/config.json) also works
CONFIG_SYMLINK="$INSTALL_DIR/config/config.json"
mkdir -p "$(dirname "$CONFIG_SYMLINK")"
if [[ ! -L "$CONFIG_SYMLINK" ]]; then
    ln -sf "$DEFAULT_CONFIG" "$CONFIG_SYMLINK"
    chown -h "$NAP_USER:$NAP_GROUP" "$CONFIG_SYMLINK"
    success "Created config symlink: $CONFIG_SYMLINK → $DEFAULT_CONFIG"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Step 7 – systemd units
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 7: systemd units ---"

# Audio source targets and services from the repo
SYSTEMD_UNITS=(
    audio-mpd.target
    audio-airplay.target
    audio-plexamp.target
    audio-bluetooth.target
    mpd.service
    shairport-sync.service
    plexamp.service
    bluetooth-audio.service
)

for unit in "${SYSTEMD_UNITS[@]}"; do
    src="$INSTALL_DIR/systemd/$unit"
    dst="$SYSTEMD_DIR/$unit"
    if [[ ! -f "$src" ]]; then
        warn "systemd unit not found: $src – skipping."
        continue
    fi
    install_file "$src" "$dst" "644"
done

# nap-backend.service  (generated here so it references the correct venv)
BACKEND_UNIT="$SYSTEMD_DIR/$BACKEND_SERVICE"
if [[ ! -f "$BACKEND_UNIT" ]]; then
    info "Writing $BACKEND_UNIT"
    cat > "$BACKEND_UNIT" <<EOF
[Unit]
Description=NAP Backend API (FastAPI / uvicorn)
Documentation=https://github.com/your-org/nap
After=network-online.target multi-user.target
Wants=network-online.target

[Service]
Type=simple
User=$NAP_USER
Group=$NAP_GROUP
WorkingDirectory=$INSTALL_DIR

# Use the venv interpreter so all pip packages are available.
ExecStart=$VENV_DIR/bin/uvicorn backend.app.main:app \\
    --host 0.0.0.0 \\
    --port 8000 \\
    --no-access-log

# Allow the service to call systemctl for source switching.
# The nap polkit rule (Step 8) grants the necessary permissions.
Environment=PYTHONPATH=$INSTALL_DIR
Environment=NAP_CONFIG_DIR=$CONFIG_DIR

Restart=on-failure
RestartSec=5
TimeoutStartSec=30
TimeoutStopSec=20

# Hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$CONFIG_DIR $LOG_DIR $LIB_DIR $INSTALL_DIR $RUN_DIR /var/run/audio.lock

# Journal
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nap-backend

[Install]
WantedBy=multi-user.target
EOF
    success "Written $BACKEND_UNIT"
else
    success "Already exists (not overwriting): $BACKEND_UNIT"
fi

systemctl daemon-reload
success "systemd daemon reloaded."

# ──────────────────────────────────────────────────────────────────────────────
# Step 8 – polkit rules (authorise systemctl isolate via D-Bus)
# ──────────────────────────────────────────────────────────────────────────────
# The backend service runs with NoNewPrivileges=yes, which prevents sudo from
# escalating privileges.  polkit authorises the D-Bus calls that systemctl
# makes internally, so no privilege escalation is required.
info "--- Step 8: polkit rules ---"
mkdir -p /etc/polkit-1/rules.d
if [[ ! -f "$POLKIT_RULES_FILE" ]]; then
    cat > "$POLKIT_RULES_FILE" <<'EOF'
// NAP – allow the nap service account to isolate audio systemd targets.
// systemctl communicates with systemd over D-Bus; polkit governs that path,
// so no sudo / setuid escalation is needed (compatible with NoNewPrivileges).
polkit.addRule(function(action, subject) {
    var ALLOWED_UNITS = [
        "audio-mpd.target",
        "audio-airplay.target",
        "audio-plexamp.target",
        "audio-bluetooth.target",
        "multi-user.target",
    ];
    if (action.id === "org.freedesktop.systemd1.manage-units" &&
            subject.user === "nap") {
        var unit = action.lookup("unit");
        var verb = action.lookup("verb");
        if (verb === "isolate" && ALLOWED_UNITS.indexOf(unit) !== -1) {
            return polkit.Result.YES;
        }
    }
});
EOF
    chmod 644 "$POLKIT_RULES_FILE"
    success "polkit rules written to $POLKIT_RULES_FILE"
else
    success "Already exists (not overwriting): $POLKIT_RULES_FILE"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Step 9 – udev rules (GPIO / IR / I2C)
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 9: udev rules ---"
cat > "$UDEV_RULES_FILE" <<EOF
# NAP – grant the nap group access to I2C and input event devices.

# I2C (LCD display via I2C bus 1)
SUBSYSTEM=="i2c-dev", KERNEL=="i2c-1", GROUP="i2c", MODE="0660"

# IR receiver (symlinked to /dev/input/ir-keys by this rule)
SUBSYSTEM=="input", ATTRS{name}=="gpio_ir_recv", SYMLINK+="input/ir-keys", GROUP="input", MODE="0660"
SUBSYSTEM=="input", ATTRS{name}=="*ir*",        SYMLINK+="input/ir-keys", GROUP="input", MODE="0660"

# GPIO (BCM GPIO access – used by RPi.GPIO)
SUBSYSTEM=="bcm2835-gpiomem",  GROUP="gpio", MODE="0660"
SUBSYSTEM=="gpio",             GROUP="gpio", MODE="0660"

# Audio lock file permissions on boot (complement to tmpfiles.d)
ACTION=="add", SUBSYSTEM=="platform", RUN+="/bin/chown $NAP_USER:audio /var/run/audio.lock"
EOF
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger          2>/dev/null || true
success "udev rules installed."

# ──────────────────────────────────────────────────────────────────────────────
# Step 10 – /var/run/audio.lock permissions
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 10: Audio lock file ---"
# Create lock file if it does not yet exist (tmpfiles.d will recreate it on
# every boot; this handles the first run before the next reboot).
LOCK_FILE="/var/run/audio.lock"
if [[ ! -f "$LOCK_FILE" ]]; then
    touch "$LOCK_FILE"
    success "Created $LOCK_FILE"
fi
chown "$NAP_USER:audio" "$LOCK_FILE"
chmod 660 "$LOCK_FILE"
# Ensure the nap user is in the audio group (belt-and-suspenders)
usermod -aG audio "$NAP_USER"
success "Audio lock file permissions set."

# ──────────────────────────────────────────────────────────────────────────────
# Step 11 – Log rotation
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 11: Log rotation ---"
cat > "$LOGROTATE_FILE" <<EOF
$LOG_DIR/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 640 $NAP_USER $NAP_GROUP
    postrotate
        systemctl kill --signal=HUP $BACKEND_SERVICE 2>/dev/null || true
    endscript
}
EOF
success "logrotate configured."

# ──────────────────────────────────────────────────────────────────────────────
# Step 12 – ALSA configuration
# ──────────────────────────────────────────────────────────────────────────────
info "--- Step 12: ALSA configuration ---"

# System-wide ALSA config (never overwrite – preserves user customisations)
if [[ ! -f /etc/asound.conf ]]; then
    install_file "$INSTALL_DIR/config/asound.conf" "/etc/asound.conf" "644"
else
    success "Already exists (not overwriting): /etc/asound.conf"
    info "  Reference config available at: $INSTALL_DIR/config/asound.conf"
fi

# Kernel-level ALSA defaults
ALSA_CONF_DIR="/etc/alsa/alsa.conf.d"
mkdir -p "$ALSA_CONF_DIR"
install_file "$INSTALL_DIR/config/90-nap-defaults.conf" \
    "$ALSA_CONF_DIR/90-nap-defaults.conf" "644"

# MPD configuration (never overwrite)
if [[ ! -f /etc/mpd.conf ]]; then
    install_file "$INSTALL_DIR/config/mpd.conf" "/etc/mpd.conf" "640"
    chown root:mpd /etc/mpd.conf
else
    success "Already exists (not overwriting): /etc/mpd.conf"
fi

# shairport-sync configuration (never overwrite)
if [[ ! -f /etc/shairport-sync.conf ]]; then
    install_file "$INSTALL_DIR/config/shairport-sync.conf" \
        "/etc/shairport-sync.conf" "640"
    chown root:shairport-sync /etc/shairport-sync.conf 2>/dev/null || true
else
    success "Already exists (not overwriting): /etc/shairport-sync.conf"
fi

# Restore ALSA mixer state on boot via alsactl
if command -v alsactl &>/dev/null; then
    # Save current state (idempotent – creates /var/lib/alsa/asound.state)
    alsactl store 2>/dev/null || true
    systemctl enable alsa-restore.service 2>/dev/null || true
    success "ALSA state save/restore configured."
fi

success "ALSA configuration done."

# ──────────────────────────────────────────────────────────────────────────────
# Step 13 – Enable and start services
# ──────────────────────────────────────────────────────────────────────────────
if [[ $OPT_NO_SERVICES -eq 0 ]]; then
    info "--- Step 13: Enable and start services ---"

    # Enable core system services
    systemctl enable --now avahi-daemon.service   2>/dev/null || true
    systemctl enable --now bluetooth.service      2>/dev/null || true

    # Enable audio source units (do NOT start them – only one can run at a time;
    # the backend switches via systemctl isolate at runtime)
    for unit in "${SYSTEMD_UNITS[@]}"; do
        if [[ -f "$SYSTEMD_DIR/$unit" ]]; then
            systemctl enable "$unit" 2>/dev/null || true
        fi
    done

    # Enable and start the NAP backend
    systemctl enable "$BACKEND_SERVICE"
    if systemctl is-active --quiet "$BACKEND_SERVICE"; then
        info "Restarting $BACKEND_SERVICE (already running)."
        systemctl restart "$BACKEND_SERVICE"
    else
        info "Starting $BACKEND_SERVICE."
        systemctl start "$BACKEND_SERVICE"
    fi

    # Give it 3 seconds to come up and report status
    sleep 3
    if systemctl is-active --quiet "$BACKEND_SERVICE"; then
        success "$BACKEND_SERVICE is running."
    else
        warn "$BACKEND_SERVICE failed to start.  Check logs with:"
        warn "  journalctl -u $BACKEND_SERVICE -n 50 --no-pager"
    fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# Step 14 – I2C / hardware interface enablement (Raspberry Pi only)
# ──────────────────────────────────────────────────────────────────────────────
if [[ $OPT_DEV -eq 0 ]] && command -v raspi-config &>/dev/null; then
    info "--- Step 14: Enabling I2C and SPI interfaces ---"
    raspi-config nonint do_i2c 0  2>/dev/null && success "I2C enabled." || warn "Could not enable I2C via raspi-config."
    raspi-config nonint do_spi 0  2>/dev/null && success "SPI enabled." || warn "Could not enable SPI via raspi-config."
fi

# ──────────────────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────────────────
info "======================================================"
success " NAP installation complete."
info ""
info " Config:  $DEFAULT_CONFIG"
info " Logs:    journalctl -u $BACKEND_SERVICE -f"
info " API:     http://<pi-address>:8000"
info " Web UI:  http://<pi-address>:8000/"
info ""
if [[ $OPT_NO_SERVICES -eq 0 ]]; then
    info " Status:  systemctl status $BACKEND_SERVICE"
fi
info " To configure audio sources, edit $DEFAULT_CONFIG"
info " or use the Web UI."
info "======================================================"
