#!/bin/sh
# Install a dedicated uConsole launcher without changing global terminal settings.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
BIN_DIR="$HOME/.local/bin"

APP_CONFIG_DIR="$CONFIG_HOME/meshtasticpass"
LXTERMINAL_DIR="$CONFIG_HOME/lxterminal"
LABWC_DIR="$CONFIG_HOME/labwc"
APPLICATIONS_DIR="$DATA_HOME/applications"

PROFILE_FILE="$LXTERMINAL_DIR/lxterminal-meshtasticpass.conf"
LABWC_CONFIG="$LABWC_DIR/rc.xml"
PROJECT_FILE="$APP_CONFIG_DIR/project-dir"
RUNNER="$BIN_DIR/meshtasticpass-run"
LAUNCHER="$BIN_DIR/meshtasticpass-launch"
DESKTOP_FILE="$APPLICATIONS_DIR/meshtasticpass.desktop"

mkdir -p \
    "$APP_CONFIG_DIR" \
    "$LXTERMINAL_DIR" \
    "$LABWC_DIR" \
    "$APPLICATIONS_DIR" \
    "$BIN_DIR"

# The runner reads this file so paths remain safe even if the repository moves.
printf '%s\n' "$SCRIPT_DIR" > "$PROJECT_FILE"

cat > "$PROFILE_FILE" <<'EOF'
[general]
fontname=Monospace 13
hidemenubar=true
hidescrollbar=true
EOF

cat > "$RUNNER" <<'EOF'
#!/bin/sh
set -eu

CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
PROJECT_FILE="$CONFIG_HOME/meshtasticpass/project-dir"

if [ ! -r "$PROJECT_FILE" ]; then
    printf '%s\n' "MeshtasticPass project location is missing." >&2
    printf '%s\n' "Run install-launcher.sh from the project directory." >&2
    exit 1
fi

IFS= read -r PROJECT_DIR < "$PROJECT_FILE"
cd "$PROJECT_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
else
    PYTHON=python3
fi

exec "$PYTHON" "$PROJECT_DIR/app.py"
EOF

cat > "$LAUNCHER" <<'EOF'
#!/bin/sh
set -eu

CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
PROJECT_FILE="$CONFIG_HOME/meshtasticpass/project-dir"
RUNNER="$HOME/.local/bin/meshtasticpass-run"

if [ ! -r "$PROJECT_FILE" ]; then
    printf '%s\n' "MeshtasticPass launcher is not configured." >&2
    exit 1
fi

IFS= read -r PROJECT_DIR < "$PROJECT_FILE"

exec lxterminal \
    --no-remote \
    --profile=meshtasticpass \
    --title=MeshtasticPass \
    --working-directory="$PROJECT_DIR" \
    --command="$RUNNER"
EOF

chmod 755 "$RUNNER" "$LAUNCHER"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=MeshtasticPass
Comment=Keyboard-first Meshtastic companion
Exec=$LAUNCHER
TryExec=lxterminal
Icon=utilities-terminal
Terminal=false
Categories=Utility;
StartupNotify=true
EOF

install_fullscreen_rule() {
    begin_marker='<!-- BEGIN MeshtasticPass launcher -->'
    end_marker='<!-- END MeshtasticPass launcher -->'

    if [ ! -f "$LABWC_CONFIG" ]; then
        cat > "$LABWC_CONFIG" <<EOF
<?xml version="1.0"?>
<labwc_config>
  $begin_marker
  <windowRules>
    <windowRule identifier="lxterminal" title="MeshtasticPass*">
      <action name="ToggleFullscreen" />
    </windowRule>
  </windowRules>
  $end_marker
</labwc_config>
EOF
        return
    fi

    cleaned=$(mktemp)
    updated=$(mktemp)
    trap 'rm -f "$cleaned" "$updated"' EXIT HUP INT TERM

    awk '
        index($0, "<!-- BEGIN MeshtasticPass launcher -->") {
            skipping = 1
            next
        }
        index($0, "<!-- END MeshtasticPass launcher -->") {
            skipping = 0
            next
        }
        !skipping { print }
        END { if (skipping) exit 2 }
    ' "$LABWC_CONFIG" > "$cleaned"

    awk '
        !inserted && /<\/(labwc_config|openbox_config)>/ {
            print "  <!-- BEGIN MeshtasticPass launcher -->"
            print "  <windowRules>"
            print "    <windowRule identifier=\"lxterminal\" title=\"MeshtasticPass*\">"
            print "      <action name=\"ToggleFullscreen\" />"
            print "    </windowRule>"
            print "  </windowRules>"
            print "  <!-- END MeshtasticPass launcher -->"
            inserted = 1
        }
        { print }
        END { if (!inserted) exit 3 }
    ' "$cleaned" > "$updated"

    cat "$updated" > "$LABWC_CONFIG"
    rm -f "$cleaned" "$updated"
    trap - EXIT HUP INT TERM
}

install_fullscreen_rule

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

if command -v labwc >/dev/null 2>&1 && [ -n "${LABWC_PID:-}" ]; then
    labwc --reconfigure >/dev/null 2>&1 || {
        printf '%s\n' "Launcher installed; log in again to activate fullscreen." >&2
    }
fi

printf '%s\n' "MeshtasticPass launcher installed."
printf '%s\n' "Menu: Startup / Accessories / MeshtasticPass"
