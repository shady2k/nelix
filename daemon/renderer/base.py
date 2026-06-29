from dataclasses import dataclass
from typing import Protocol


@dataclass
class Frame:
    """A rendered terminal snapshot. `rows` is exactly the viewport height, each row
    right-trimmed, blank rows preserved as "" (a rectangular grid). `dirty`/`wrap` are
    reserved for a Phase 2 engine extension (per-row changed / soft-wrap continuation)."""
    rows: "list[str]"
    cursor: "tuple[int, int]"          # (x, y), 0-indexed
    cursor_visible: bool
    alt_screen: bool                   # True => alternate screen active
    dirty: "list[bool] | None" = None
    wrap: "list[bool] | None" = None


class Renderer(Protocol):
    def feed(self, data: bytes) -> None: ...        # incremental; parser state carries across calls
    def snapshot(self) -> Frame: ...                # current active screen
    def render(self) -> str: ...                    # "\n".join(snapshot().rows)
    def resize(self, cols: int, rows: int) -> None: ...
    def reset(self) -> None: ...                    # fresh terminal of the same size
    def close(self) -> None: ...


def make_renderer(cols: int = 120, rows: int = 40) -> Renderer:
    """The single construction site for the VT engine. Imported lazily so `Frame`/`Renderer`
    can be used without loading wasmtime."""
    from daemon.renderer.ghostty import GhosttyRenderer
    return GhosttyRenderer(cols=cols, rows=rows)
