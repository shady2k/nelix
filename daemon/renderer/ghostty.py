import os

from wasmtime import Engine, Store, Module, Instance, Func, FuncType, ValType

from daemon.renderer.base import Frame

_WASM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shim.wasm")
# Compiled ONCE per process; instances are cheap to create from it.
_ENGINE = Engine()
_MODULE = Module.from_file(_ENGINE, _WASM_PATH)
_INBUF_CAP = 2 << 20    # must match shim.c INBUF (2 MiB)


class GhosttyRenderer:
    """Drive libghostty-vt (shim.wasm) via wasmtime: one terminal per renderer, fed
    incrementally. Implements daemon.renderer.base.Renderer."""

    def __init__(self, cols: int = 120, rows: int = 40):
        self._cols = cols
        self._rows = rows
        self._store = Store(_ENGINE)
        log = Func(self._store, FuncType([ValType.i32(), ValType.i32()], []), lambda a, b: None)
        self._inst = Instance(self._store, _MODULE, [log])
        self._ex = self._inst.exports(self._store)
        self._mem = self._ex["memory"]
        self._new(cols, rows)

    def _call(self, name, *a):
        return self._ex[name](self._store, *a)

    def _new(self, cols, rows):
        r = self._call("spike_new", cols, rows)
        if r != 0:
            raise RuntimeError(f"ghostty spike_new({cols},{rows}) failed: GhosttyResult={r}")

    def feed(self, data: bytes) -> None:
        if not data:
            return
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("feed() expects bytes")
        inbuf = self._call("spike_inbuf")
        mv = memoryview(bytes(data))
        for i in range(0, len(mv), _INBUF_CAP):     # chunk feeds larger than INBUF (offline replays)
            chunk = bytes(mv[i:i + _INBUF_CAP])
            self._mem.write(self._store, chunk, inbuf)
            self._call("spike_write_n", len(chunk))

    def snapshot(self) -> Frame:
        n = self._call("spike_format")
        if n < 0:
            raise RuntimeError(f"ghostty spike_format failed: {n}")
        ob = self._call("spike_outbuf")
        text = bytes(self._mem.read(self._store, ob, ob + n)).decode("utf-8", "replace")
        rows = [r.rstrip() for r in text.split("\n")]
        rows = (rows + [""] * self._rows)[:self._rows]      # exactly `rows` entries, blanks as ""
        cursor = (self._call("spike_cursor_x"), self._call("spike_cursor_y"))
        return Frame(rows=rows, cursor=cursor,
                     cursor_visible=bool(self._call("spike_cursor_visible")),
                     alt_screen=self._call("spike_active_screen") == 1)

    def render(self) -> str:
        return "\n".join(self.snapshot().rows)

    def resize(self, cols: int, rows: int) -> None:
        # Phase 1: there is no live resize trigger (sessions are fixed-size). The existing shim
        # has no resize export, so recreate the terminal at the new size (state reset). A native,
        # content-preserving resize is a Phase 2 shim extension.
        self._cols, self._rows = cols, rows
        self._new(cols, rows)

    def reset(self) -> None:
        self._new(self._cols, self._rows)

    def close(self) -> None:
        # Drop wasm references; wasmtime frees the Store/Instance/memory on GC.
        self._ex = None
        self._mem = None
        self._inst = None
        self._store = None
