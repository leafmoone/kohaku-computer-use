"""Windows child process; only the windowless MTA thread touches UI Automation."""
import ctypes as c

if hasattr(c, 'WinDLL'):
    _early_user = c.WinDLL('user32', use_last_error=True)
    _dpi = _early_user.SetProcessDpiAwarenessContext
    _dpi.argtypes = [c.c_void_p]
    _dpi.restype = c.c_int
    _per_monitor_v2 = c.c_void_p(-4)
    if not _dpi(_per_monitor_v2):
        _dpi_error = c.get_last_error()
        _get_context = _early_user.GetThreadDpiAwarenessContext
        _get_context.argtypes, _get_context.restype = [], c.c_void_p
        _compare_contexts = _early_user.AreDpiAwarenessContextsEqual
        _compare_contexts.argtypes, _compare_contexts.restype = [c.c_void_p, c.c_void_p], c.c_int
        if not _compare_contexts(_get_context(), _per_monitor_v2):
            raise c.WinError(_dpi_error)

import base64
import io
import json
import ntpath
import sys
import threading
import time
from collections import deque
from ctypes import wintypes as w
from queue import Queue

from .models import (
    destructive_control,
    identity,
    matches_patterns,
    parse_chord,
    risky_chord,
)


class WorkerError(Exception):
    """A checked failure before or during a worker operation."""


class MouseInput(c.Structure):
    _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('data', w.DWORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('extra', c.c_size_t)]


class KeyboardInput(c.Structure):
    _fields_ = [('vk', w.WORD), ('scan', w.WORD), ('flags', w.DWORD), ('time', w.DWORD),
                ('extra', c.c_size_t)]


class InputValue(c.Union):
    _fields_ = [('mouse', MouseInput), ('keyboard', KeyboardInput)]


class Input(c.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('kind', w.DWORD), ('value', InputValue)]


class BitmapInfo(c.Structure):
    _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG), ('planes', w.WORD),
                ('bits', w.WORD), ('compression', w.DWORD), ('size_image', w.DWORD),
                ('x_ppm', w.LONG), ('y_ppm', w.LONG), ('colors', w.DWORD), ('important', w.DWORD),
                ('palette', w.DWORD)]


WNDENUMPROC = getattr(c, 'WINFUNCTYPE', c.CFUNCTYPE)(w.BOOL, w.HWND, w.LPARAM)
H = w.HANDLE
P = c.POINTER
UINT = w.UINT
D = w.DWORD
B = w.BOOL
I = c.c_int
HWND = w.HWND


def bind(dll, name, result, *args):
    func = getattr(dll, name)
    func.restype, func.argtypes = result, args
    return func


class Native:
    def __init__(self):
        u = c.WinDLL('user32', use_last_error=True)
        k = c.WinDLL('kernel32', use_last_error=True)
        a = c.WinDLL('advapi32', use_last_error=True)
        g = c.WinDLL('gdi32', use_last_error=True)
        d = c.WinDLL('dwmapi', use_last_error=True)
        self.enum = bind(u, 'EnumWindows', B, WNDENUMPROC, w.LPARAM)
        self.visible = bind(u, 'IsWindowVisible', B, HWND)
        self.valid = bind(u, 'IsWindow', B, HWND)
        self.minimized = bind(u, 'IsIconic', B, HWND)
        self.rect = bind(u, 'GetWindowRect', B, HWND, P(w.RECT))
        self.dpi = bind(u, 'GetDpiForWindow', UINT, HWND)
        self.title = bind(u, 'GetWindowTextW', I, HWND, w.LPWSTR, I)
        self.class_name = bind(u, 'GetClassNameW', I, HWND, w.LPWSTR, I)
        self.owner = bind(u, 'GetWindow', HWND, HWND, UINT)
        self.ancestor = bind(u, 'GetAncestor', HWND, HWND, UINT)
        self.style = bind(u, 'GetWindowLongW', w.LONG, HWND, I)
        self.thread = bind(u, 'GetWindowThreadProcessId', D, HWND, P(D))
        self.foreground = bind(u, 'GetForegroundWindow', HWND)
        self.set_foreground = bind(u, 'SetForegroundWindow', B, HWND)
        self.at_point = bind(u, 'WindowFromPoint', HWND, w.POINT)
        self.metrics = bind(u, 'GetSystemMetrics', I, I)
        self.send_input = bind(u, 'SendInput', UINT, UINT, P(Input), I)
        self.map_key = bind(u, 'MapVirtualKeyW', UINT, UINT, UINT)
        self.open_process = bind(k, 'OpenProcess', H, D, B, D)
        self.current_process = bind(k, 'GetCurrentProcess', H)
        self.close = bind(k, 'CloseHandle', B, H)
        self.process_name = bind(k, 'QueryFullProcessImageNameW', B, H, D, w.LPWSTR, P(D))
        self.open_token = bind(a, 'OpenProcessToken', B, H, D, P(H))
        self.token_info = bind(a, 'GetTokenInformation', B, H, I, c.c_void_p, D, P(D))
        self.sid_count = bind(a, 'GetSidSubAuthorityCount', P(c.c_ubyte), c.c_void_p)
        self.sid_part = bind(a, 'GetSidSubAuthority', P(D), c.c_void_p, D)
        self.cloaked = bind(d, 'DwmGetWindowAttribute', w.LONG, HWND, D, P(D), D)
        self.print_window = bind(u, 'PrintWindow', B, HWND, w.HDC, UINT)
        self.get_dc = bind(u, 'GetDC', w.HDC, HWND)
        self.release_dc = bind(u, 'ReleaseDC', I, HWND, w.HDC)
        self.create_dc = bind(g, 'CreateCompatibleDC', w.HDC, w.HDC)
        self.delete_dc = bind(g, 'DeleteDC', B, w.HDC)
        self.dib = bind(g, 'CreateDIBSection', H, w.HDC, P(BitmapInfo), UINT, P(c.c_void_p), H, D)
        self.select = bind(g, 'SelectObject', H, w.HDC, H)
        self.delete = bind(g, 'DeleteObject', B, H)
        self.blit = bind(g, 'BitBlt', B, w.HDC, I, I, I, I, w.HDC, I, I, D)
        self.flush_gdi = bind(g, 'GdiFlush', B)
        self.integrity_level = self.integrity(self.current_process())

    def integrity(self, handle):
        token = H()
        if not self.open_token(handle, 8, c.byref(token)):
            return None
        try:
            size = D()
            self.token_info(token, 25, None, 0, c.byref(size))
            if not size.value:
                return None
            data = c.create_string_buffer(size.value)
            if not self.token_info(token, 25, data, size, c.byref(size)):
                return None
            sid = c.cast(data, P(c.c_void_p))[0]
            count = self.sid_count(sid)[0]
            return self.sid_part(sid, count - 1)[0] if count else None
        finally:
            self.close(token)

    def pid(self, hwnd):
        pid = D()
        self.thread(hwnd, c.byref(pid))
        return pid.value

    def window_rect(self, hwnd):
        rect = w.RECT()
        if not self.rect(hwnd, c.byref(rect)):
            return None
        return make_rect(rect.left, rect.top, rect.right, rect.bottom)

    def shown(self, hwnd):
        flag = D()
        return bool(self.visible(hwnd) and (self.cloaked(hwnd, 14, c.byref(flag), 4) != 0 or not flag.value))

    def root(self, hwnd, ref):
        if not self.valid(hwnd) or self.ancestor(hwnd, 2) != hwnd or not self.shown(hwnd):
            raise WorkerError('missing_or_hidden_window')
        pid = self.pid(hwnd)
        handle = self.open_process(0x1000, False, pid)
        exe_path, level = '', None
        if handle:
            try:
                path, size = c.create_unicode_buffer(32768), D(32768)
                if self.process_name(handle, 0, path, c.byref(size)):
                    exe_path = path.value
                level = self.integrity(handle)
            finally:
                self.close(handle)
        title, class_name = c.create_unicode_buffer(1024), c.create_unicode_buffer(256)
        self.title(hwnd, title, len(title))
        self.class_name(hwnd, class_name, len(class_name))
        rect = self.window_rect(hwnd)
        if rect is None or self.pid(hwnd) != pid:
            raise WorkerError('window_identity_changed')
        kind = {'#32768': 'menu', '#32770': 'dialog'}.get(class_name.value)
        if kind is None:
            kind = 'popover' if self.owner(hwnd, 4) and self.style(hwnd, -16) & 0x80000000 else 'window'
        return {'root_ref': ref, 'hwnd': hwnd, 'pid': pid, 'exe_name': ntpath.basename(exe_path),
                'exe_path': exe_path, 'title': title.value, 'class_name': class_name.value, 'kind': kind,
                'rect': rect, 'dpi_scale': (self.dpi(hwnd) or 96) / 96, 'is_foreground': self.foreground() == hwnd,
                'is_minimized': bool(self.minimized(hwnd)), 'is_elevated': (
                    level is None or self.integrity_level is None or level > self.integrity_level), 'denylisted': False}

    def enumerate(self):
        windows = []

        @WNDENUMPROC
        def collect(hwnd, _lparam):
            windows.append(hwnd)
            return True

        if not self.enum(collect, 0):
            raise c.WinError(c.get_last_error())
        return windows

    def virtual_screen(self):
        return tuple(self.metrics(index) for index in (76, 77, 78, 79))

    def unobstructed(self, hwnd, rect):
        if self.minimized(hwnd) or self.style(hwnd, -20) & 0x80000:
            return False
        sx, sy, sw, sh = self.virtual_screen()
        if not contains(make_rect(sx, sy, sx + sw, sy + sh), rect['x'], rect['y']) or not contains(
            make_rect(sx, sy, sx + sw, sy + sh), rect['x'] + rect['width'] - 1, rect['y'] + rect['height'] - 1
        ):
            return False
        above = self.owner(hwnd, 3)
        while above:
            if self.shown(above) and not self.minimized(above):
                other = self.window_rect(above)
                if other is None or intersection(rect, other) is not None:
                    return False
            above = self.owner(above, 3)
        return True

    def capture(self, root):
        from PIL import Image

        rect = root['rect']
        x, y, width, height = (rect[key] for key in ('x', 'y', 'width', 'height'))
        if not (0 < width <= 16384 and 0 < height <= 16384 and width * height <= 32_000_000):
            return None, None, None, None
        screen = self.get_dc(None)
        if not screen:
            raise WorkerError('screen_dc_unavailable')
        dc, bitmap, prev = None, None, None
        try:
            dc = self.create_dc(screen)
            bits = c.c_void_p()
            header = BitmapInfo(40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0, 0)
            bitmap = self.dib(screen, c.byref(header), 0, c.byref(bits), None, 0)
            if not dc or not bitmap or not bits.value:
                raise WorkerError('capture_surface_unavailable')
            prev = self.select(dc, bitmap)
            if not prev or prev == c.c_void_p(-1).value:
                raise WorkerError('capture_bitmap_unavailable')
            c.memset(bits, 0, width * height * 4)
            image = None
            method = 'PrintWindow'
            if self.print_window(root['hwnd'], dc, 2) and self.flush_gdi():
                image = Image.frombuffer('RGB', (width, height), c.string_at(bits, width * height * 4), 'raw', 'BGRX', 0, 1)
            if image is None or image.getbbox() is None:
                method = 'BitBlt'
                hwnd = root['hwnd']
                if not self.unobstructed(hwnd, rect) or not self.blit(
                    dc, 0, 0, width, height, screen, x, y, 0x00CC0020
                ) or not self.flush_gdi() or not self.unobstructed(hwnd, rect):
                    return None, None, None, None
                image = Image.frombuffer('RGB', (width, height), c.string_at(bits, width * height * 4), 'raw', 'BGRX', 0, 1)
                if image.getbbox() is None:
                    return None, None, None, None
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            return base64.b64encode(buffer.getvalue()).decode('ascii'), image.width, image.height, method
        finally:
            if prev and prev != c.c_void_p(-1).value:
                self.select(dc, prev)
            if bitmap:
                self.delete(bitmap)
            if dc:
                self.delete_dc(dc)
            self.release_dc(None, screen)

    def inject(self, events):
        if not events:
            return
        inputs = (Input * len(events))(*events)
        sent = self.send_input(len(inputs), inputs, c.sizeof(Input))
        if sent != len(inputs):
            raise WorkerError('sendinput_partial_or_blocked')

    def chord(self, chord):
        _, mods, key = parse_chord(chord, False)
        keys = (*mods, key)
        events = []
        for key, release in [(item, False) for item in keys] + [(item, True) for item in reversed(keys)]:
            scan = self.map_key(key, 4)
            flags = (2 if release else 0) | (8 if scan else 0) | (1 if scan >> 8 == 0xE0 else 0)
            events.append(Input(kind=1, keyboard=KeyboardInput(0 if scan else key, scan & 0xFF, flags, 0, 0)))
        self.inject(events)

    def text(self, text):
        data = text.encode('utf-16-le')
        self.inject([
            Input(kind=1, keyboard=KeyboardInput(0, data[i] | data[i + 1] << 8, flag, 0, 0))
            for i in range(0, len(data), 2) for flag in (4, 6)
        ])

    def mouse(self, point, action):
        sx, sy, sw, sh = self.virtual_screen()
        x, y = point[0] - sx, point[1] - sy
        if not (0 <= x < sw and 0 <= y < sh and sw > 1 and sh > 1):
            raise WorkerError('point_outside_desktop')
        dx, dy = round(65535 * x / (sw - 1)), round(65535 * y / (sh - 1))
        events = [Input(kind=0, mouse=MouseInput(dx, dy, 0, 0xC001, 0, 0))]
        name = action['action']
        if name == 'scroll':
            direction = action['direction']
            amount = 120 * action.get('amount', 1) * (-1 if direction in ('down', 'left') else 1)
            events.append(Input(kind=0, mouse=MouseInput(0, 0, amount, 0x1000 if direction in ('left', 'right') else 0x800, 0, 0)))
        else:
            flags = (8, 16) if name == 'right_click' else (2, 4)
            for flag in flags * (2 if name == 'double_click' else 1):
                events.append(Input(kind=0, mouse=MouseInput(0, 0, 0, flag, 0, 0)))
        self.inject(events)


def make_rect(left, top, right, bottom):
    return {'x': int(left), 'y': int(top), 'width': max(0, int(right) - int(left)),
            'height': max(0, int(bottom) - int(top))}


def contains(rect, x, y):
    return rect['x'] <= x < rect['x'] + rect['width'] and rect['y'] <= y < rect['y'] + rect['height']


def intersection(a, b):
    result = make_rect(max(a['x'], b['x']), max(a['y'], b['y']),
                       min(a['x'] + a['width'], b['x'] + b['width']),
                       min(a['y'] + a['height'], b['y'] + b['height']))
    return result if result['width'] and result['height'] else None


def image_rect(rect, root, width, height):
    clipped = intersection(rect, root)
    if not clipped:
        return None
    x = (clipped['x'] - root['x']) * width / root['width']
    y = (clipped['y'] - root['y']) * height / root['height']
    right = x + clipped['width'] * width / root['width']
    bottom = y + clipped['height'] * height / root['height']
    from math import ceil, floor

    return make_rect(floor(x), floor(y), min(width, ceil(right)), min(height, ceil(bottom)))


PATTERNS = {
    'invoke': (10000, 'Invoke'), 'scroll': (10004, 'Scroll'), 'value': (10002, 'Value'),
    'expand_collapse': (10005, 'ExpandCollapse'), 'selection_item': (10010, 'SelectionItem'),
    'text': (10014, 'Text'), 'toggle': (10015, 'Toggle'), 'legacy': (10018, 'LegacyIAccessible'),
}
PATTERN_ACTIONS = {'invoke': 'invoke', 'set_text': 'value', 'toggle': 'toggle', 'select': 'selection_item',
                   'expand': 'expand_collapse', 'collapse': 'expand_collapse', 'scroll': 'scroll'}


class Automation:
    def __init__(self, native):
        # comtypes initializes its importing thread itself; import only on the MTA.
        sys.coinit_flags = 0
        import comtypes
        import comtypes.client

        self.com_error = comtypes.COMError
        self.module = comtypes.client.GetModule('UIAutomationCore.dll')
        self.uia = comtypes.client.CreateObject(self.module.CUIAutomation, interface=self.module.IUIAutomation)
        self.walker = self.uia.ControlViewWalker
        self.native = native

    def pattern(self, element, name):
        pattern_id, interface = PATTERNS[name]
        try:
            obj = element.GetCurrentPattern(pattern_id)
            return obj.QueryInterface(getattr(self.module, f'IUIAutomation{interface}Pattern')) if obj else None
        except self.com_error as exc:
            # Providers return UIA_E_NOTSUPPORTED rather than a null pointer on some controls.
            if exc.hresult & 0xFFFFFFFF in (0x80040204, 0x80004002):
                return None
            raise

    def walk(self, root_element, limit, depth_limit):
        # Fetch one sibling at a time so provider calls are bounded even on wide trees.
        pending = deque([(root_element, 0, False)])
        count = 0
        while pending and count < limit:
            element, depth, sibling = pending.popleft()
            if not element:
                continue
            if sibling:
                following = self.walker.GetNextSiblingElement(element)
                if following:
                    pending.appendleft((following, depth, True))
            child = None if depth > depth_limit else self.walker.GetFirstChildElement(element)
            if child and depth < depth_limit:
                pending.append((child, depth + 1, True))
            count += 1
            yield element, depth, bool((depth == depth_limit and child) or (count == limit and pending))

    def runtime_id(self, element):
        return tuple(element.GetRuntimeId() or ())

    def label(self, element):
        code = element.CurrentControlType
        # The generated type library exposes language-neutral UIA control identifiers.
        return ROLES.get(code, element.CurrentLocalizedControlType or 'Control')

    def describe(self, element, index, depth, truncated):
        rect = element.CurrentBoundingRectangle
        password = bool(element.CurrentIsPassword)
        capabilities = [name for name in PATTERNS if name != 'legacy' and self.pattern(element, name)]
        if 'invoke' not in capabilities and self.pattern(element, 'legacy'):
            capabilities.append('invoke')
        value = ''
        if not password and 'value' in capabilities:
            value = self.pattern(element, 'value').CurrentValue or ''
        elif not password and 'text' in capabilities:
            value = self.pattern(element, 'text').DocumentRange.GetText(240) or ''
        return {'ref': f'@e{index}', 'role': self.label(element), 'name': (element.CurrentName or '')[:240],
                'automation_id': (element.CurrentAutomationId or '')[:256], 'value_preview': value[:240],
                'rect': make_rect(rect.left, rect.top, rect.right, rect.bottom), 'image_rect': None,
                'enabled': bool(element.CurrentIsEnabled), 'focused': bool(element.CurrentHasKeyboardFocus),
                'capabilities': capabilities, 'depth': depth, 'truncated': truncated,
                'runtime_id': self.runtime_id(element)}

    def checked_root(self, request):
        expected = request['identity']
        root = self.native.root(expected['hwnd'], request.get('root_ref', ''))
        if not root['exe_path'] or identity(root) != expected:
            raise WorkerError('window_identity_changed')
        if matches_patterns(root, request.get('denylist_patterns', [])):
            raise WorkerError('denylisted_target')
        return root

    def authorize_live(self, request, root, action, element):
        approved_title = request['approved_title']
        if approved_title is not None:
            if root['title'] != approved_title:
                raise WorkerError('approved_window_title_changed')
            return
        if matches_patterns(root, request['allowlist_patterns']):
            return
        if action['action'] == 'key' and risky_chord(action['key']):
            raise WorkerError('one_time_approval_required')
        examined = element
        for _ in range(64):
            if not examined:
                return
            if destructive_control(self.label(examined), examined.CurrentName or ''):
                raise WorkerError('live_target_requires_confirmation')
            if examined.CurrentNativeWindowHandle == root['hwnd']:
                return
            examined = self.uia.RawViewWalker.GetParentElement(examined)
        raise WorkerError('element_ancestry_unverified')

    def find(self, root_element, stored, limit):
        choices = []
        for element, _, _ in self.walk(root_element, limit, 10):
            if stored['runtime_id'] and tuple(stored['runtime_id']) == self.runtime_id(element):
                if self.label(element) != stored['role'] or (element.CurrentAutomationId or '')[:256] != stored['automation_id']:
                    raise WorkerError('element_identity_changed')
                return element
            if not stored['runtime_id'] and stored['automation_id'] and (
                (element.CurrentAutomationId or '')[:256] == stored['automation_id'] and self.label(element) == stored['role']
            ):
                choices.append(element)
        if len(choices) == 1:
            return choices[0]
        raise WorkerError('stale_or_ambiguous_element')

    def observe(self, request):
        root = self.checked_root(request)
        root_element = self.uia.ElementFromHandle(root['hwnd'])
        elements = [self.describe(element, index, depth, truncated)
                    for index, (element, depth, truncated) in enumerate(self.walk(root_element, request['max_elements'], 10), 1)]
        if not elements:
            raise WorkerError('uia_root_unavailable')
        png, width, height, method = None, None, None, None
        if request['include_image']:
            png, width, height, method = self.native.capture(root)
        current = self.checked_root(request)
        if current['rect'] != root['rect'] or current['dpi_scale'] != root['dpi_scale']:
            raise WorkerError('window_geometry_changed')
        if png:
            for element in elements:
                element['image_rect'] = image_rect(element['rect'], root['rect'], width, height)
        return {'root': root, 'elements': elements, 'png': png, 'image_width': width,
                'image_height': height, 'capture_method': method}

    def semantic(self, action, pattern):
        name = action['action']
        if name == 'invoke':
            pattern.Invoke()
        elif name == 'set_text':
            pattern.SetValue(action['text'])
            return 'worked' if pattern.CurrentValue == action['text'] else 'didnt'
        elif name == 'toggle':
            pattern.Toggle()
        elif name == 'select':
            pattern.Select()
        elif name == 'expand':
            pattern.Expand()
        elif name == 'collapse':
            pattern.Collapse()
        elif name == 'scroll':
            direction = action['direction']
            movement = 0 if direction in ('up', 'left') else 3
            horizontal = direction in ('left', 'right')
            for _ in range(action.get('amount', 1)):
                pattern.Scroll(movement if horizontal else 2, 2 if horizontal else movement)
        return 'unknown'

    def point(self, element, action, root):
        if 'x' in action:
            return action['x'], action['y']
        bounds = root['rect']
        if element:
            rect = element.CurrentBoundingRectangle
            bounds = intersection(bounds, make_rect(rect.left, rect.top, rect.right, rect.bottom))
        if not bounds or not bounds['width'] or not bounds['height']:
            return None
        return bounds['x'] + bounds['width'] // 2, bounds['y'] + bounds['height'] // 2

    def preflight(self, request, root, point, target):
        hwnd = root['hwnd']
        current = self.checked_root(request)
        if current['rect'] != root['rect'] or current['dpi_scale'] != root['dpi_scale']:
            raise WorkerError('window_geometry_changed')
        if current['is_elevated'] or current['is_minimized'] or self.native.foreground() != hwnd:
            raise WorkerError('foreground_not_verified')
        if point is None or not contains(root['rect'], *point):
            raise WorkerError('point_outside_target')
        native_point = w.POINT(*point)
        hit = self.native.at_point(native_point)
        if not hit or self.native.ancestor(hit, 2) != hwnd or self.native.pid(hit) != root['pid']:
            raise WorkerError('occluded_target')
        element = self.uia.ElementFromPoint(self.module.tagPOINT(*point))
        if not element or element.CurrentProcessId != root['pid']:
            raise WorkerError('occluded_target')
        if target:
            candidate = element
            for _ in range(64):
                if not candidate:
                    break
                if self.uia.CompareElements(candidate, target):
                    break
                candidate = self.uia.RawViewWalker.GetParentElement(candidate)
            else:
                candidate = None
            if not candidate:
                raise WorkerError('occluded_target')
        if self.native.foreground() != hwnd:
            raise WorkerError('foreground_not_verified')
        return current, element

    def focus_point(self, root, element):
        if not element or element.CurrentProcessId != root['pid']:
            raise WorkerError('target_focus_not_verified')
        current = element
        for _ in range(64):
            if not current:
                break
            if current.CurrentNativeWindowHandle == root['hwnd']:
                return self.point(element, {}, root)
            current = self.uia.RawViewWalker.GetParentElement(current)
        raise WorkerError('target_focus_not_verified')

    def prepare_foreground(self, request, root, element, action, point):
        if not request['allow_foreground_input']:
            raise WorkerError('foreground_input_disabled')
        if root['is_elevated'] or root['is_minimized']:
            raise WorkerError('elevated_or_minimized_target')
        if 'x' in action and (root['rect'] != request['foreground_rect'] or root['dpi_scale'] != request['foreground_dpi']):
            raise WorkerError('window_geometry_changed')
        self.authorize_live(request, self.checked_root(request), action, element)
        hwnd = root['hwnd']
        if self.native.foreground() != hwnd:
            self.native.set_foreground(hwnd)
            if self.native.foreground() != hwnd:
                raise WorkerError('foreground_not_verified')
        name = action['action']
        keys = name in ('type_text', 'set_text', 'key', 'expand', 'collapse')
        if not keys or element:
            current, hit = self.preflight(request, root, point, element)
            self.authorize_live(request, current, action, hit)
        focused = None
        if keys:
            if element:
                element.SetFocus()
            focused = self.uia.GetFocusedElement()
            if element and not self.uia.CompareElements(element, focused):
                raise WorkerError('target_focus_not_verified')
            point = self.focus_point(root, focused)
            current, hit = self.preflight(request, root, point, focused)
            self.authorize_live(request, current, action, hit)
        return point, focused

    def foreground_action(self, request, root, action, point, focused, target):
        name = action['action']
        if focused:
            self.verify_focus(request, root, action, point, focused)
        if name in ('set_text', 'type_text'):
            if action.get('clear_first', name == 'set_text'):
                self.native.chord('Ctrl+A')
                self.verify_focus(request, root, action, point, focused)
                self.native.chord('Backspace')
                self.verify_focus(request, root, action, point, focused)
            self.native.text(action['text'])
        elif name in ('expand', 'collapse', 'key'):
            self.native.chord(action.get('key') or ('Right' if name == 'expand' else 'Left'))
        else:
            current, hit = self.preflight(request, root, point, target)
            self.authorize_live(request, current, action, hit)
            self.native.mouse(point, action)

    def verify_focus(self, request, root, action, point, expected):
        focused = self.uia.GetFocusedElement()
        if not focused or not self.uia.CompareElements(expected, focused) or self.focus_point(root, focused) != point:
            raise WorkerError('target_focus_not_verified')
        current, hit = self.preflight(request, root, point, focused)
        self.authorize_live(request, current, action, hit)

    def check_expect(self, request, expect):
        deadline = time.monotonic() + expect.get('timeout_ms', 1500) / 1000
        while True:
            root = self.checked_root(request)
            found, truncated = False, False
            for element, _, clipped in self.walk(self.uia.ElementFromHandle(root['hwnd']), 512, 10):
                truncated = truncated or clipped
                if 'role' in expect and expect['role'].casefold() != self.label(element).casefold():
                    continue
                if 'text' in expect:
                    query = expect['text'].casefold()
                    if query not in (element.CurrentName or '').casefold():
                        if element.CurrentIsPassword:
                            continue
                        value = self.pattern(element, 'value')
                        if not value or query not in (value.CurrentValue or '')[:240].casefold():
                            continue
                found = True
                break
            gone = expect.get('gone', False)
            if (found and not gone) or (gone and not found and not truncated):
                return 'worked'
            left = deadline - time.monotonic()
            if left <= 0:
                return 'unknown' if truncated and not found else 'didnt'
            time.sleep(min(0.15, left))

    def act(self, request):
        results = []
        stored = {element['ref']: element for element in request['elements']}
        for action in request['actions']:
            name, ref = action['action'], action.get('ref')
            report = {'action': name, 'ref': ref, 'point': None, 'method': None, 'outcome': 'blocked', 'reason': None}
            results.append(report)
            dispatched = False
            try:
                root = self.checked_root(request)
                if root['is_elevated']:
                    raise WorkerError('elevated_or_unverified_target')
                root_element = self.uia.ElementFromHandle(root['hwnd'])
                element = self.find(root_element, stored[ref], request['max_elements']) if ref else None
                if element and (not element.CurrentIsEnabled or element.CurrentProcessId != root['pid']):
                    raise WorkerError('element_disabled_or_foreign')
                if element and (element.CurrentName or '')[:240] != stored[ref]['name']:
                    raise WorkerError('element_name_changed')
                point = self.point(element, action, root)
                report['point'] = point
                pattern_name = PATTERN_ACTIONS.get(name)
                target = element if element is not None else root_element
                pattern = self.pattern(target, pattern_name) if pattern_name else None
                legacy = self.pattern(target, 'legacy') if name == 'invoke' and not pattern else None
                if pattern or (legacy and legacy.CurrentDefaultAction):
                    current = self.checked_root(request)
                    if current['is_elevated']:
                        raise WorkerError('elevated_or_unverified_target')
                    self.authorize_live(request, current, action, target)
                    if name == 'set_text' and pattern.CurrentIsReadOnly:
                        raise WorkerError('element_read_only')
                    report['method'] = 'UIA'
                    dispatched = True
                    if pattern:
                        report['outcome'] = self.semantic(action, pattern)
                    else:
                        legacy.DoDefaultAction()
                        report['outcome'] = 'unknown'
                else:
                    point, focused = self.prepare_foreground(request, root, element, action, point)
                    report['point'], report['method'] = point, 'SendInput'
                    dispatched = True
                    self.foreground_action(request, root, action, point, focused, element)
                    report['outcome'] = 'unknown'
                self.checked_root(request)
                if 'expect' in action:
                    report['outcome'] = self.check_expect(request, action['expect'])
            except (WorkerError, self.com_error, OSError) as exc:
                report['outcome'] = 'unknown' if dispatched else 'blocked'
                report['reason'] = str(exc) if isinstance(exc, WorkerError) else type(exc).__name__
                break
            if report['outcome'] == 'didnt':
                break
        return results

    def dispatch(self, request):
        op = request['op']
        if op == 'list_roots':
            roots = []
            for hwnd in self.native.enumerate():
                if self.native.shown(hwnd):
                    try:
                        roots.append(self.native.root(hwnd, f'@r{len(roots) + 1}'))
                    except WorkerError as exc:
                        print(f'Root skipped: {exc}', file=sys.stderr)
            return roots
        if op == 'identify':
            return self.native.root(request['hwnd'], request['root_ref'])
        if op == 'observe':
            return self.observe(request)
        if op == 'act':
            return self.act(request)
        raise WorkerError('unknown_operation')


ROLES = dict(enumerate((
    'Button', 'Calendar', 'CheckBox', 'ComboBox', 'Edit', 'Hyperlink', 'Image', 'ListItem', 'List', 'Menu',
    'MenuBar', 'MenuItem', 'ProgressBar', 'RadioButton', 'ScrollBar', 'Slider', 'Spinner', 'StatusBar', 'Tab',
    'TabItem', 'Text', 'ToolBar', 'ToolTip', 'Tree', 'TreeItem', 'Custom', 'Group', 'Thumb', 'DataGrid',
    'DataItem', 'Document', 'SplitButton', 'Window', 'Pane', 'Header', 'HeaderItem', 'Table', 'TitleBar',
    'Separator', 'SemanticZoom', 'AppBar',
), 50000))


def hotkey_loop(chord, emit):
    user = c.WinDLL('user32', use_last_error=True)
    register = bind(user, 'RegisterHotKey', B, HWND, I, UINT, UINT)
    unregister = bind(user, 'UnregisterHotKey', B, HWND, I)
    peek = bind(user, 'PeekMessageW', B, P(w.MSG), HWND, UINT, UINT, UINT)
    get = bind(user, 'GetMessageW', B, P(w.MSG), HWND, UINT, UINT)
    mask, _, key = parse_chord(chord, True)
    msg = w.MSG()
    peek(c.byref(msg), None, 0, 0, 0)
    registered = bool(register(None, 1, mask | 0x4000, key))
    emit({'event': 'hotkey_registration', 'hotkey': chord, 'registered': registered,
          'error': 0 if registered else c.get_last_error()})
    if not registered:
        return
    try:
        while (result := get(c.byref(msg), None, 0, 0)) > 0:
            if msg.message == 0x0312 and msg.wParam == 1:
                emit({'event': 'hotkey_panic', 'hotkey': chord})
        if result < 0:
            emit({'event': 'hotkey_registration', 'hotkey': chord, 'registered': False, 'error': c.get_last_error()})
    finally:
        unregister(None, 1)


def uia_loop(requests, emit):
    native = Native()
    automation = None
    while (request := requests.get()) is not None:
        response = {'id': request['id']}
        try:
            if automation is None:
                automation = Automation(native)
            response['result'] = automation.dispatch(request)
        except WorkerError as exc:
            response['error'] = str(exc)
        except Exception as exc:  # noqa: BLE001
            print(f'Worker {request["op"]}: {type(exc).__name__}: {exc}', file=sys.stderr)
            response['error'] = f'worker_{type(exc).__name__}'
        emit(response)


def main():
    if sys.platform != 'win32':
        raise SystemExit('The computer-use worker requires Windows')
    protocol, sys.stdout = sys.stdout, sys.stderr
    protocol.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    output_lock = threading.Lock()
    requests = Queue(maxsize=1)

    def emit(record):
        line = json.dumps(record, ensure_ascii=True, separators=(',', ':'))
        with output_lock:
            protocol.write(line + '\n')
            protocol.flush()

    mta = threading.Thread(target=uia_loop, args=(requests, emit), name='computer-use-uia', daemon=True)
    mta.start()
    if len(sys.argv) > 1:
        threading.Thread(target=hotkey_loop, args=(sys.argv[1], emit), name='computer-use-hotkey', daemon=True).start()
    for line in sys.stdin:
        requests.put(json.loads(line))
    requests.put(None)
    mta.join(1)


if __name__ == '__main__':
    main()
