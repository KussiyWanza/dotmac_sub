"""Read OLT hardware topology out of a Huawei running configuration.

The estate holds 5 shelves, 3 cards and 23 card ports for seven OLTs, so
``PonPort.olt_card_port_id`` is set on 23 of 502 rows and structural identity is
underivable for the rest. The topology was never recorded; it exists only on the
devices. This module reads it back out of a running config, which the fleet
already archives, so the common case needs no device I/O at all.

Two grammar facts drive the parse, both verified against production configs:

* ``interface gpon <frame>/<slot>`` is the authority for frame and slot.
  ``board add <frame>/<slot> <type>`` is *not* sufficient on its own -- the BOI
  config declares ``interface gpon 0/1`` with no matching ``board add`` line --
  so boards are collected for their type but frames and slots come from the
  interface blocks.
* ``ont add <port> <ont_id> sn-auth "<serial>"`` carries **no** F/S/P. Frame and
  slot come from the enclosing interface block; the line supplies the port and
  ONT id. Parsing these lines outside their block context yields a port number
  attached to nothing, which is the same class of mistake as ``pon-{port}``.

Ports come from ``port <n> ont-auto-find ...``, which every archived config
lists for the board's whole complement regardless of whether an ONT sits on the
port. Ports mentioned only by an ``ont add`` line are folded in as well, so an
occupied port is never missed if a firmware omits its auto-find line. Reading
ports from ``ont add`` alone would have made the result a lower bound covering
only occupied ports, which is what an earlier cut of this module did.

Pure and offline by construction: text in, typed records out, no database and no
network. That keeps the grammar testable against captured fixtures and keeps the
import decision separate from the reading of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "OltTopologyReading",
    "ParsedBoard",
    "ParsedOnt",
    "ParsedPonInterface",
    "parse_running_config",
]

#: ``board add 0/1 H803GPFD`` — frame/slot and the board type.
_BOARD_ADD = re.compile(r"^\s*board\s+add\s+(\d+)/(\d+)\s+(\S+)\s*$", re.IGNORECASE)

#: ``interface gpon 0/1`` — opens a block whose contents inherit frame/slot.
_INTERFACE_GPON = re.compile(r"^\s*interface\s+gpon\s+(\d+)/(\d+)\s*$", re.IGNORECASE)

#: Any other ``interface ...`` line closes the current gpon block.
_INTERFACE_ANY = re.compile(r"^\s*interface\s+\S+", re.IGNORECASE)

#: ``ont add 0 13 sn-auth "48575443A31C862D" omci ont-lineprofile-id 40``
#: The serial may be quoted or bare depending on firmware.
_ONT_ADD = re.compile(
    r"^\s*ont\s+add\s+(\d+)\s+(\d+)\s+sn-auth\s+\"?([0-9A-Za-z]+)\"?",
    re.IGNORECASE,
)

#: ``port 13 ont-auto-find enable`` — the board's full port complement, listed
#: whether or not any ONT sits on the port. This is the authoritative port
#: enumeration; ``ont add`` only reveals ports that happen to be occupied.
_PORT_LINE = re.compile(r"^\s*port\s+(\d+)\s+ont-auto-find\b", re.IGNORECASE)

#: ``quit`` leaves the interface block.
_QUIT = re.compile(r"^\s*quit\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParsedBoard:
    """A ``board add`` declaration: which slot holds which board type."""

    frame: int
    slot: int
    board_type: str


@dataclass(frozen=True, slots=True)
class ParsedOnt:
    """One ONT declared inside an ``interface gpon`` block."""

    frame: int
    slot: int
    port: int
    ont_id: int
    serial: str


@dataclass(frozen=True, slots=True)
class ParsedPonInterface:
    """One ``interface gpon frame/slot`` block and the ports seen inside it."""

    frame: int
    slot: int
    ports: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class OltTopologyReading:
    """Everything one running config states about PON hardware.

    ``interfaces`` is the authority for which frames and slots exist.
    ``boards`` adds the board type where the config declares it. ``onts`` is the
    serial-to-position map, which is what lets a PON row whose name is unusable
    be identified by what is attached to it.
    """

    boards: tuple[ParsedBoard, ...] = ()
    interfaces: tuple[ParsedPonInterface, ...] = ()
    onts: tuple[ParsedOnt, ...] = ()

    def board_type_for(self, frame: int, slot: int) -> str | None:
        for board in self.boards:
            if board.frame == frame and board.slot == slot:
                return board.board_type
        return None


@dataclass
class _OpenInterface:
    """Mutable accumulator for the block currently being read."""

    frame: int
    slot: int
    ports: set[int] = field(default_factory=set)


def parse_running_config(text: str) -> OltTopologyReading:
    """Read PON topology from a Huawei running configuration.

    Unrecognised lines are ignored rather than rejected: a running config
    carries far more than topology, and failing on the first unfamiliar line
    would make the parse hostage to firmware differences it does not care about.
    """
    boards: list[ParsedBoard] = []
    interfaces: list[ParsedPonInterface] = []
    onts: list[ParsedOnt] = []
    current: _OpenInterface | None = None

    def _close() -> None:
        nonlocal current
        if current is not None:
            interfaces.append(
                ParsedPonInterface(
                    frame=current.frame,
                    slot=current.slot,
                    ports=tuple(sorted(current.ports)),
                )
            )
            current = None

    for raw in (text or "").splitlines():
        board = _BOARD_ADD.match(raw)
        if board is not None:
            boards.append(
                ParsedBoard(
                    frame=int(board[1]), slot=int(board[2]), board_type=board[3]
                )
            )
            continue

        opened = _INTERFACE_GPON.match(raw)
        if opened is not None:
            _close()
            current = _OpenInterface(frame=int(opened[1]), slot=int(opened[2]))
            continue

        if current is None:
            continue

        if _QUIT.match(raw) or _INTERFACE_ANY.match(raw):
            _close()
            continue

        declared = _PORT_LINE.match(raw)
        if declared is not None:
            current.ports.add(int(declared[1]))
            continue

        ont = _ONT_ADD.match(raw)
        if ont is not None:
            port = int(ont[1])
            current.ports.add(port)
            onts.append(
                ParsedOnt(
                    frame=current.frame,
                    slot=current.slot,
                    port=port,
                    ont_id=int(ont[2]),
                    serial=ont[3].upper(),
                )
            )

    _close()
    return OltTopologyReading(
        boards=tuple(boards),
        interfaces=tuple(interfaces),
        onts=tuple(onts),
    )
