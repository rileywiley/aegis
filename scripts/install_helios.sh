#!/bin/bash
# Install Helios to /Applications and set up LaunchAgent
set -euo pipefail

APP_SRC="helios/dist/Helios.app"
APP_DST="/Applications/Helios.app"
PLIST_DST="$HOME/Library/LaunchAgents/com.aegis.helios.plist"
DAEMON_LABEL="com.aegis.helios"

if [ ! -d "$APP_SRC" ]; then
    echo "ERROR: $APP_SRC not found. Run scripts/build_helios.sh first."
    exit 1
fi

# Ensure log directory exists
mkdir -p "$HOME/.aegis/capture/logs"

# Stop existing daemon if running
launchctl bootout "gui/$(id -u)/$DAEMON_LABEL" 2>/dev/null || true
sleep 1

# Copy app bundle
echo "Installing Helios.app to /Applications..."
rm -rf "$APP_DST"
cp -R "$APP_SRC" "$APP_DST"

# Write LaunchAgent plist for daemon auto-start
echo "Writing LaunchAgent plist..."
mkdir -p "$(dirname "$PLIST_DST")"
cat > "$PLIST_DST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$DAEMON_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Applications/Helios.app/Contents/MacOS/Helios</string>
        <string>--daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- WhisperX / faster-whisper subprocess-spawn ffmpeg to decode
             audio. launchd starts daemons with a minimal PATH that does
             NOT include /opt/homebrew/bin where Homebrew ffmpeg lives,
             so transcription fails with "[Errno 2] No such file or
             directory: 'ffmpeg'". Inject a richer PATH so subprocess
             lookups find ffmpeg. -->
        <key>PATH</key>
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>$HOME/.aegis/capture/logs/daemon-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.aegis/capture/logs/daemon-stderr.log</string>
</dict>
</plist>
PLIST

# Load daemon
echo "Loading daemon..."
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

# Wait and verify
sleep 3
if curl -s http://127.0.0.1:3031/v1/health > /dev/null 2>&1; then
    echo "Helios daemon is running. Health check OK."
else
    echo "Warning: daemon may still be starting. Check: curl http://127.0.0.1:3031/v1/health"
fi

# Register Helios.app as a Login Item so the menu-bar UI auto-starts on
# login. The daemon is handled by the LaunchAgent above; this Login
# Item handles the GUI half so the user never has to launch it
# manually. Idempotent: we strip any existing entry before re-adding.
echo "Registering Helios.app as a Login Item (menu-bar UI auto-start)..."
osascript >/dev/null 2>&1 <<'OSA' || true
tell application "System Events"
    if login item "Helios" exists then
        delete login item "Helios"
    end if
    make login item at end with properties {path:"/Applications/Helios.app", hidden:false, name:"Helios"}
end tell
OSA

# Launch the menu-bar UI for the current session so the user sees the
# icon now without logging out and back in.
open -a "/Applications/Helios.app"

echo "Helios installed."
echo
echo "To uninstall:"
echo "  launchctl bootout gui/\$(id -u)/$DAEMON_LABEL"
echo "  rm \"$PLIST_DST\""
echo "  rm -rf \"$APP_DST\""
echo "  osascript -e 'tell application \"System Events\" to delete login item \"Helios\"'"
