"""Wire and in-process data for the desktop broker and worker."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

Mode = Literal['locked', 'read_only', 'confirm', 'armed']
Outcome = Literal['worked', 'didnt', 'unknown', 'blocked', 'info']
Event = Literal['observe', 'act', 'blocked', 'mode_change', 'panic', 'panic_reset']
ActionName = Literal[
    'invoke', 'set_text', 'toggle', 'select', 'expand', 'collapse', 'scroll',
    'click', 'double_click', 'right_click', 'type_text', 'key',
]


class Rect(TypedDict):
    x: int
    y: int
    width: int
    height: int


class Identity(TypedDict):
    hwnd: int
    pid: int
    exe_path: str


class RootItem(TypedDict):
    root_ref: str
    hwnd: int
    pid: int
    exe_name: str
    title: str
    class_name: str
    kind: Literal['window', 'dialog', 'menu', 'popover']
    rect: Rect
    dpi_scale: float
    is_foreground: bool
    is_minimized: bool
    is_elevated: bool
    denylisted: bool


class WorkerRoot(RootItem):
    exe_path: str


class ElementItem(TypedDict):
    ref: str
    role: str
    name: str
    automation_id: str
    value_preview: str
    rect: Rect
    image_rect: Rect | None
    enabled: bool
    focused: bool
    capabilities: list[str]
    depth: int
    truncated: bool


class WorkerElement(ElementItem):
    runtime_id: tuple[int, ...]


class ActionPoint(TypedDict):
    x: int
    y: int
    image_x: int | None
    image_y: int | None
    action: ActionName
    ref: str | None
    outcome: Outcome


class StateSummary(TypedDict):
    state_id: str
    epoch: int
    created_at: float
    valid_for_act: bool
    root_ref: str
    hwnd: int
    pid: int
    exe_name: str
    title: str
    dpi_scale: float
    rect: Rect
    element_count: int
    has_image: bool
    image_width: int | None
    image_height: int | None
    capture_method: str | None
    last_action_point: ActionPoint | None


class StateDetail(StateSummary):
    elements: list[ElementItem]


class LeasePayload(TypedDict):
    session_id: str
    creature_id: str
    agent_name: str
    updated_at: float
    target_hwnd: int | None
    target_title: str | None


class Stats(TypedDict):
    observations: int
    actions: int
    blocked: int


class StatusPayload(TypedDict):
    supported: bool
    platform: str
    mode: Mode
    tripped: bool
    trip_reason: str | None
    hotkey: str
    hotkey_registered: bool
    allow_foreground_input: bool
    idle_timeout_s: int
    idle_remaining_s: float | None
    denylist_patterns: list[str]
    allowlist_patterns: list[str]
    active_lease: LeasePayload | None
    latest_state_id: str | None
    worker_alive: bool
    stats: Stats


class AuditEntry(TypedDict):
    id: str
    timestamp: float
    actor: str
    event: Event
    target_title: str | None
    summary: str
    outcome: Outcome


class StateIndex(TypedDict):
    states: list[StateSummary]
    audit: list[AuditEntry]


class Expect(TypedDict):
    text: NotRequired[str]
    role: NotRequired[str]
    gone: NotRequired[bool]
    timeout_ms: NotRequired[int]


class Action(TypedDict):
    action: ActionName
    ref: NotRequired[str]
    x: NotRequired[int]
    y: NotRequired[int]
    text: NotRequired[str]
    key: NotRequired[str]
    clear_first: NotRequired[bool]
    direction: NotRequired[Literal['up', 'down', 'left', 'right']]
    amount: NotRequired[int]
    wait_ms: NotRequired[int]
    expect: NotRequired[Expect]


class ActionReport(TypedDict):
    action: ActionName
    ref: str | None
    outcome: Outcome
    method: str | None
    reason: str | None
    point: tuple[int, int] | None


class ActPayload(TypedDict):
    results: list[ActionReport]
    post_state: StateDetail | None


class WorkerRequest(TypedDict):
    id: int
    op: Literal['list_roots', 'identify', 'observe', 'act']
    identity: NotRequired[Identity]
    hwnd: NotRequired[int]
    root_ref: NotRequired[str]
    include_image: NotRequired[bool]
    restore: NotRequired[bool]
    max_elements: NotRequired[int]
    actions: NotRequired[list[Action]]
    elements: NotRequired[list[WorkerElement]]
    allow_foreground_input: NotRequired[bool]
    denylist_patterns: NotRequired[list[str]]
    foreground_rect: NotRequired[Rect]
    foreground_dpi: NotRequired[float]
    allowlist_patterns: NotRequired[list[str]]
    approved_title: NotRequired[str | None]


class WorkerSnapshot(TypedDict):
    root: WorkerRoot
    elements: list[WorkerElement]
    png: str | None
    image_width: int | None
    image_height: int | None
    capture_method: str | None


class WorkerReply(TypedDict):
    id: int
    result: NotRequired[list[WorkerRoot] | WorkerRoot | WorkerSnapshot | list[ActionReport]]
    error: NotRequired[str]


class HotkeyRegistered(TypedDict):
    event: Literal['hotkey_registration']
    hotkey: str
    registered: bool
    error: NotRequired[int]


class HotkeyPanic(TypedDict):
    event: Literal['hotkey_panic']
    hotkey: str


@dataclass(frozen=True, slots=True)
class Policy:
    mode: Mode
    allow_foreground_input: bool
    idle_timeout_s: int
    hotkey: str
    denylist_patterns: tuple[str, ...]
    allowlist_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Actor:
    session_id: str
    creature_id: str
    agent_name: str


@dataclass(frozen=True, slots=True)
class Lease:
    actor: Actor
    state_id: str | None
    updated_at: float
    target_hwnd: int | None
    target_title: str | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    state: StateDetail
    root: WorkerRoot
    elements: tuple[WorkerElement, ...]
    png: bytes | None
    max_elements: int


@dataclass(frozen=True, slots=True)
class Approval:
    actor: Actor
    state_id: str
    epoch: int
    actions: str
    title: str
    deadline: float


DEFAULT_DENYLIST = (
    '1Password', 'Bitwarden', 'KeePass', 'CredentialUIBroker',
    'User Account Control', 'Windows Security', 'Taskmgr',
)
DEFAULT_POLICY = Policy('locked', True, 0, 'Ctrl+Alt+F12', DEFAULT_DENYLIST, ())
DESTRUCTIVE = re.compile(
    r'\b(delete|remove|erase|discard|wipe|destroy|uninstall|format|reset|terminate|shut down|'
    r'restart|sign out|log off|close|empty recycle bin)\b|删除|刪除|卸载|解除安裝|清除|清空|關閉|关闭|关机|關機|重置',
    re.IGNORECASE,
)
MODIFIERS = {'CTRL': (2, 0x11), 'ALT': (1, 0x12), 'SHIFT': (4, 0x10), 'WIN': (8, 0x5B)}
KEYS = {
    **{f'F{i}': 0x6F + i for i in range(1, 13)},
    **{chr(i): i for i in range(0x30, 0x3A)},
    **{chr(i): i for i in range(0x41, 0x5B)},
    'BACKSPACE': 0x08, 'TAB': 0x09, 'ENTER': 0x0D, 'ESC': 0x1B, 'ESCAPE': 0x1B,
    'SPACE': 0x20, 'PAGEUP': 0x21, 'PAGEDOWN': 0x22, 'END': 0x23, 'HOME': 0x24,
    'LEFT': 0x25, 'UP': 0x26, 'RIGHT': 0x27, 'DOWN': 0x28, 'INSERT': 0x2D, 'DELETE': 0x2E,
}


def parse_chord(chord: str, hotkey: bool) -> tuple[int, tuple[int, ...], int]:
    parts = [part.strip().upper() for part in chord.split('+')]
    mods, key = parts[:-1], parts[-1]
    if len(mods) != len(set(mods)) or any(mod not in MODIFIERS for mod in mods) or key not in KEYS:
        raise ValueError('Invalid key chord')
    if hotkey and (not mods or not (key.isalpha() and len(key) == 1 or key.startswith('F'))):
        raise ValueError('Use Ctrl, Alt, Shift or Win with F1–F12 or A–Z')
    return sum(MODIFIERS[mod][0] for mod in mods), tuple(MODIFIERS[mod][1] for mod in mods), KEYS[key]


def destructive_control(role: str, name: str) -> bool:
    return role in ('Button', 'MenuItem', 'SplitButton', 'Hyperlink') and bool(DESTRUCTIVE.search(name))


def risky_chord(chord: str) -> bool:
    mask, _, key = parse_chord(chord, False)
    return bool(mask & 8 or (mask & 1 and key == 0x73))


def identity(root: WorkerRoot) -> Identity:
    return {'hwnd': root['hwnd'], 'pid': root['pid'], 'exe_path': root['exe_path']}


def matches_patterns(root: WorkerRoot, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(
        re.search(pattern, root[key], re.IGNORECASE)
        for pattern in patterns
        for key in ('title', 'exe_name', 'exe_path', 'class_name')
    )
