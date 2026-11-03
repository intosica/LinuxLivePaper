#!/usr/bin/env bash
# Build LinuxLivePaper as a flatpak, install it for this user and write dist/LinuxLivePaper.flatpak.
#   ./build-flatpak.sh            build + install + bundle
#   ./build-flatpak.sh --no-install
# First run compiles x264 + ffmpeg (several minutes); later runs reuse the cache in .flatpak/.
set -euo pipefail
cd "$(dirname "$0")"
APP=io.github.linuxlivepaper
INSTALL=--install
[[ ${1:-} == --no-install ]] && INSTALL=

command -v flatpak-builder >/dev/null || { echo "flatpak-builder is missing: sudo apt install flatpak-builder" >&2; exit 1; }
flatpak --user remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

flatpak-builder --user --install-deps-from=flathub --force-clean --state-dir=.flatpak/state \
    --repo=.flatpak/repo $INSTALL .flatpak/build "flatpak/$APP.yml"
mkdir -p dist
flatpak build-bundle .flatpak/repo dist/LinuxLivePaper.flatpak "$APP" \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
echo "Bundle: $PWD/dist/LinuxLivePaper.flatpak"
if [[ -n $INSTALL ]]; then echo "Run:    flatpak run $APP"; fi
