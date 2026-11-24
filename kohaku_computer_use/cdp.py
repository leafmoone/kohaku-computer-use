from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from .models import (
    Action,
    ActionReport,
    WorkerElement,
    WorkerRoot,
    WorkerSnapshot,
    parse_chord,
)

log = logging.getLogger(__name__)


class CDPError(Exception):
    pass


def origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None:
            return None
        host = parsed.hostname.rstrip('.').casefold()
        if host in ('localhost', '::1', '0.0.0.0') or host.startswith('127.'):
            host = 'loopback'
        return parsed.scheme, host, parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        return None


def studio_origins() -> set[tuple[str, str, int]]:
    ports = [8001, os.environ.get('KT_HTTP_PORT'), os.environ.get('KT_SERVE_PORT')]
    for i, arg in enumerate(sys.argv):
        key, sep, value = arg.partition('=')
        value = value if sep else sys.argv[i + 1] if i + 1 < len(sys.argv) else ''
        if key == '--port':
            ports.append(value)
        elif key == '--state-path' and value and os.environ.get('KT_STARTUP_SURFACE') == 'desktop':
            try:
                state = json.loads(Path(value).read_text(encoding='utf-8'))
                if state.get('status') == 'ready':
                    ports.append(state.get('port'))
            except (OSError, ValueError) as exc:
                log.debug('Desktop startup state is not ready: %s', type(exc).__name__)
    return {(scheme, 'loopback', int(port)) for port in ports if str(port).isdigit() for scheme in ('http', 'https')}


def blocked(url: str, blocked_urls: set[tuple[str, str, int]]) -> bool:
    return origin(url) in blocked_urls


def page_root(row: dict, port: int, blocked_urls: set[tuple[str, str, int]]) -> WorkerRoot | None:
    url, title, target, ws = (row.get(key) for key in ('url', 'title', 'id', 'webSocketDebuggerUrl'))
    if row.get('type') != 'page' or not all(isinstance(value, str) for value in (url, title, target, ws)):
        return None
    if blocked(url, blocked_urls) or not target:
        return None
    try:
        endpoint = urlsplit(ws)
        if (endpoint.scheme != 'ws' or endpoint.hostname not in ('127.0.0.1', 'localhost', '::1')
                or endpoint.port != port or endpoint.username is not None or endpoint.path != f'/devtools/page/{target}'):
            return None
    except ValueError:
        return None
    return {'root_ref': '', 'kind': 'cdp', 'hwnd': 0, 'pid': 0, 'exe_path': f'cdp:{target}', 'exe_name': 'cdp-page',
            'class_name': 'CDP', 'title': title, 'url': url, 'ws_url': f'ws://127.0.0.1:{port}{endpoint.path}',
            'rect': {'x': 0, 'y': 0, 'width': 0, 'height': 0}, 'dpi_scale': 1., 'is_foreground': False,
            'is_minimized': False, 'is_elevated': False, 'denylisted': False}


def discover_cdp_pages(ports: Sequence[int], blocked_urls: set[tuple[str, str, int]]) -> tuple[list[WorkerRoot], list[int]]:
    if not ports:
        return [], []
    with httpx.Client(timeout=0.15, trust_env=False) as http:
        def probe(port: int) -> tuple[int, list[WorkerRoot]] | None:
            try:
                rows = http.get(f'http://127.0.0.1:{port}/json/list').raise_for_status().json()
            except (httpx.HTTPError, ValueError) as exc:
                log.debug('CDP discovery on %s: %s', port, exc)
                return None
            if not isinstance(rows, list):
                return None
            return port, [root for row in rows if isinstance(row, dict) and (root := page_root(row, port, blocked_urls))]

        with ThreadPoolExecutor(max_workers=len(ports)) as pool:
            listeners = [entry for entry in pool.map(probe, ports) if entry is not None]
    return [root for _, roots in listeners for root in roots], [port for port, _ in listeners]


def browser_path() -> str:
    if sys.platform == 'win32':
        import winreg

        for exe in ('msedge.exe', 'chrome.exe'):
            for access in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}', 0, access) as key:
                        path, _ = winreg.QueryValueEx(key, None)
                    if Path(path).is_file():
                        return path
                except FileNotFoundError:
                    continue
    raise CDPError('cdp_browser_not_found')


def matching_page(roots: Sequence[WorkerRoot], url: str) -> WorkerRoot | None:
    wanted = origin(url)
    return next((root for root in roots if origin(root['url']) == wanted and root['url'].startswith(url)), None)


def open_page(url: str, ports: Sequence[int], listening: Sequence[int], blocked_urls: set[tuple[str, str, int]],
              guard: Callable[[], None]) -> WorkerRoot:
    if not ports:
        raise CDPError('cdp_no_ports_configured')
    try:
        if listening:
            port = listening[0]
            with httpx.Client(timeout=1.0, trust_env=False) as http:
                guard()
                row = http.put(f'http://127.0.0.1:{port}/json/new?{quote(url, safe="")}').raise_for_status().json()
            if isinstance(row, dict) and (root := page_root(row, port, blocked_urls)):
                return root
            raise CDPError('cdp_new_page_unavailable')
        path = browser_path()
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            raise CDPError('cdp_localappdata_unavailable')
        profile = Path(local) / 'KohakuTerrarium' / 'cu-cdp-profile'
        port = ports[0]
        guard()
        subprocess.Popen([path, f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
                          f'--user-data-dir={profile}', url], creationflags=subprocess.DETACHED_PROCESS,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            guard()
            if root := matching_page(discover_cdp_pages((port,), blocked_urls)[0], url):
                return root
            time.sleep(0.08)
    except (OSError, httpx.HTTPError, ValueError) as exc:
        raise CDPError('cdp_open_failed') from exc
    raise CDPError('cdp_browser_start_timeout')


DOM = '''
const selector = 'a,button,input,textarea,select,[role],[contenteditable="true"],.cm-content,.monaco-editor,.jp-Cell,.jp-OutputArea-output,pre,code,h1,h2,h3,output';
const shorten = (s, n) => (s || '').trim().slice(0, n);
function describe(el) {
  const r = el.getBoundingClientRect();
  const rect = {x:Math.round(r.x), y:Math.round(r.y), width:Math.round(r.width), height:Math.round(r.height)};
  const style = getComputedStyle(el);
  if (!r.width || !r.height || style.visibility !== 'visible' || style.opacity === '0') return null;
  const edit = el.matches('textarea,input:not([type=button],[type=checkbox],[type=radio],[type=submit],[type=reset]),[contenteditable="true"]');
  const secret = el.matches('input[type=password]');
  const output = el.matches('.jp-OutputArea-output,pre,code,h1,h2,h3,output,[role=status],[role=alert]');
  const role = edit ? 'Edit' : el.matches('button,input[type=submit],input[type=button],input[type=reset],[role=button]') ? 'Button'
    : el.matches('a,[role=link]') ? 'Hyperlink' : el.matches('select') ? 'ComboBox' : output ? 'Text'
    : (el.getAttribute('role') || (el.matches('.jp-Cell') ? 'Cell' : el.tagName.toLowerCase()));
  const name = shorten(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.title ||
    (!edit && !output ? el.innerText || (el.matches('input') ? el.value : '') : ''), 120);
  const value_preview = secret ? '' : shorten(edit || output ? el.value ?? el.innerText : '', 240);
  const enabled = !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true';
  return {role,name,value_preview,rect,enabled,focused:document.activeElement===el,
    automation_id:el.id,depth:0,truncated:false,image_rect:null,
    capabilities:!enabled || output ? [] : edit ? ['set_text','click'] : ['click','invoke']};
}
function entries(limit) {
  const rows = [];
  for (const el of document.querySelectorAll(selector)) {
    const row = describe(el);
    if (row) rows.push([el,row]);
    if (rows.length >= limit) break;
  }
  return rows;
}
'''
OBSERVE = '''
const refs = new Map();
const elements = entries(arg.limit).map(([el,row],i) => {
  const ref = 'e' + (i+1);
  el.dataset.kcuRef = ref;
  refs.set(ref, el);
  return {...row,ref:'@'+ref};
});
const snapshots = globalThis.kcuSnapshots ??= new Map();
snapshots.set(arg.state, refs);
if (snapshots.size > 16) snapshots.delete(snapshots.keys().next().value);
return {title:document.title,url:location.href,width:innerWidth,height:innerHeight,elements};
'''
HIT = '''
const editables = '[contenteditable="true"],textarea,input';
const node = arg.ref ? globalThis.kcuSnapshots?.get(arg.state)?.get(arg.ref) : null;
const initial = node?.isConnected && describe(node);
const focus = node && (arg.edit && !node.matches(editables) ? node.querySelector(editables) : node);
if (arg.ref && (!initial || !initial.enabled)) return {error:'cdp_element_unavailable'};
if (arg.prepare && node) {
  if (arg.focus) focus?.focus({preventScroll:true});
  else node.scrollIntoView({block:'nearest',inline:'nearest'});
}
if (arg.focus && arg.prepare && node && (!focus || (document.activeElement !== focus && !focus.contains(document.activeElement))))
  return {error:'cdp_element_not_focused'};
const r = node?.getBoundingClientRect();
const point = arg.point || (r ? [Math.round(r.x+r.width/2), Math.round(r.y+r.height/2)] : [innerWidth>>1,innerHeight>>1]);
const hit = arg.mouse ? document.elementFromPoint(...point) : (focus || document.activeElement);
if (hit?.closest('iframe,frame,object,embed') || (node && node.matches('iframe,frame,object,embed')))
  return {error:'cdp_embedded_target_unsupported'};
if (arg.mouse && (!hit || (node && !node.contains(hit)))) return {error:'cdp_point_not_on_target'};
const targets = [];
for (let el=hit; el; el=el.parentElement) if (el.matches(selector)) {
  const row = describe(el);
  if (row) targets.push([row.role,row.name,row.enabled]);
}
return {point, targets};
'''
KEYS = {'ENTER':'Enter', 'ESCAPE':'Escape', 'ESC':'Escape', 'TAB':'Tab', 'BACKSPACE':'Backspace', 'SPACE':' ',
        'UP':'ArrowUp', 'DOWN':'ArrowDown', 'LEFT':'ArrowLeft', 'RIGHT':'ArrowRight', 'PAGEUP':'PageUp',
        'PAGEDOWN':'PageDown', 'HOME':'Home', 'END':'End', 'INSERT':'Insert', 'DELETE':'Delete'}


@dataclass(slots=True)
class Connection:
    socket: ClientConnection
    guard: Callable[[], None]
    serial: int = 0
    input_guard: Callable[[], None] | None = None
    context_id: int | None = None

    def command(self, method: str, **params) -> dict:
        if self.input_guard and method.startswith('Input.'):
            self.input_guard()
        self.guard()
        self.serial += 1
        ident = self.serial
        self.socket.send(json.dumps({'id': ident, 'method': method, 'params': params}))
        deadline = time.monotonic() + 2.0
        while True:
            message = json.loads(self.socket.recv(timeout=max(0., deadline - time.monotonic())))
            if message.get('id') == ident:
                if 'error' in message:
                    raise CDPError(f'cdp_{method}_failed')
                return message['result']

    def evaluate(self, body: str, args: dict) -> object:
        if self.context_id is None:
            frame = self.command('Page.getFrameTree')['frameTree']['frame']
            self.context_id = self.command('Page.createIsolatedWorld', frameId=frame['id'], worldName='kohaku-computer-use')['executionContextId']
        data = self.command('Runtime.evaluate', expression=f'(arg => {{ {DOM}{body} }})({json.dumps(args)})',
                            contextId=self.context_id, returnByValue=True)
        if 'exceptionDetails' in data:
            raise CDPError('cdp_evaluation_failed')
        return data['result']['value']

    def key(self, chord: str):
        bits, _, vk = parse_chord(chord, False)
        modifiers = (bits & 3) | (8 if bits & 4 else 0) | (4 if bits & 8 else 0)
        part = chord.split('+')[-1].strip().upper()
        key = KEYS.get(part, part.lower() if len(part) == 1 and not modifiers & 8 else part)
        code = 'Space' if key == ' ' else (('Digit' if part.isdigit() else 'Key') + part if len(part) == 1 else key)
        spec = {'key': key, 'code': code, 'windowsVirtualKeyCode': vk, 'modifiers': modifiers}
        self.command('Input.dispatchKeyEvent', type='rawKeyDown', **spec)
        if len(key) == 1 and not modifiers & 7:
            self.command('Input.dispatchKeyEvent', type='char', text=key, **spec)
        self.command('Input.dispatchKeyEvent', type='keyUp', **spec)


def socket_for(root: WorkerRoot):
    return connect(root['ws_url'], open_timeout=1, close_timeout=0.2, max_size=16 * 1024 * 1024, proxy=None)


def location(connection: Connection, root: WorkerRoot, check: Callable[[WorkerRoot], None]) -> WorkerRoot:
    # Read browser-owned navigation state, not page-controlled DOM attributes.
    frame = connection.command('Page.getFrameTree')['frameTree']['frame']
    current = {**root, 'url': frame['url'] + frame.get('urlFragment', ''), 'document_id': frame['loaderId']}
    check(current)
    return current


def observe_cdp(root: WorkerRoot, include_image: bool, limit: int, guard: Callable[[], None],
                check: Callable[[WorkerRoot], None]) -> WorkerSnapshot:
    try:
        guard()
        with socket_for(root) as sock:
            conn = Connection(sock, guard)
            marker = uuid4().int
            deadline = time.monotonic() + 1.0
            while True:
                before = location(conn, root, check)
                data = conn.evaluate(OBSERVE, {'limit': limit, 'state': str(marker)})
                current = location(conn, root, check)
                if (data['url'].split('#', 1)[0] == current['url'].split('#', 1)[0]
                        and before['document_id'] == current['document_id']):
                    break
                if time.monotonic() >= deadline:
                    raise CDPError('cdp_page_navigated')
                conn.context_id = None
                time.sleep(0.05)
            current.update(title=data['title'], rect={'x': 0, 'y': 0, 'width': data['width'], 'height': data['height']})
            check(current)
            elements = [{**row, 'runtime_id': (marker,)} for row in data['elements']]
            png = iw = ih = None
            if include_image:
                png = conn.command('Page.captureScreenshot', format='png')['data']
                header = base64.b64decode(png[:32], validate=True)
                iw, ih = int.from_bytes(header[16:20], 'big'), int.from_bytes(header[20:24], 'big')
                for element in elements:
                    r = element['rect']
                    element['image_rect'] = {key: round(value * (iw / data['width'] if key in ('x', 'width') else ih / data['height']))
                                             for key, value in r.items()}
            if include_image and location(conn, current, check)['document_id'] != current['document_id']:
                raise CDPError('cdp_page_navigated')
            return {'root': current, 'elements': elements, 'png': png, 'image_width': iw, 'image_height': ih,
                    'capture_method': 'CDP' if png else None}
    except (WebSocketException, OSError, ValueError, KeyError, TypeError) as exc:
        raise CDPError('cdp_observe_failed') from exc


def dispatch(conn: Connection, action: Action, point: tuple[int, int]):
    name = action['action']
    x, y = point
    if name in ('click', 'invoke', 'double_click', 'right_click'):
        button = 'right' if name == 'right_click' else 'left'
        for i in range(1, (2 if name == 'double_click' else 1) + 1):
            for event in ('mousePressed', 'mouseReleased'):
                conn.command('Input.dispatchMouseEvent', type=event, x=x, y=y, button=button, clickCount=i)
    elif name in ('set_text', 'type_text'):
        if action.get('clear_first', name == 'set_text'):
            conn.key('Ctrl+A')
            conn.key('Backspace')
        conn.command('Input.insertText', text=action['text'])
    elif name == 'key':
        conn.key(action['key'])
    else:
        direction = action['direction']
        delta = 120 * action.get('amount', 1)
        dx, dy = {'up': (0, -delta), 'down': (0, delta), 'left': (-delta, 0), 'right': (delta, 0)}[direction]
        conn.command('Input.dispatchMouseEvent', type='mouseWheel', x=x, y=y, deltaX=dx, deltaY=dy)


def expectation(conn: Connection, expect: dict) -> str:
    deadline = time.monotonic() + expect.get('timeout_ms', 1500) / 1000
    while True:
        found = conn.evaluate('return entries(512).some(([,row]) => (!arg.role || row.role.toLowerCase() === arg.role) '
                              '&& (!arg.text || (row.name+" "+row.value_preview).toLowerCase().includes(arg.text)));',
                              {key: value.lower() for key, value in expect.items() if key in ('role', 'text')})
        if found != expect.get('gone', False):
            return 'worked'
        if time.monotonic() >= deadline:
            return 'didnt'
        time.sleep(0.04)


def act_cdp(root: WorkerRoot, elements: Sequence[WorkerElement], actions: Sequence[Action], guard: Callable[[], None],
            check: Callable[[WorkerRoot], None], authorize: Callable[[list], None]) -> list[ActionReport]:
    results: list[ActionReport] = []
    try:
        guard()
        with socket_for(root) as sock:
            conn = Connection(sock, guard)

            def verify_document():
                live = location(conn, root, check)
                if live['url'] != root['url'] or live['document_id'] != root['document_id']:
                    raise CDPError('cdp_page_navigated')

            conn.input_guard = verify_document
            for action in actions:
                name, ref = action['action'], action.get('ref')
                report = {'action': name, 'ref': ref, 'method': None, 'outcome': 'blocked', 'reason': None, 'point': None}
                dispatched = False
                try:
                    verify_document()
                    if name not in ('click', 'invoke', 'double_click', 'right_click', 'set_text', 'type_text', 'key', 'scroll'):
                        raise CDPError('cdp_action_unsupported')
                    keyboard = name in ('set_text', 'type_text', 'key')
                    marker = str(elements[0]['runtime_id'][0]) if elements else ''
                    args = {'ref': ref[1:] if ref else None, 'state': marker, 'mouse': not keyboard,
                            'focus': keyboard, 'edit': name in ('set_text', 'type_text'), 'point': [action['x'], action['y']] if 'x' in action else None, 'prepare': False}
                    hit = conn.evaluate(HIT, args)
                    if hit.get('error') and (not ref or hit['error'] != 'cdp_point_not_on_target'):
                        raise CDPError(hit['error'])
                    if not hit.get('error'):
                        authorize(hit['targets'])
                    dispatched = bool(ref)
                    hit = conn.evaluate(HIT, {**args, 'prepare': True})
                    if 'error' in hit:
                        raise CDPError(hit['error'])
                    authorize(hit['targets'])
                    point = tuple(hit['point'])
                    dispatched = True
                    dispatch(conn, action, point)
                    report.update(method='CDP', point=None if keyboard else point)
                    deadline = time.monotonic() + action.get('wait_ms', 0) / 1000
                    while (left := deadline - time.monotonic()) > 0:
                        guard()
                        time.sleep(min(0.05, left))
                    if 'expect' in action:
                        verify_document()
                        report['outcome'] = expectation(conn, action['expect'])
                    else:
                        report['outcome'] = 'unknown'
                except (CDPError, WebSocketException, OSError, ValueError, KeyError, TypeError) as exc:
                    report.update(outcome='unknown' if dispatched else 'blocked', reason=str(exc) if isinstance(exc, CDPError) else 'cdp_dispatch_failed')
                    results.append(report)
                    break
                results.append(report)
                if report['outcome'] == 'didnt':
                    break
        return results
    except (WebSocketException, OSError, ValueError) as exc:
        raise CDPError('cdp_connection_failed') from exc
