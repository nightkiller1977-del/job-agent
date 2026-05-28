#!/usr/bin/env bash
# Syncs the latest Jobright Autofill extension from Chrome to the project.
# Run manually after Chrome auto-updates the extension, or any time.

CHROME_EXT="$HOME/Library/Application Support/Google/Chrome/Default/Extensions/odcnpipkhjegpefkfplmedhmkmmhmoko"
PROJECT_EXT="./state/extensions/jobright-autofill"

LATEST=$(ls -d "$CHROME_EXT"/*/ 2>/dev/null | sort -V | tail -1)
if [ -z "$LATEST" ]; then
  echo "Error: Jobright extension not found in Chrome. Install it from:"
  echo "  https://chromewebstore.google.com/detail/odcnpipkhjegpefkfplmedhmkmmhmoko"
  exit 1
fi

VERSION=$(basename "$LATEST" | sed 's/_0$//')
echo "Syncing version $VERSION → $PROJECT_EXT"
rm -rf "$PROJECT_EXT"
cp -r "$LATEST" "$PROJECT_EXT"
echo "Done. Extension is at $PROJECT_EXT"
