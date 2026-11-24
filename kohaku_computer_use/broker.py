"""Single process-wide authority over observations, leases, approvals, and input."""
from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, get_args
from uuid import uuid4
from weakref import ReferenceType, ref

from . import cdp
from .models import (
    DEFAULT_POLICY,
    Action,
    ActionPoint,
    ActionReport,
    Actor,
    ActPayload,
    Approval,
    AuditEntry,
    Event,
    Lease,
    Outcome,
    Reasoning,
    RootItem,
    Snapshot,
    StateDetail,
    StateIndex,
    StatusPayload,
    WorkerRequest,
    WorkerRoot,
    WorkerSnapshot,
    destructive_control,
    identity,
    matches_patterns,
    parse_chord,
    risky_chord,
)

if TYPE_CHECKING:
    from kohakuterrarium.modules.tool.base import ToolContext

log = logging.getLogger(__name__)
CALL_TIMEOUT = 12.0
HOTKEY_READY_TIMEOUT = 2.5
OPERATOR = 'operator'
DELEGATE_CANCEL = 'computer_use.delegate_cancel'


class BrokerError(Exception):
    def __init__(self, status: int, reason: str, outcome: Outcome):
        super().__init__(reason)
        self.status, self.outcome = status, outcome


def _plugin_enabled(agent: object) -> bool:
    manager = getattr(agent, 'plugin_manager', None) or getattr(agent, 'plugins', None)
    return manager is not None and manager.is_enabled('computer_use')


def runtime_session_id(agent: object, session: object) -> str:
    return str(getattr(getattr(agent, 'session_store', None), 'session_id', None)
               or getattr(session, 'session_id', None) or getattr(session, 'key', None) or '')


def actor_for(context: ToolContext | None) -> Actor:
    if context is None:
        raise BrokerError(403, 'missing_tool_context', 'blocked')
    session = runtime_session_id(context.agent, context.session)
    if not (session and context.creature_id and context.agent_name):
        raise BrokerError(403, 'missing_creature_identity', 'blocked')
    return Actor(str(session), context.creature_id, context.agent_name)


def action_key(actions: list[Action]) -> str:
    return json.dumps(actions, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def public_root(root: WorkerRoot) -> RootItem:
    return {key: deepcopy(value) for key, value in root.items() if key not in ('exe_path', 'ws_url', 'document_id')}


def window_rank(root: WorkerRoot) -> tuple[bool, bool, bool, int]:
    width, height = root['rect']['width'], root['rect']['height']
    minimized = root['is_minimized']
    tiny = not minimized and (width < 80 or height < 60)
    return not tiny, root['is_foreground'], not minimized, width * height


def action_needs_confirmation(snapshot: Snapshot, actions: list[Action]) -> bool:
    for action in actions:
        if action['action'] == 'key' and risky_chord(action['key']):
            return True
        for element in snapshot.elements:
            rect = element['rect']
            is_target = action.get('ref') == element['ref'] or (
                'x' in action and rect['x'] <= action['x'] < rect['x'] + rect['width']
                and rect['y'] <= action['y'] < rect['y'] + rect['height']
            ) or (not action.get('ref') and 'x' not in action and element['focused'])
            if is_target and destructive_control(element['role'], element['name']):
                return True
    return False


class WorkerPipe:
    def __init__(self, hotkey: str | None, event: Callable):
        from kohakuterrarium.utils.interpreter import python_command

        env = os.environ.copy()
        package_parent = str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [package_parent, env.get('PYTHONPATH')]))
        env['PYTHONIOENCODING'] = 'utf-8'
        self.hotkey = hotkey
        command = [*python_command(), '-m', 'kohaku_computer_use.win_worker']
        if hotkey is not None:
            command.append(hotkey)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.outgoing = queue.Queue()
        self.ready = threading.Event()
        self.event = event

    def start(self):
        for name, task in [('read', self.read), ('write', self.write), ('stderr', self.stderr)]:
            threading.Thread(target=task, daemon=True, name=f'computer-use-{name}').start()

    def read(self):
        try:
            with self.process.stdout as stream:
                for line in stream:
                    self.event(self, json.loads(line))
        except (ValueError, OSError) as exc:
            log.warning('Desktop worker output error: %s', exc)
        finally:
            self.event(self, {'event': 'worker_exit'})
            self.ready.set()

    def write(self):
        try:
            with self.process.stdin as stream:
                while (request := self.outgoing.get()) is not None:
                    stream.write(json.dumps(request, ensure_ascii=True, separators=(',', ':')) + '\n')
                    stream.flush()
        except OSError as exc:
            log.warning('Desktop worker input closed: %s', exc)
            self.event(self, {'event': 'worker_exit'})

    def stderr(self):
        with self.process.stderr as stream:
            for line in stream:
                log.warning('Desktop worker: %s', line.rstrip())

    def stop(self):
        self.outgoing.put(None)
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=2)


class DesktopBroker:
    def __init__(self):
        self.supported = sys.platform == 'win32'
        self.policy = DEFAULT_POLICY
        self.tripped = False
        self.trip_reason: str | None = None
        self.hotkey_registered = False
        self.generation = 0
        self.epoch = 0
        self.deadline: float | None = None
        self.timer: threading.Timer | None = None
        self.lease: Lease | None = None
        self._agents: list[tuple[ReferenceType[object], str, str]] = []
        self._had_enabled_agent = False
        self.approvals: dict[str, Approval] = {}
        self.states: deque[Snapshot] = deque(maxlen=16)
        self.audit: deque[AuditEntry] = deque(maxlen=30)
        self.stats = {'observations': 0, 'actions': 0, 'blocked': 0}
        self.roots: dict[str, WorkerRoot] = {}
        self.studio_origins = cdp.studio_origins()
        self.next_root = 0
        self.lock = threading.RLock()
        self.calls = threading.Lock()
        self.worker: WorkerPipe | None = None
        self.serial = 0
        self.pending: tuple[int, queue.Queue] | None = None
        self.inflight: tuple[Actor, str] | None = None

    def _record(self, actor: str, event: Event, title: str | None, summary: str, outcome: Outcome):
        if event == 'blocked':
            self.stats['blocked'] += 1
        self.audit.appendleft({'id': uuid4().hex, 'timestamp': time.time(), 'actor': actor, 'event': event,
                               'target_title': title, 'summary': summary, 'outcome': outcome})

    def record_block(self, actor: str, reason: str):
        with self.lock:
            self._record(actor, 'blocked', None, reason, 'blocked')

    def _invalidate(self):
        self.generation += 1
        self.lease = None
        self.approvals.clear()

    def _stop_worker(self, reason: str):
        worker, self.worker = self.worker, None
        self.hotkey_registered = False
        if self.pending is not None:
            self.pending[1].put({'interrupted': reason})
            self.pending = None
        if worker:
            try:
                worker.stop()
            except (OSError, subprocess.TimeoutExpired):
                log.exception('Could not reap desktop worker')
                self.tripped, self.trip_reason = True, 'worker_reap_failed'
                self.worker = worker
            finally:
                worker.ready.set()

    def _finish_locked_read(self):
        # Window references are broker-owned and survive a temporary UIA-only worker.
        if self.policy.mode == 'locked' and self.worker is not None and self.worker.hotkey is None:
            self._stop_worker('locked_read_finished')

    def _lock_mode(self, reason: str, stop_worker: bool):
        old = self.policy.mode
        self.policy = replace(self.policy, mode='locked')
        self.deadline = None
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self._invalidate()
        if stop_worker:
            self._stop_worker(reason)
        if old != 'locked':
            self._record('broker', 'mode_change', None, reason, 'info')

    def _idle(self):
        with self.lock:
            if self.deadline is not None and self.deadline <= time.monotonic():
                self._lock_mode('idle_timeout', True)

    def _touch(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.policy.mode == 'locked' or self.policy.idle_timeout_s <= 0:
            self.deadline = None
            return
        self.deadline = time.monotonic() + self.policy.idle_timeout_s
        self.timer = threading.Timer(self.policy.idle_timeout_s, self._idle)
        self.timer.daemon = True
        self.timer.start()

    def _spawn(self):
        if not self.supported:
            raise BrokerError(501, 'unsupported_platform', 'blocked')
        if self.tripped:
            raise BrokerError(409, 'panic_latched', 'blocked')
        worker = self.worker
        if worker is None:
            try:
                hotkey = self.policy.hotkey if self.policy.mode != 'locked' else None
                worker = WorkerPipe(hotkey, self._worker_event)
                self.worker = worker
                worker.start()
            except OSError as exc:
                log.warning('Desktop worker start failed: %s', exc)
                self._lock_mode('worker_start_failed', True)
                raise BrokerError(503, 'worker_start_failed', 'blocked') from exc
        return worker

    def _retry_unavailable_worker(self):
        worker = self.worker
        if worker is not None and (worker.hotkey != self.policy.hotkey or worker.process.poll() is not None or
                                   (worker.ready.is_set() and not self.hotkey_registered)):
            self._stop_worker('retry_hotkey')
            if self.tripped:
                self._lock_mode('worker_reap_failed', False)

    def ensure_hotkey(self):
        with self.lock:
            self._idle()
            if not self.supported or self.tripped or self.policy.mode == 'locked':
                return
            self._retry_unavailable_worker()
            worker = self._spawn()
        worker.ready.wait(HOTKEY_READY_TIMEOUT)

    def _worker_event(self, worker: WorkerPipe, message: dict):
        with self.lock:
            if worker is not self.worker:
                return
            event = message.get('event')
            if event == 'hotkey_registration':
                if self.tripped or self.policy.mode == 'locked' or worker.hotkey is None:
                    return
                self.hotkey_registered = bool(message['registered'])
                if not self.hotkey_registered:
                    log.warning('Could not register desktop hotkey %s (Windows error %s)', message['hotkey'], message.get('error'))
                    if self.policy.mode in ('confirm', 'armed'):
                        self._lock_mode('hotkey_unavailable', True)
                worker.ready.set()
            elif event == 'hotkey_panic':
                self.panic(f'Global hotkey {message["hotkey"]}', 'hotkey')
            elif event == 'worker_exit':
                self._lock_mode('worker_exit', True)
            elif self.pending is not None and message.get('id') == self.pending[0]:
                self.pending[1].put(message)

    def _rpc(self, request: WorkerRequest, guard: Callable[[], None]):
        mutating = request['op'] == 'act'
        with self.lock:
            guard()
            worker = self._spawn()
            self.serial += 1
            request['id'] = self.serial
            replies = queue.Queue()
            guard()
            if self.worker is not worker:
                raise BrokerError(409, 'worker_replaced', 'blocked')
            if worker.process.poll() is not None:
                self._lock_mode('worker_exit', True)
                raise BrokerError(409, 'worker_exit', 'blocked')
            self.pending = (self.serial, replies)
            worker.outgoing.put(request)
        try:
            waits = sum(action.get('wait_ms', 0) + (action['expect'].get('timeout_ms', 1500)
                        if 'expect' in action else 0) for action in request.get('actions', ()))
            reply = replies.get(timeout=CALL_TIMEOUT + waits / 1000)
        except queue.Empty as exc:
            with self.lock:
                if self.worker is worker:
                    self._lock_mode('worker_timeout', True)
            raise BrokerError(504, 'worker_timeout', 'unknown' if mutating else 'blocked') from exc
        with self.lock:
            if self.pending is not None and self.pending[1] is replies:
                self.pending = None
            if 'interrupted' in reply or self.worker is not worker:
                raise BrokerError(409, reply.get('interrupted', 'worker_replaced'), 'unknown' if mutating else 'blocked')
        if 'error' in reply:
            reason = reply['error']
            status = 403 if reason == 'denylisted_target' else 502 if reason.startswith('worker_') else 409
            raise BrokerError(status, reason, 'unknown' if mutating else 'blocked')
        return reply['result']

    def track_agent(self, host: object, agent_name: str, session_id: str, loaded: bool):
        with self.lock:
            self._agents = [item for item in self._agents if item[0]() is not host]
            if loaded:
                self._agents.append((ref(host), agent_name, session_id))
            self._reconcile_plugin_disable()

    def _reconcile_plugin_disable(self):
        self._agents = [item for item in self._agents if item[0]() is not None]
        hosts = [(host, name, session, _plugin_enabled(host))
                 for handle, name, session in self._agents if (host := handle()) is not None]
        enabled = any(active for _, _, _, active in hosts)
        if self._had_enabled_agent and not enabled:
            if self.policy.mode != 'locked':
                self._lock_mode('plugin_disabled', True)
            else:
                self._invalidate()
        for host, name, session, active in hosts:
            if not active:
                creature = getattr(host, 'creature_id', None) or getattr(host, '_creature_id', '')
                self.release(runtime_session_id(host, getattr(host, 'session', None)) or session, creature, name)
        self._had_enabled_agent = enabled

    def status(self) -> StatusPayload:
        with self.lock:
            self._reconcile_plugin_disable()
            self._idle()
            lease = self.lease
            payload = asdict(self.policy)
            payload['denylist_patterns'] = list(self.policy.denylist_patterns)
            payload['allowlist_patterns'] = list(self.policy.allowlist_patterns)
            payload['cdp_ports'] = list(self.policy.cdp_ports)
            settings = {key: payload[key] for key in ('desktop_model', 'desktop_reasoning_effort', 'cdp_ports')}
            return {**payload, 'policy': settings, 'supported': self.supported, 'platform': sys.platform, 'tripped': self.tripped,
                    'trip_reason': self.trip_reason, 'hotkey_registered': self.hotkey_registered,
                    'idle_remaining_s': max(0., self.deadline - time.monotonic()) if self.deadline is not None else None,
                    'active_lease': {**asdict(lease.actor), 'updated_at': lease.updated_at,
                                     'target_hwnd': lease.target_hwnd, 'target_title': lease.target_title} if lease else None,
                    'latest_state_id': self.states[0].state['state_id'] if self.states else None,
                    'worker_alive': self.worker is not None and self.worker.process.poll() is None,
                    'stats': dict(self.stats)}

    def change_policy(self, changes: dict) -> StatusPayload:
        with self.lock:
            self._reconcile_plugin_disable()
            mode = changes.get('mode', self.policy.mode)
            if self.tripped and mode != 'locked':
                raise BrokerError(409, 'panic_latched', 'blocked')
            if mode != 'locked' and not self.supported:
                raise BrokerError(501, 'unsupported_platform', 'blocked')
            if mode not in ('locked', 'read_only', 'confirm', 'armed'):
                raise BrokerError(422, 'invalid_mode', 'blocked')
            changes = dict(changes)
            if 'desktop_model' in changes:
                model = changes['desktop_model']
                if not isinstance(model, str) or len(model.strip()) > 200:
                    raise BrokerError(422, 'invalid_desktop_model', 'blocked')
                changes['desktop_model'] = model.strip()
            if changes.get('desktop_reasoning_effort', 'low') not in get_args(Reasoning):
                raise BrokerError(422, 'invalid_desktop_reasoning_effort', 'blocked')
            if 'cdp_ports' in changes:
                ports = changes['cdp_ports']
                if not isinstance(ports, (list, tuple)) or any(type(port) is not int or not 1024 <= port <= 65535 for port in ports):
                    raise BrokerError(422, 'invalid_cdp_ports', 'blocked')
                changes['cdp_ports'] = tuple(dict.fromkeys(ports))
                if len(changes['cdp_ports']) > 8:
                    raise BrokerError(422, 'too_many_cdp_ports', 'blocked')
            try:
                if 'hotkey' in changes:
                    parse_chord(changes['hotkey'], True)
                for key in ('denylist_patterns', 'allowlist_patterns'):
                    if key in changes:
                        changes[key] = tuple(changes[key])
                        for pattern in changes[key]:
                            re.compile(pattern, re.IGNORECASE)
            except (ValueError, re.error) as exc:
                raise BrokerError(422, f'invalid_policy: {exc}', 'blocked') from exc
            old = self.policy
            new = replace(old, **changes)
            if old == new:
                if new.mode == 'locked' and self.trip_reason == 'worker_reap_failed':
                    self._stop_worker('operator_lock')
                    if self.worker is not None:
                        raise BrokerError(503, 'worker_reap_failed', 'blocked')
                if new.mode not in ('confirm', 'armed'):
                    return self.status()
                new = old
            else:
                if new.mode != 'locked' and (self.pending is not None or old.hotkey != new.hotkey):
                    self._stop_worker('policy_changed')
                if self.tripped:
                    new = replace(new, mode='locked')
                self._invalidate()
                self.policy = new
                if new.mode == 'locked':
                    self._lock_mode('operator_lock', True)
                else:
                    self._touch()
                self._record(OPERATOR, 'mode_change', None, f'{old.mode} → {new.mode}', 'info')
            if self.tripped or new.mode == 'locked':
                if self.worker is not None:
                    raise BrokerError(503, 'worker_reap_failed', 'blocked')
                return self.status()
            self._retry_unavailable_worker()
            worker = self._spawn()

        ready = worker.ready.wait(HOTKEY_READY_TIMEOUT)
        with self.lock:
            status = self.status()
            if new.mode in ('confirm', 'armed'):
                if self.tripped:
                    raise BrokerError(409, 'panic_latched', 'blocked')
                if (not ready or self.worker is not worker or not status['worker_alive']
                        or not status['hotkey_registered']):
                    # A newer request may have replaced the worker without changing the policy.
                    if self.policy is new and self.worker is worker:
                        self._lock_mode('hotkey_unavailable', True)
                    raise BrokerError(409, f'hotkey_unavailable: Global hotkey {new.hotkey} is unavailable or occupied by another process', 'blocked')
                if self.policy is not new:
                    raise BrokerError(409, 'desktop_policy_changed', 'blocked')
            return status

    def panic(self, reason: str, actor: str) -> StatusPayload:
        with self.lock:
            self.tripped, self.trip_reason = True, reason
            self._lock_mode('panic', True)
            self._record(actor, 'panic', None, reason, 'info')
            return self.status()

    def reset_panic(self) -> StatusPayload:
        with self.lock:
            self._lock_mode('panic_reset', True)
            if self.worker is not None:
                raise BrokerError(503, 'worker_reap_failed', 'blocked')
            self.tripped, self.trip_reason = False, None
            self._record(OPERATOR, 'panic_reset', None, 'Panic reset; desktop remains locked', 'info')
            return self.status()

    def close(self):
        with self.lock:
            self._lock_mode('shutdown', True)

    def release(self, session_id: str, creature_id: str, agent_name: str):
        with self.lock:
            subjects = [lease.actor for lease in (self.lease,) if lease is not None]
            if self.inflight is not None:
                subjects.append(self.inflight[0])
            if any(a.session_id == session_id and a.agent_name == agent_name and
                   (not creature_id or a.creature_id == creature_id) for a in subjects):
                self._invalidate()
                if self.inflight is not None and self.pending is not None:
                    self._stop_worker('lease_released')

    def _gate(self, context: ToolContext | None, mutate: bool):
        self._idle()
        if self.tripped:
            raise BrokerError(409, 'panic_latched', 'blocked')
        if not self.supported:
            raise BrokerError(501, 'unsupported_platform', 'blocked')
        if context is not None:
            cancelled = context.runtime_services.get(DELEGATE_CANCEL)
            if cancelled is not None and cancelled.is_set():
                raise BrokerError(409, 'desktop_delegate_cancelled', 'blocked')
            actor = actor_for(context)
            if not _plugin_enabled(context.agent):
                raise BrokerError(403, 'computer_use_plugin_disabled', 'blocked')
            if self.policy.mode == 'locked' or (mutate and self.policy.mode == 'read_only'):
                raise BrokerError(403, f'desktop_{self.policy.mode}', 'blocked')
            if self.lease and self.lease.actor != actor:
                raise BrokerError(409, 'desktop_leased_to_another_creature', 'blocked')
            if mutate and not self.hotkey_registered:
                raise BrokerError(409, 'hotkey_unavailable', 'blocked')

    def remember_studio(self, server: tuple | None):
        if server and isinstance(server[1], int):
            with self.lock:
                self.studio_origins.update((scheme, host, server[1]) for scheme in ('http', 'https')
                                           for host in ('loopback', server[0]))

    def _check_root(self, root: WorkerRoot):
        if root.get('kind') == 'cdp' and cdp.blocked(root['url'], self.studio_origins):
            raise BrokerError(403, 'studio_target', 'blocked')
        if matches_patterns(root, self.policy.denylist_patterns):
            raise BrokerError(403, 'denylisted_target', 'blocked')
        if not root['exe_path']:
            raise BrokerError(403, 'unverified_window_identity', 'blocked')

    def _resolve_state(self, context: ToolContext, state_id: str, mutate: bool) -> Snapshot:
        self._gate(context, mutate)
        actor = actor_for(context)
        if not self.lease or self.lease.actor != actor or self.lease.state_id != state_id:
            raise BrokerError(409, 'state_not_in_active_lease', 'blocked')
        snapshot = self._find_state(state_id)
        self._check_root(snapshot.root)
        if mutate and snapshot.root['is_elevated']:
            raise BrokerError(403, 'elevated_target', 'blocked')
        return snapshot

    def _fresh(self, root: WorkerRoot, guard: Callable[[], None]) -> WorkerRoot:
        if root.get('kind') == 'cdp':
            pages, _ = self._discover(guard)
            current = next((page for page in pages if page['exe_path'] == root['exe_path'] and page['ws_url'] == root['ws_url']), None)
            if current is None or current['url'] != root['url']:
                raise BrokerError(409, 'cdp_page_changed', 'blocked')
            current = {**root, 'title': current['title']}
        else:
            current = self._rpc({'id': 0, 'op': 'identify', 'root_ref': root['root_ref'], 'hwnd': root['hwnd']}, guard)
        with self.lock:
            guard()
            self._check_root(current)
            if identity(current) != identity(root):
                raise BrokerError(409, 'window_identity_changed', 'blocked')
        return current

    def _update_roots(self, listed: list[WorkerRoot]) -> list[WorkerRoot]:
        known = {tuple(identity(root).values()): ref for ref, root in self.roots.items()}
        result = []
        for item in listed:
            ref = known.get(tuple(identity(item).values()))
            if ref is None:
                self.next_root += 1
                ref = f'@r{self.next_root}'
            result.append({**item, 'root_ref': ref, 'denylisted': matches_patterns(item, self.policy.denylist_patterns)})
        self.roots = {root['root_ref']: root for root in result}
        return result

    def _discover(self, guard: Callable[[], None]) -> tuple[list[WorkerRoot], list[int]]:
        with self.lock:
            guard()
            self.studio_origins.update(cdp.studio_origins())
            ports, blocked = self.policy.cdp_ports, self.studio_origins.copy()
        return cdp.discover_cdp_pages(ports, blocked)

    def _list(self, guard: Callable[[], None], include_cdp: bool) -> list[WorkerRoot]:
        listed = self._rpc({'id': 0, 'op': 'list_roots'}, guard)
        pages = self._discover(guard)[0] if include_cdp else []
        with self.lock:
            guard()
            if not include_cdp:
                pages = [root for root in self.roots.values() if root.get('kind') == 'cdp']
            updated = self._update_roots(listed + pages)
            return updated if include_cdp else updated[:len(listed)]

    def list_roots(self, context: ToolContext | None) -> list[RootItem]:
        def guard():
            self._gate(context, False)
            if generation != self.generation:
                raise BrokerError(409, 'desktop_policy_or_lease_changed', 'blocked')

        with self.calls:
            with self.lock:
                self._gate(context, False)
                generation = self.generation
            try:
                listed = self._list(guard, True)
                with self.lock:
                    guard()
                    if context is not None:
                        self._touch()
                    return [public_root(root) for root in listed]
            finally:
                with self.lock:
                    self._finish_locked_read()

    def _target(self, root_ref: str | None, hwnd: int | None, query: str | None,
                guard: Callable[[], None]) -> WorkerRoot:
        with self.lock:
            guard()
            if root_ref:
                root = self.roots.get(root_ref)
                if root is None or hwnd is not None and root['hwnd'] != hwnd:
                    raise BrokerError(409, 'unknown_or_mismatched_root_ref', 'blocked')
            else:
                root = None
        if root is None:
            roots = self._list(guard, False)
            if hwnd is None and query is not None:
                needle = query.casefold()
                root = max((item for item in roots if not item['denylisted'] and item['exe_path']
                            and any(needle in item[key].casefold() for key in ('title', 'exe_name'))),
                           key=window_rank, default=None)
                if root is None:
                    raise BrokerError(404, 'window_not_found', 'blocked')
            else:
                root = next((item for item in roots if item['hwnd'] == hwnd or (hwnd is None and item['is_foreground'])), None)
                if root is None:
                    raise BrokerError(404, 'no_matching_root', 'blocked')
        elif root.get('kind') == 'cdp':
            pages, _ = self._discover(guard)
            page = next((item for item in pages if item['ws_url'] == root['ws_url']), None)
            if page is None:
                raise BrokerError(409, 'cdp_page_unavailable', 'blocked')
            root = {**page, 'root_ref': root['root_ref']}
        with self.lock:
            guard()
            self._check_root(root)
        return root

    def _check_url(self, url: str):
        if cdp.origin(url) is None or any(ord(char) <= 32 or char == '\\' for char in url):
            raise BrokerError(422, 'cdp_requires_http_url', 'blocked')
        self.studio_origins.update(cdp.studio_origins())
        if cdp.blocked(url, self.studio_origins):
            raise BrokerError(403, 'studio_target', 'blocked')
        if any(re.search(pattern, url, re.IGNORECASE) for pattern in self.policy.denylist_patterns):
            raise BrokerError(403, 'denylisted_target', 'blocked')

    def _check_open_page(self, context: ToolContext, url: str):
        self._gate(context, True)
        self._check_url(url)
        if not self.policy.allow_foreground_input:
            raise BrokerError(403, 'foreground_input_disabled', 'blocked')

    def preview_url(self, context: ToolContext, url: str) -> tuple[bool, int]:
        with self.calls:
            with self.lock:
                self._gate(context, False)
                self._check_url(url)
                generation = self.generation

            def guard():
                self._gate(context, False)
                if generation != self.generation:
                    raise BrokerError(409, 'desktop_policy_or_lease_changed', 'blocked')

            pages, _ = self._discover(guard)
            with self.lock:
                guard()
                if cdp.matching_page(pages, url):
                    return False, generation
                self._check_open_page(context, url)
                return self.policy.mode == 'confirm', generation

    def approve_url(self, context: ToolContext, url: str, generation: int) -> str:
        with self.lock:
            self._check_open_page(context, url)
            if generation != self.generation:
                raise BrokerError(409, 'approval_context_changed', 'blocked')
            token = uuid4().hex
            now = time.monotonic()
            self.approvals = {key: existing for key, existing in self.approvals.items() if existing.deadline >= now}
            self.approvals[token] = Approval(actor_for(context), '', self.epoch, url, '', now + 15)
            return token

    def _url_target(self, context: ToolContext, url: str, token: str | None, guard: Callable[[], None]) -> WorkerRoot:
        with self.lock:
            guard()
            self._check_url(url)
            approval = self.approvals.pop(token, None) if token else None
        pages, listening = self._discover(guard)
        root = cdp.matching_page(pages, url)
        if root is None:
            def open_guard():
                with self.lock:
                    guard()
                    self._check_open_page(context, url)
                    if self.policy.mode == 'confirm' and not (approval and approval.actor == actor_for(context)
                            and not approval.state_id and approval.actions == url and approval.epoch == self.epoch
                            and approval.deadline >= time.monotonic()):
                        raise BrokerError(403, 'one_time_approval_required', 'blocked')

            with self.lock:
                open_guard()
                ports, blocked = self.policy.cdp_ports, self.studio_origins.copy()
            try:
                root = cdp.open_page(url, ports, listening, blocked, open_guard)
            except cdp.CDPError as exc:
                raise BrokerError(409, str(exc), 'blocked') from exc
            pages.append(root)
        with self.lock:
            guard()
            updated = self._update_roots([item for item in self.roots.values() if item.get('kind') != 'cdp'] + pages)
            root = next(item for item in updated if item['exe_path'] == root['exe_path'] and item['kind'] == 'cdp')
            self._check_root(root)
            return root

    def precheck(self, context: ToolContext, tool: str, args: dict):
        actor_for(context)
        with self.lock:
            self._gate(context, tool == 'cu_act')
            if tool == 'cu_query':
                self._resolve_state(context, args['state_id'], False)
            if tool == 'cu_observe' and (root := self.roots.get(args.get('root_ref'))) is not None:
                self._check_root(root)

    def approval_preview(self, context: ToolContext, state_id: str, actions: list[Action]) -> tuple[bool, str, int]:
        def guard():
            self._resolve_state(context, state_id, True)

        with self.calls:
            with self.lock:
                snapshot = self._resolve_state(context, state_id, True)
                self._check_refs(snapshot, actions)
            current = self._fresh(snapshot.root, guard)
            with self.lock:
                guard()
                required = self.policy.mode == 'confirm' or (
                    action_needs_confirmation(snapshot, actions) and not matches_patterns(current, self.policy.allowlist_patterns)
                )
                return required, current['title'], self.generation

    def confirmation_targets(self, context: ToolContext, state_id: str, actions: list[Action]) -> dict[str, str]:
        with self.lock:
            snapshot = self._resolve_state(context, state_id, True)
            refs = {action.get('ref') for action in actions}
            return {row['ref']: f'{row["role"]}: {row["name"]}' for row in snapshot.elements if row['ref'] in refs}

    def approve(self, context: ToolContext, state_id: str, actions: list[Action], generation: int, title: str) -> str:
        with self.lock:
            snapshot = self._resolve_state(context, state_id, True)
            if generation != self.generation:
                raise BrokerError(409, 'approval_context_changed', 'blocked')
            now = time.monotonic()
            self.approvals = {token: value for token, value in self.approvals.items() if value.deadline >= now}
            token = uuid4().hex
            self.approvals[token] = Approval(actor_for(context), state_id, snapshot.state['epoch'], action_key(actions), title, now + 15)
            return token

    def _find_state(self, state_id: str) -> Snapshot:
        snapshot = next((item for item in self.states if item.state['state_id'] == state_id), None)
        if snapshot is None:
            raise BrokerError(404, 'state_not_found', 'blocked')
        return snapshot

    def _state(self, snapshot: Snapshot) -> StateDetail:
        return {**deepcopy(snapshot.state), 'valid_for_act': bool(self.lease and self.lease.state_id == snapshot.state['state_id']
                and self.policy.mode in ('confirm', 'armed') and not self.tripped and self.hotkey_registered
                and not snapshot.root['is_elevated'] and not matches_patterns(snapshot.root, self.policy.denylist_patterns))}

    def _point(self, worker: WorkerSnapshot, report: ActionReport | None) -> ActionPoint | None:
        if not report or report['point'] is None or report['method'] is None:
            return None
        x, y = report['point']
        rect = worker['root']['rect']
        iw, ih = worker['image_width'], worker['image_height']
        in_rect = rect['x'] <= x < rect['x'] + rect['width'] and rect['y'] <= y < rect['y'] + rect['height']
        return {'x': x, 'y': y, 'image_x': (x - rect['x']) * iw // rect['width'] if iw and in_rect else None,
                'image_y': (y - rect['y']) * ih // rect['height'] if ih and in_rect else None,
                'action': report['action'], 'ref': report['ref'], 'outcome': report['outcome']}

    def _store(self, worker: WorkerSnapshot, max_elements: int, actor: Actor | None, report: ActionReport | None) -> Snapshot:
        root = worker['root']
        self._check_root(root)
        self.epoch += 1
        png = base64.b64decode(worker['png'], validate=True) if worker['png'] else None
        detail = {key: root[key] for key in ('root_ref', 'hwnd', 'pid', 'exe_name', 'title', 'dpi_scale', 'rect', 'kind', 'url') if key in root}
        detail.update({'state_id': uuid4().hex, 'epoch': self.epoch, 'created_at': time.time(), 'valid_for_act': False,
                       'element_count': len(worker['elements']), 'has_image': bool(png), 'image_width': worker['image_width'],
                       'image_height': worker['image_height'], 'capture_method': worker['capture_method'],
                       'last_action_point': self._point(worker, report),
                       'elements': [{key: value for key, value in item.items() if key != 'runtime_id'} for item in worker['elements']]})
        snapshot = Snapshot(detail, root, tuple(worker['elements']), png, max_elements)
        if self.lease and len(self.states) == self.states.maxlen and self.lease.state_id == self.states[-1].state['state_id']:
            self._invalidate()
        self.states.appendleft(snapshot)
        self.stats['observations'] += 1
        if actor is not None:
            self.approvals.clear()
            self.lease = Lease(actor, detail['state_id'], time.time(), root['hwnd'], root['title'])
            self._touch()
        self._record(actor.agent_name if actor else OPERATOR, 'observe', root['title'],
                     f'Observed {detail["element_count"]} elements', 'worked')
        return snapshot

    def _capture(self, root: WorkerRoot, include_image: bool, max_elements: int, restore: bool,
                 guard: Callable[[], None]) -> WorkerSnapshot:
        if root.get('kind') == 'cdp':
            def cdp_guard():
                with self.lock:
                    guard()

            def check(page):
                with self.lock:
                    self._check_root(page)

            try:
                return cdp.observe_cdp(root, include_image, max_elements, cdp_guard, check)
            except cdp.CDPError as exc:
                raise BrokerError(409, str(exc), 'blocked') from exc
        with self.lock:
            guard()
            request = {'id': 0, 'op': 'observe', 'identity': identity(root), 'root_ref': root['root_ref'],
                       'include_image': include_image, 'max_elements': max_elements, 'restore': restore,
                       'denylist_patterns': list(self.policy.denylist_patterns)}
        return self._rpc(request, guard)

    def observe(self, context: ToolContext | None, root_ref: str | None, hwnd: int | None,
                include_image: bool, max_elements: int, query: str | None, restore: bool) -> tuple[StateDetail, bytes | None]:
        return self._observe(context, root_ref, hwnd, include_image, max_elements, query, restore, None, None)

    def observe_url(self, context: ToolContext, url: str, include_image: bool, max_elements: int,
                    token: str | None) -> tuple[StateDetail, bytes | None]:
        return self._observe(context, None, None, include_image, max_elements, None, False, url, token)

    def _observe(self, context: ToolContext | None, root_ref: str | None, hwnd: int | None, include_image: bool,
                 max_elements: int, query: str | None, restore: bool, url: str | None,
                 token: str | None) -> tuple[StateDetail, bytes | None]:
        actor = actor_for(context) if context is not None else None
        label = actor.agent_name if actor else OPERATOR
        with self.calls:
            with self.lock:
                generation = self.generation
                if actor:
                    self.inflight = (actor, 'observe')

            def guard():
                self._gate(context, False)
                if generation != self.generation:
                    raise BrokerError(409, 'desktop_policy_or_lease_changed', 'blocked')

            try:
                root = self._url_target(context, url, token, guard) if url is not None else self._target(root_ref, hwnd, query, guard)
                result = self._capture(root, include_image, max_elements, restore, guard)
                with self.lock:
                    guard()
                    snapshot = self._store(result, max_elements, actor, None)
                    return self._state(snapshot), snapshot.png
            except BrokerError as exc:
                self.record_block(label, str(exc))
                raise
            finally:
                with self.lock:
                    self.inflight = None
                    self._finish_locked_read()

    def query(self, context: ToolContext, state_id: str) -> StateDetail:
        def guard():
            self._resolve_state(context, state_id, False)

        with self.calls:
            with self.lock:
                snapshot = self._resolve_state(context, state_id, False)
            self._fresh(snapshot.root, guard)
            with self.lock:
                guard()
                self.lease = replace(self.lease, updated_at=time.time())
                self._touch()
                return self._state(snapshot)

    def _check_refs(self, snapshot: Snapshot, actions: list[Action]):
        refs = {element['ref'] for element in snapshot.elements}
        for action in actions:
            if action.get('ref') and action['ref'] not in refs:
                raise BrokerError(422, 'element_ref_not_in_state', 'blocked')

    def act(self, context: ToolContext, state_id: str, actions: list[Action], token: str | None) -> ActPayload:
        actor = actor_for(context)
        with self.calls:
            with self.lock:
                generation = self.generation
                self.inflight = (actor, 'act')
                approval = self.approvals.pop(token, None) if token else None

            def guard():
                if generation != self.generation:
                    raise BrokerError(409, 'desktop_policy_or_lease_changed', 'blocked')
                self._resolve_state(context, state_id, True)

            root = None
            try:
                with self.lock:
                    snapshot = self._resolve_state(context, state_id, True)
                    self._check_refs(snapshot, actions)
                    root = snapshot.root
                current = self._fresh(root, guard)

                def has_approval() -> bool:
                    return bool(approval and approval.deadline >= time.monotonic()
                                and approval.actor == actor and approval.state_id == state_id
                                and approval.epoch == snapshot.state['epoch'] and approval.actions == action_key(actions)
                                and approval.title == current['title'])

                def dispatch_guard():
                    guard()
                    if current['is_elevated']:
                        raise BrokerError(403, 'elevated_target', 'blocked')
                    must_confirm = self.policy.mode == 'confirm' or (
                        action_needs_confirmation(snapshot, actions)
                        and not matches_patterns(current, self.policy.allowlist_patterns)
                    )
                    if must_confirm and not has_approval():
                        raise BrokerError(403, 'one_time_approval_required', 'blocked')

                with self.lock:
                    dispatch_guard()
                    request = {'id': 0, 'op': 'act', 'identity': identity(current), 'root_ref': root['root_ref'],
                               'actions': actions, 'elements': list(snapshot.elements), 'max_elements': snapshot.max_elements,
                               'foreground_rect': root['rect'], 'foreground_dpi': root['dpi_scale'],
                               'allow_foreground_input': self.policy.allow_foreground_input,
                               'denylist_patterns': list(self.policy.denylist_patterns),
                               'allowlist_patterns': list(self.policy.allowlist_patterns),
                               'approved_title': current['title'] if has_approval() else None}
                if current.get('kind') == 'cdp':
                    approved, patterns = has_approval(), self.policy.allowlist_patterns

                    def cdp_guard():
                        with self.lock:
                            try:
                                guard()
                            except BrokerError as exc:
                                raise cdp.CDPError(str(exc)) from exc

                    def check(page):
                        with self.lock:
                            try:
                                guard()
                                self._check_root(page)
                            except BrokerError as exc:
                                raise cdp.CDPError(str(exc)) from exc

                    def authorize(targets):
                        if any(not enabled for _, _, enabled in targets):
                            raise cdp.CDPError('cdp_element_disabled')
                        if not approved and not matches_patterns(current, patterns) and any(
                                destructive_control(role, name) for role, name, _ in targets):
                            raise cdp.CDPError('one_time_approval_required')

                    try:
                        results = cdp.act_cdp(current, snapshot.elements, actions, cdp_guard, check, authorize)
                    except cdp.CDPError as exc:
                        raise BrokerError(409, str(exc), 'unknown') from exc
                else:
                    results = self._rpc(request, dispatch_guard)
            except BrokerError as exc:
                with self.lock:
                    if exc.outcome == 'unknown':
                        self.stats['actions'] += 1
                        if generation == self.generation:
                            self._invalidate()
                    self._record(actor.agent_name, 'act' if exc.outcome == 'unknown' else 'blocked',
                                 root['title'] if root else None, str(exc), exc.outcome)
                    self.inflight = None
                if exc.outcome == 'unknown':
                    return {'results': [{'action': action['action'], 'ref': action.get('ref'), 'method': None,
                                         'outcome': 'unknown', 'reason': str(exc), 'point': None} for action in actions],
                            'post_state': None}
                raise
            with self.lock:
                last_dispatched = None
                for report in results:
                    blocked = report['outcome'] == 'blocked'
                    if not blocked:
                        self.stats['actions'] += 1
                        last_dispatched = report
                    self._record(actor.agent_name, 'blocked' if blocked else 'act', root['title'],
                                 report['action'] + (f': {report["reason"]}' if report['reason'] else ''), report['outcome'])
                if generation == self.generation and self.lease and self.lease.actor == actor:
                    self.approvals.clear()
                    self.lease = Lease(actor, None, time.time(), current['hwnd'], current['title'])
                    self._touch()

            def post_guard():
                self._gate(context, False)
                if generation != self.generation or not self.lease or self.lease.actor != actor:
                    raise BrokerError(409, 'desktop_policy_or_lease_changed', 'blocked')

            try:
                result = self._capture(current, bool(snapshot.png), snapshot.max_elements, False, post_guard)
                with self.lock:
                    post_guard()
                    post = self._state(self._store(result, snapshot.max_elements, actor, last_dispatched))
            except BrokerError as exc:
                self.record_block(actor.agent_name, f'post_state: {exc}')
                post = None
            finally:
                with self.lock:
                    self.inflight = None
            return {'results': results, 'post_state': post, 'before_state': snapshot.state}

    def state_index(self) -> StateIndex:
        with self.lock:
            return {'states': [{key: value for key, value in self._state(snapshot).items() if key != 'elements'}
                               for snapshot in self.states], 'audit': deepcopy(list(self.audit))}

    def state(self, state_id: str) -> StateDetail:
        with self.lock:
            return self._state(self._find_state(state_id))

    def frame(self, state_id: str) -> bytes:
        with self.lock:
            snapshot = self._find_state(state_id)
            if not snapshot.png:
                raise BrokerError(404, 'frame_not_found', 'blocked')
            return snapshot.png


BROKER = DesktopBroker()
atexit.register(BROKER.close)
