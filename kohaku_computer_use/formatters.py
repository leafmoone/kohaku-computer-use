from __future__ import annotations

import json
from collections import defaultdict, deque

from .models import ActPayload, ElementItem, Rect, RootItem, StateDetail


def quote(text: str, limit: int) -> str:
    text = ' '.join(text.split())
    return json.dumps(text if len(text) <= limit else text[:limit - 1] + '…', ensure_ascii=False)


def box(rect: Rect) -> str:
    return f'{rect["x"]},{rect["y"]},{rect["width"]},{rect["height"]}'


def roots_text(roots: list[RootItem]) -> str:
    lines = []
    for root in roots:
        kind = 'cdp' if root['kind'] == 'cdp' else 'window'
        flags = ','.join(flag for flag, field in (('fg', 'is_foreground'), ('min', 'is_minimized'),
                        ('elevated', 'is_elevated'), ('denied', 'denylisted')) if root[field]) or '-'
        location = f'url={quote(root["url"], 240)}' if kind == 'cdp' else f'rect={box(root["rect"])}'
        lines.append(f'{root["root_ref"]} [{kind}] exe={root["exe_name"]} title={quote(root["title"], 140)} {location} flags={flags}')
    return '\n'.join(lines) or 'No desktop roots found.'


def element_text(row: ElementItem) -> str:
    rect = row['rect']
    role = ' '.join(row['role'].split())[:60]
    fields = [row['ref'], f'[{role}]']
    if row['name']:
        fields.append(quote(row['name'], 100))
    if row['value_preview'] and row['value_preview'] != row['name']:
        fields.append(f'val={quote(row["value_preview"], 170)}')
    if row['capabilities']:
        actions = {'value': 'set_text', 'selection_item': 'select', 'expand_collapse': 'expand,collapse'}
        fields.append('[' + ','.join(actions.get(capability, capability) for capability in row['capabilities']) + ']')
    if rect['width'] and rect['height']:
        fields.append(f'({rect["x"]},{rect["y"]} {rect["width"]}x{rect["height"]})')
    if row['focused']:
        fields.append('focus')
    if not row['enabled']:
        fields.append('disabled')
    return ' '.join(fields)


def visible_elements(state: StateDetail) -> list[ElementItem]:
    viewport = state['rect']
    visible = []
    for row in state['elements']:
        rect = row['rect']
        if all(rect[size] > 0 and rect[axis] < viewport[axis] + viewport[size]
               and viewport[axis] < rect[axis] + rect[size] for axis, size in (('x', 'width'), ('y', 'height'))):
            visible.append(row)
    return visible or state['elements']


def state_header(state: StateDetail, full: bool) -> str:
    kind = 'cdp' if state.get('kind') == 'cdp' else 'window'
    extra = f' exe={state["exe_name"]} rect={box(state["rect"])} scale={state["dpi_scale"]:g}' if full else ''
    url = f' url={quote(state["url"], 240)}' if full and 'url' in state else ''
    valid = '' if state['valid_for_act'] else ' read_only'
    return f'[state={state["state_id"]} root={state["root_ref"]} kind={kind} title={quote(state["title"], 140)}{extra}{url}{valid}]'


def outline(state: StateDetail) -> str:
    return '\n'.join([state_header(state, True), *(element_text(row) for row in visible_elements(state))])


def element_key(row: ElementItem) -> tuple:
    if row['automation_id']:
        return row['role'], row['automation_id']
    rect = row['rect']
    return row['role'], row['depth'], rect['x'], rect['y']


def changes(before: StateDetail, after: StateDetail) -> list[str]:
    remaining = defaultdict(deque)
    for row in visible_elements(before):
        remaining[element_key(row)].append(row)
    lines = []
    for row in visible_elements(after):
        matches = remaining[element_key(row)]
        if not matches:
            lines.append('+ ' + element_text(row))
        elif any(matches[0][key] != row[key] for key in ('name', 'value_preview', 'enabled', 'focused')):
            lines.append('~ ' + element_text(row))
        if matches:
            matches.popleft()
    lines.extend(f'- [{row["role"]}] {quote(row["name"], 100)}' for rows in remaining.values() for row in rows if row['name'])
    return lines


def action_text(payload: ActPayload) -> str:
    post, before = payload['post_state'], payload.get('before_state')
    lines = [state_header(post, False) if post else '[no successor state; re-observe]']
    for i, row in enumerate(payload['results'], 1):
        target = row['ref'] or (','.join(map(str, row['point'])) if row['point'] else '')
        detail = row['reason'] or row['method']
        lines.append(f'{i}. {row["action"]}{" " + target if target else ""} -> {row["outcome"]}{f" ({detail})" if detail else ""}')
    if post:
        same = before and before['root_ref'] == post['root_ref'] and before.get('url') == post.get('url')
        delta = changes(before, post) if same else None
        elements = visible_elements(post)
        if delta is None or len(delta) > 35:
            lines.extend(element_text(row) for row in elements[:40])
        else:
            lines.extend(delta)
            key_rows = sorted((row for row in elements if row['enabled'] and row['capabilities']), key=lambda row: not row['focused'])[:15]
            if key_rows:
                lines.append('Current interactive refs:')
                lines.extend(element_text(row) for row in key_rows)
    return '\n'.join(lines)
