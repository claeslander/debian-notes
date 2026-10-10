#!/bin/sh
# Register the source checkout with the desktop for the current user (icon + launcher),
# without building a package. Run with --uninstall to remove it again.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
DESKTOP="$DATA/applications/org.debiannotes.Notes.desktop"
ICON="$DATA/icons/hicolor/scalable/apps/debian-notes.svg"

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$DESKTOP" "$ICON"
  echo "Removed $DESKTOP and $ICON"
else
  install -D -m 644 "$ROOT/data/icons/debian-notes.svg" "$ICON"
  mkdir -p "$(dirname "$DESKTOP")"
  sed "s|^Exec=.*|Exec=$ROOT/bin/debian-notes|" "$ROOT/data/org.debiannotes.Notes.desktop" > "$DESKTOP"
  echo "Installed $DESKTOP and $ICON"
fi

gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$DATA/applications" 2>/dev/null || true
