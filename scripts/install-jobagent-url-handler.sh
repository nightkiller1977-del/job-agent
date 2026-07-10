#!/usr/bin/env bash
# install-jobagent-url-handler.sh
#
# Registers the jobagent:// URL scheme on macOS so that clicking
# jobagent://prepare-sessions?source=linkedin in Telegram (or any app)
# opens Terminal and runs the correct prepare-sessions command.
#
# Usage:
#   bash scripts/install-jobagent-url-handler.sh
#
# Uninstall:
#   bash scripts/install-jobagent-url-handler.sh --uninstall

set -euo pipefail

APP_BUNDLE="$HOME/Applications/JobAgentURLHandler.app"
PLIST="$HOME/Library/LaunchAgents/com.jobagent.urlhandler.plist"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# ── Uninstall ───────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -rf "$APP_BUNDLE" "$PLIST"
  echo "Uninstalled JobAgent URL handler."
  exit 0
fi

# ── Create minimal .app bundle ───────────────────────────────────────────────
echo "Creating $APP_BUNDLE ..."
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Info.plist — registers the jobagent:// URL scheme
cat > "$APP_BUNDLE/Contents/Info.plist" <<INFOPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>JobAgentURLHandler</string>
  <key>CFBundleIdentifier</key>
  <string>com.jobagent.urlhandler</string>
  <key>CFBundleVersion</key>
  <string>1.0</string>
  <key>CFBundleExecutable</key>
  <string>handler</string>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLSchemes</key>
      <array>
        <string>jobagent</string>
      </array>
      <key>CFBundleURLName</key>
      <string>com.jobagent.urlhandler</string>
    </dict>
  </array>
  <key>LSBackgroundOnly</key>
  <false/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
</dict>
</plist>
INFOPLIST

# Shell script that handles the URL and opens Terminal
cat > "$APP_BUNDLE/Contents/MacOS/handler" <<HANDLER
#!/usr/bin/env bash
# Receives the jobagent:// URL as \$1 from macOS LaunchServices
URL="\${1:-}"

# Extract the path and query (e.g. prepare-sessions?source=linkedin)
COMMAND="\$(echo "\$URL" | sed 's|jobagent://||')"
ACTION="\$(echo "\$COMMAND" | cut -d'?' -f1)"
QUERY="\$(echo "\$COMMAND" | cut -d'?' -f2)"

# Parse source= from query string
SOURCE=""
for param in \$(echo "\$QUERY" | tr '&' '\\n'); do
  key="\$(echo "\$param" | cut -d'=' -f1)"
  val="\$(echo "\$param" | cut -d'=' -f2)"
  [[ "\$key" == "source" ]] && SOURCE="\$val"
done

# Validate action and source to prevent command injection
if [[ "\$ACTION" != "prepare-sessions" && "\$ACTION" != "session-status" && "\$ACTION" != "heartbeat" ]]; then
  echo "Error: Forbidden action '\$ACTION'" >&2
  exit 1
fi

if [[ -n "\$SOURCE" ]]; then
  if [[ ! "\$SOURCE" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Error: Invalid source parameter '\$SOURCE'" >&2
    exit 1
  fi
fi

# Build the actual CLI command
PROJECT="$PROJECT_DIR"
if [[ -n "\$SOURCE" ]]; then
  CMD="cd '\$PROJECT' && python src/main.py \$ACTION --source \$SOURCE"
else
  CMD="cd '\$PROJECT' && python src/main.py \$ACTION"
fi

# Open Terminal and run the command
osascript <<APPLESCRIPT
tell application "Terminal"
  activate
  do script "\$CMD"
end tell
APPLESCRIPT
HANDLER

chmod +x "$APP_BUNDLE/Contents/MacOS/handler"

# ── Register with Launch Services ────────────────────────────────────────────
echo "Registering URL scheme with Launch Services ..."
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP_BUNDLE" 2>/dev/null || true

# Also set as default handler for jobagent:// scheme
defaults write com.apple.LaunchServices/com.apple.launchservices.secure LSHandlers \
  -array-add "{LSHandlerURLScheme=jobagent;LSHandlerRoleAll=com.jobagent.urlhandler;}" 2>/dev/null || true

echo ""
echo "✅ JobAgent URL handler installed."
echo "   Test it by running:"
echo "   open 'jobagent://prepare-sessions?source=linkedin'"
echo ""
echo "   When Telegram delivers a jobagent:// link, tapping it will"
echo "   open Terminal and run prepare-sessions automatically."
