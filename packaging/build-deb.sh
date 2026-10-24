#!/bin/sh
# Build dist/debian-notes_<version>_all.deb from the source tree.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/debian_notes/__init__.py")
PKG=debian-notes
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

install -d "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/share/$PKG" \
  "$STAGE/usr/share/applications" "$STAGE/usr/share/icons/hicolor/scalable/apps" \
  "$STAGE/usr/share/metainfo" "$STAGE/usr/share/doc/$PKG"

install -m 755 "$ROOT/bin/debian-notes" "$STAGE/usr/bin/debian-notes"
cp -r "$ROOT/src/debian_notes" "$STAGE/usr/share/$PKG/"
find "$STAGE/usr/share/$PKG" -name '__pycache__' -prune -exec rm -rf {} +
install -m 644 "$ROOT/data/org.debiannotes.Notes.desktop" "$STAGE/usr/share/applications/"
install -m 644 "$ROOT/data/icons/debian-notes.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$ROOT/data/org.debiannotes.Notes.metainfo.xml" "$STAGE/usr/share/metainfo/"

{
  echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
  echo "Upstream-Name: $PKG"
  echo ""
  echo "Files: *"
  echo "Copyright: 2026 Lander Claes"
  echo "License: MIT"
  sed 's/^/ /; s/^ $/ ./' "$ROOT/LICENSE"
} > "$STAGE/usr/share/doc/$PKG/copyright"
printf '%s (%s) unstable; urgency=low\n\n  * Release %s.\n\n -- Lander Claes <183724844+claeslander@users.noreply.github.com>  %s\n' \
  "$PKG" "$VERSION" "$VERSION" "$(date -R)" | gzip -9n > "$STAGE/usr/share/doc/$PKG/changelog.Debian.gz"

SIZE=$(du -sk "$STAGE/usr" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: editors
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1
Recommends: fonts-inter, gstreamer1.0-plugins-good
Installed-Size: $SIZE
Maintainer: Lander Claes <183724844+claeslander@users.noreply.github.com>
Description: Notes app with checklists, tables, sketches and locked notes
 A fast, private notes app in the style of Apple Notes: folders and
 subfolders, a note list grouped by date, and a rich editor with titles,
 headings, lists, checklists, tables, images, attachments and sketches.
 Includes pinned notes, hashtags, search, password-locked notes, Recently
 Deleted, gallery view, dark mode and export to PDF, HTML and Markdown.
CONTROL

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr/share" -type f -exec chmod 644 {} +

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/${PKG}_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT"
echo "Built $OUT"
