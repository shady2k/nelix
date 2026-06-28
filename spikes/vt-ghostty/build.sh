#!/usr/bin/env bash
#
# Reproducible build of the libghostty-vt wasm renderer for the nelix faithful-VT spike.
#
# Produces spikes/vt-ghostty/.build/shim.wasm — a freestanding wasm32 module that bundles
# Ghostty's VT engine plus shim.c's flat ABI, drivable in-process from Python via wasmtime.
#
# Pins:
#   - Zig 0.15.2  (ghostty requires EXACTLY this; Homebrew's newer Zig is rejected)
#   - ghostty commit 07d31666e73bce337b9cece60a884c67fe8906f4 (2026-06-27)
#
# Needs: bash, curl, tar (xz), git, network. Zig is downloaded locally into .build (no root,
# no system install). Everything lands in .build/ which is gitignored.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/.build"
ZIG_VERSION="0.15.2"
GHOSTTY_COMMIT="07d31666e73bce337b9cece60a884c67fe8906f4"
mkdir -p "$BUILD"

# --- 1. Pinned Zig (env $ZIG honored; else downloaded locally; version is enforced) ------
# ghostty rejects any Zig != $ZIG_VERSION at configure time, so we verify, not just presence.
ZIG="${ZIG:-$BUILD/zig/zig}"
need_dl=0
if [ ! -x "$ZIG" ]; then
  need_dl=1
elif [ "$("$ZIG" version 2>/dev/null)" != "$ZIG_VERSION" ]; then
  if [ "$ZIG" = "$BUILD/zig/zig" ]; then
    need_dl=1   # our managed copy is the wrong version — refetch
  else
    echo "!! \$ZIG ($ZIG) is $("$ZIG" version 2>/dev/null), but ghostty needs exactly $ZIG_VERSION" >&2
    exit 1
  fi
fi
if [ "$need_dl" = 1 ]; then
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)   ZARCH="aarch64-macos" ;;
    Darwin-x86_64)  ZARCH="x86_64-macos" ;;
    Linux-x86_64)   ZARCH="x86_64-linux" ;;
    Linux-aarch64)  ZARCH="aarch64-linux" ;;
    *) echo "!! no pinned Zig $ZIG_VERSION mapping for $(uname -s)-$(uname -m); install Zig $ZIG_VERSION manually and pass ZIG=/path/to/zig" >&2; exit 1 ;;
  esac
  url="https://ziglang.org/download/$ZIG_VERSION/zig-$ZARCH-$ZIG_VERSION.tar.xz"
  echo ">> fetching Zig $ZIG_VERSION ($ZARCH)"
  curl -fSL --retry 2 "$url" -o "$BUILD/zig.tar.xz"
  rm -rf "$BUILD/zig" && mkdir -p "$BUILD/zig"
  tar xf "$BUILD/zig.tar.xz" -C "$BUILD/zig" --strip-components=1
  rm -f "$BUILD/zig.tar.xz"
  ZIG="$BUILD/zig/zig"
fi
echo ">> zig $("$ZIG" version)"

# --- 2. Pinned ghostty source (always reconciled to the exact commit) --------------------
GHOSTTY="$BUILD/ghostty"
if [ ! -d "$GHOSTTY/.git" ]; then
  git init -q "$GHOSTTY"
  git -C "$GHOSTTY" remote add origin https://github.com/ghostty-org/ghostty.git
fi
if [ "$(git -C "$GHOSTTY" rev-parse -q --verify HEAD 2>/dev/null)" != "$GHOSTTY_COMMIT" ]; then
  echo ">> fetching ghostty @ $GHOSTTY_COMMIT"
  git -C "$GHOSTTY" fetch -q --depth 1 origin "$GHOSTTY_COMMIT"
  git -C "$GHOSTTY" checkout -q -f FETCH_HEAD
fi

# --- 3. Build libghostty-vt for wasm32-freestanding --------------------------------------
echo ">> building libghostty-vt.a (wasm32-freestanding, ReleaseSmall)"
( cd "$GHOSTTY" && "$ZIG" build -Demit-lib-vt -Dtarget=wasm32-freestanding -Doptimize=ReleaseSmall )

# --- 4. Compile the flat-ABI shim and link the archive -> shim.wasm ----------------------
echo ">> compiling shim.c -> shim.wasm"
"$ZIG" cc \
  -target wasm32-freestanding -O2 \
  -I "$GHOSTTY/include" \
  -nostdlib -Wl,--no-entry \
  -Wl,--export=spike_new -Wl,--export=spike_write_n -Wl,--export=spike_format \
  -Wl,--export=spike_inbuf -Wl,--export=spike_outbuf \
  -Wl,--export=spike_cursor_x -Wl,--export=spike_cursor_y \
  -Wl,--export=spike_active_screen -Wl,--export=spike_cursor_visible \
  "$HERE/shim.c" "$GHOSTTY/zig-out/lib/libghostty-vt.a" \
  -o "$BUILD/shim.wasm"

echo ">> done: $BUILD/shim.wasm"
ls -la "$BUILD/shim.wasm"
