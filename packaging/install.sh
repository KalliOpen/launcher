#!/bin/sh
set -eu
install -Dm755 packaging/io.kalliopen.Launcher.desktop "$DESTDIR/usr/share/applications/io.kalliopen.Launcher.desktop"
install -Dm644 packaging/io.kalliopen.Launcher.autostart "$DESTDIR/etc/xdg/autostart/io.kalliopen.Launcher.desktop"
install -Dm644 logo.svg "$DESTDIR/usr/share/icons/hicolor/scalable/apps/io.kalliopen.Launcher.svg"
