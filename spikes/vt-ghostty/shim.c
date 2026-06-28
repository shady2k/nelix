/* Flat-ABI shim over libghostty-vt for the nelix faithful-VT-render spike.
 *
 * libghostty-vt's terminal/formatter API uses nested, sized structs
 * (GHOSTTY_INIT_SIZED). Packing those by hand from Python over the wasm
 * boundary is error-prone, so this shim lets the C compiler own the ABI and
 * exposes a dead-simple scalar-only surface that Python (via wasmtime) drives:
 *
 *   spike_new(cols, rows)      -> GhosttyResult (0 = success); (re)creates the terminal
 *   spike_inbuf()              -> ptr; Python writes raw PTY bytes here (<= 2 MiB)
 *   spike_write_n(n)           -> feed n bytes of INBUF through the VT parser
 *   spike_format()             -> byte length of the plain-text screen written to OUTBUF (<0 = err)
 *   spike_outbuf()             -> ptr; Python reads `len` bytes of UTF-8 plain text
 *   spike_cursor_x/y()         -> cursor position (0-indexed)
 *   spike_cursor_visible()     -> 1/0
 *   spike_active_screen()      -> 0 = primary, 1 = alternate
 *
 * This is the prototype of the eventual nelix Renderer adapter (create/feed/snapshot).
 * The lib imports env.log(ptr,len); the host (Python/wasmtime) supplies it as a no-op.
 */
#include <stddef.h>
#include <stdint.h>
#include <ghostty/vt.h>

#define EXPORT __attribute__((used, visibility("default")))

static GhosttyTerminal T = NULL;
static uint8_t INBUF[2u << 20];  /* raw PTY bytes written here by Python (max 2 MiB) */
static uint8_t OUTBUF[1u << 20]; /* formatted plain-text screen lands here */

EXPORT uint8_t *spike_inbuf(void) { return INBUF; }
EXPORT uint8_t *spike_outbuf(void) { return OUTBUF; }

EXPORT int spike_new(uint16_t cols, uint16_t rows) {
  if (T) { ghostty_terminal_free(T); T = NULL; }
  /* GhosttyTerminalOptions is NOT a sized struct (no `size` field; see terminal.h),
     unlike the formatter options below — plain init matches the official c-vt-formatter example. */
  GhosttyTerminalOptions opts = { .cols = cols, .rows = rows, .max_scrollback = 1000 };
  return (int)ghostty_terminal_new(NULL, &T, opts);
}

EXPORT void spike_write_n(size_t n) {
  if (!T) return;
  if (n > sizeof(INBUF)) n = sizeof(INBUF); /* never over-read past the host-filled buffer */
  ghostty_terminal_vt_write(T, INBUF, n);
}

EXPORT long spike_format(void) {
  if (!T) return -3;
  GhosttyFormatterTerminalOptions fo = GHOSTTY_INIT_SIZED(GhosttyFormatterTerminalOptions);
  fo.emit = GHOSTTY_FORMATTER_FORMAT_PLAIN;
  fo.trim = true;
  GhosttyFormatter f;
  if (ghostty_formatter_terminal_new(NULL, &f, T, fo) != GHOSTTY_SUCCESS) return -1;
  size_t written = 0;
  /* format_buf returns GHOSTTY_OUT_OF_SPACE (not partial success) if OUTBUF is too small,
     so a too-large screen surfaces as -2 here — never a silent truncation. */
  GhosttyResult r = ghostty_formatter_format_buf(f, OUTBUF, sizeof(OUTBUF), &written);
  ghostty_formatter_free(f);
  if (r != GHOSTTY_SUCCESS) return -2;
  return (long)written;
}

EXPORT int spike_cursor_x(void) { uint16_t v = 0; ghostty_terminal_get(T, GHOSTTY_TERMINAL_DATA_CURSOR_X, &v); return v; }
EXPORT int spike_cursor_y(void) { uint16_t v = 0; ghostty_terminal_get(T, GHOSTTY_TERMINAL_DATA_CURSOR_Y, &v); return v; }
EXPORT int spike_active_screen(void) { GhosttyTerminalScreen v = GHOSTTY_TERMINAL_SCREEN_PRIMARY; ghostty_terminal_get(T, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &v); return (int)v; }
EXPORT int spike_cursor_visible(void) { bool v = false; ghostty_terminal_get(T, GHOSTTY_TERMINAL_DATA_CURSOR_VISIBLE, &v); return v ? 1 : 0; }
