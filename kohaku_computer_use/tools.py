"""KohakuTerrarium tools for a state-scoped desktop lease."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from copy import deepcopy
from typing import cast, get_args

from jsonschema import Draft202012Validator
from kohakuterrarium.llm.base import ToolSchema
from kohakuterrarium.llm.message import ImagePart, TextPart
from kohakuterrarium.modules.tool.base import (
    BaseTool,
    ExecutionMode,
    ToolConfig,
    ToolContext,
    ToolResult,
)
from kohakuterrarium.modules.tool.media_policy import MediaPolicy

from .broker import BROKER, BrokerError, actor_for
from .delegate import DELEGATE_MODEL, run_delegate
from .formatters import action_text, outline, roots_text
from .models import ActionName, StateDetail, parse_chord


def schema(properties: dict, required: list[str]) -> dict:
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


def string(limit: int, nonempty: bool) -> dict:
    return {'type': 'string', 'maxLength': limit, 'minLength': int(nonempty)}


def integer(minimum: int, maximum: int) -> dict:
    return {'type': 'integer', 'minimum': minimum, 'maximum': maximum}


BOOL = {'type': 'boolean'}
STATE_ID = string(64, True)
REF = {
    'type': 'string', 'pattern': r'^@e[1-9][0-9]*\Z', 'maxLength': 12,
    'description': 'Element reference like @e1 from the current state.',
}
EXPECT = schema({'text': string(240, True), 'role': string(100, True), 'gone': BOOL, 'timeout_ms': integer(0, 5000)}, [])
ACTION = schema({
    'action': {'type': 'string', 'enum': list(get_args(ActionName))}, 'ref': REF,
    'x': integer(-2**31, 2**31 - 1), 'y': integer(-2**31, 2**31 - 1),
    'text': string(4000, False),
    'key': {**string(40, True), 'description': 'Key chord like Enter or Ctrl+S.'},
    'clear_first': BOOL, 'direction': {'type': 'string', 'enum': ['up', 'down', 'left', 'right']},
    'amount': integer(1, 10), 'expect': EXPECT,
    'wait_ms': {**integer(0, 10000), 'description': 'Optional pause after this action in milliseconds.'},
}, ['action'])
STRICT_SCHEMAS = {
    'cu_roots': schema({}, []),
    'cu_observe': schema({
        'root_ref': {'type': 'string', 'pattern': r'^@r[1-9][0-9]*\Z', 'maxLength': 16,
                     'description': 'Window reference like @r1 from cu_roots.'},
        'hwnd': {**integer(1, 2**63 - 1), 'description': 'Numeric window handle from cu_roots.'},
        'query': {**string(240, True), 'description': 'Window title or executable substring, like QQ or Notepad.'},
        'url': {**string(2048, True), 'description': 'HTTP(S) browser page URL prefix.'},
        'restore': {**BOOL, 'description': 'Restore the window if minimized (defaults to true).'},
        'include_image': BOOL, 'max_elements': integer(1, 512),
    }, []),
    'cu_query': schema({'state_id': STATE_ID, 'ref': REF, 'text': string(240, True), 'role': string(100, True),
                        'limit': integer(1, 512)}, ['state_id']),
    'cu_act': schema({'state_id': STATE_ID, 'actions': {'type': 'array', 'items': ACTION, 'minItems': 1, 'maxItems': 8}},
                     ['state_id', 'actions']),
    'cu_delegate': schema({'task': {**string(2000, True), 'description': 'Bounded GUI task and expected result.'},
                           'max_steps': {**integer(1, 12), 'description': 'Maximum turns (defaults to 6).'}}, ['task']),
}
PROVIDER_SCHEMA_KEYS = frozenset({'type', 'properties', 'required', 'items', 'enum', 'description'})


def provider_schema(node: dict) -> dict:
    result = {key: deepcopy(value) for key, value in node.items()
              if key in PROVIDER_SCHEMA_KEYS and key not in ('properties', 'items')}
    if 'properties' in node:
        result['properties'] = {key: provider_schema(value) for key, value in node['properties'].items()}
    if 'items' in node:
        result['items'] = provider_schema(node['items'])
    return result


TOOL_SCHEMAS = {name: provider_schema(value) for name, value in STRICT_SCHEMAS.items()}
VALIDATORS = {name: Draft202012Validator(value) for name, value in STRICT_SCHEMAS.items()}
DESCRIPTIONS = {
    'cu_roots': 'List desktop windows and local CDP browser pages with stable @rN references.',
    'cu_observe': 'Observe a window or CDP page and acquire a lease with state-scoped element references.',
    'cu_query': 'Filter semantic elements in the current leased snapshot without taking a new screenshot.',
    'cu_act': 'Act on the current leased snapshot and return outcomes and a fresh state; never retry an unknown outcome blindly.',
    'cu_delegate': 'Run a bounded GUI task in a fresh desktop-only sub-agent, preserving desktop permissions and approval prompts.',
}
DOCS = {
    'cu_roots': 'Takes no arguments. Use a returned @rN with cu_observe; denylisted roots cannot be inspected.',
    'cu_observe': 'Pass root_ref, hwnd, query (window title or executable substring), or url. With none, use the foreground. '
                  'restore defaults to true for windows. Screenshots are opt-in except for sparse Windows UIA trees. '
                  'A new URL may open a tab and require confirmation. Use the returned state and @eN references.',
    'cu_query': 'Pass state_id. Filter by ref, exact role (case-insensitive) or text substring; optionally pass limit.',
    'cu_act': 'Pass state_id and actions (1–8 objects, each with action). Prefer @eN ref from that exact state. '
              'invoke, set_text, toggle, select, expand and collapse require ref. scroll takes direction, optional amount, and optionally a ref or x/y. '
              'click, double_click and right_click take ref or x/y (desktop pixels for windows; viewport CSS pixels for CDP). '
              'CDP supports click, invoke, double_click, right_click, set_text, type_text, key and scroll. type_text takes text and optional '
              'ref and clear_first; set_text takes text; key takes a chord such as Ctrl+A. '
              'An optional expect checks text and/or role, with gone and timeout_ms (at most 5000). '
              'Use wait_ms (0–10000) to override the brief pause after a click or invoke. '
              'After any attempt use the successor state in the header or re-observe; unknown does not imply no effect.',
    'cu_delegate': 'Pass a self-contained task and optional max_steps (1–12, default 6). The sub-agent sees no parent '
                   'history and the same desktop mode, target restrictions and approvals apply.',
}
VISION_EXCLUDED = re.compile(r'(?:^|[/.:])deepseek-(?:chat|reasoner)(?:$|[:@])', re.IGNORECASE)


def omit_optional_nulls(value: object, node: dict) -> object:
    if isinstance(value, dict) and node['type'] == 'object':
        properties = node['properties']
        required = node['required']
        return {
            key: omit_optional_nulls(item, properties[key]) if key in properties else item
            for key, item in value.items()
            if item is not None or key not in properties or key in required
        }
    if isinstance(value, list) and node['type'] == 'array':
        return [omit_optional_nulls(item, node['items']) for item in value]
    return value


REF_ACTIONS = {'invoke', 'set_text', 'toggle', 'select', 'expand', 'collapse'}
TEXT_ACTIONS = {'set_text', 'type_text'}
CLICK_ACTIONS = {'click', 'double_click', 'right_click'}
ACTION_FIELDS = {
    **{name: {'ref'} for name in REF_ACTIONS},
    **{name: {'ref', 'text', 'clear_first'} for name in TEXT_ACTIONS},
    **{name: {'ref', 'x', 'y'} for name in CLICK_ACTIONS},
    'key': {'key'},
    'scroll': {'ref', 'x', 'y', 'direction', 'amount'},
}


def blank(value: object) -> bool:
    return isinstance(value, str) and not value.strip()


def normalize_action(item: dict) -> dict:
    name = item.get('action')
    if not isinstance(name, str) or name not in ACTION_FIELDS:
        return item
    active = ACTION_FIELDS[name] | {'action', 'expect', 'wait_ms'}
    # Keep unknown fields so the strict schema can still flag them.
    item = {key: value for key, value in item.items() if key in active or key not in ACTION['properties']}
    if blank(item.get('ref')):
        item.pop('ref')
    if 'ref' in item and (name in CLICK_ACTIONS or name == 'scroll'):
        item.pop('x', None)
        item.pop('y', None)
    if name == 'scroll' and type(item.get('amount')) is int and item['amount'] < 1:
        item.pop('amount')
    if isinstance(expect := item.get('expect'), dict):
        expect = {key: value for key, value in expect.items() if key not in ('text', 'role') or not blank(value)}
        if {'text', 'role'} & expect.keys() or expect.keys() - EXPECT['properties'].keys():
            item['expect'] = expect
        else:
            item.pop('expect')
    return item


def normalize_placeholders(tool: str, args: dict) -> dict:
    strings, number = {
        'cu_observe': (('root_ref', 'query', 'url'), 'max_elements'),
        'cu_query': (('ref', 'text', 'role'), 'limit'),
        'cu_delegate': ((), 'max_steps'),
    }.get(tool, ((), ''))
    for key in strings:
        if blank(args.get(key)):
            args.pop(key)
    if number and type(args.get(number)) is int and args[number] == 0:
        args.pop(number)
    if (tool == 'cu_observe' and type(hwnd := args.get('hwnd')) is int
            and (hwnd == 0 or (hwnd == 1 and any(args.get(key) for key in strings)))):
        args.pop('hwnd')
    if tool == 'cu_act' and isinstance(actions := args.get('actions'), list):
        args['actions'] = [normalize_action(item) if isinstance(item, dict) else item for item in actions]
    return args


def validate_args(tool: str, args: dict) -> dict:
    # KT SubAgent retains this metadata on the original call to pair its result.
    clean_input = {key: value for key, value in args.items() if key != '_tool_call_id'} if isinstance(args, dict) else args
    normalized = omit_optional_nulls(clean_input, STRICT_SCHEMAS[tool])
    if isinstance(normalized, dict):
        normalized = normalize_placeholders(tool, normalized)
    error = next(VALIDATORS[tool].iter_errors(normalized), None)
    if error is not None:
        path = '.'.join(str(part) for part in error.absolute_path) or 'arguments'
        raise BrokerError(422, f'invalid {path} ({error.validator})', 'blocked')
    args = cast(dict, normalized)
    if tool == 'cu_observe' and 'url' in args and any(key in args for key in ('root_ref', 'hwnd', 'query')):
        raise BrokerError(422, 'url_cannot_be_combined_with_other_target_selectors', 'blocked')
    if tool != 'cu_act':
        for field in ('hwnd', 'max_elements', 'limit', 'max_steps'):
            value = args.get(field)
            if value is not None and not isinstance(value, int):
                raise BrokerError(422, f'{field}_must_be_integer', 'blocked')
        return args
    for item in args['actions']:
        name = item['action']
        coords = 'x' in item or 'y' in item
        if coords and not {'x', 'y'} <= item.keys():
            raise BrokerError(422, 'coordinates_must_be_a_pair', 'blocked')
        if name in REF_ACTIONS and 'ref' not in item:
            raise BrokerError(422, 'semantic_action_requires_ref', 'blocked')
        if name in CLICK_ACTIONS and not (coords or 'ref' in item):
            raise BrokerError(422, 'click_requires_ref_or_coordinates', 'blocked')
        for field, required_by in [('text', TEXT_ACTIONS), ('key', {'key'}), ('direction', {'scroll'})]:
            if name in required_by and field not in item:
                raise BrokerError(422, f'invalid_{field}_for_action', 'blocked')
        if 'text' in item:
            try:
                item['text'].encode('utf-16-le')
            except UnicodeEncodeError as exc:
                raise BrokerError(422, 'text_contains_invalid_unicode', 'blocked') from exc
        if 'key' in item:
            try:
                parse_chord(item['key'], False)
            except ValueError as exc:
                raise BrokerError(422, 'invalid_key_chord', 'blocked') from exc
        if 'expect' in item:
            expect = item['expect']
            if 'timeout_ms' in expect and not isinstance(expect['timeout_ms'], int):
                raise BrokerError(422, 'timeout_ms_must_be_integer', 'blocked')
        # The JSON Schema integer type also accepts numerically integral floats.
        for field in ('x', 'y', 'amount', 'wait_ms'):
            if field in item and not isinstance(item[field], int):
                raise BrokerError(422, f'{field}_must_be_integer', 'blocked')
    return args


def query_view(state: StateDetail, args: dict) -> StateDetail:
    query, role, ref = args.get('text', '').casefold(), args.get('role', '').casefold(), args.get('ref')
    elements = [item for item in state['elements'] if (not role or item['role'].casefold() == role)
                and (not ref or item['ref'] == ref) and (not query or any(query in item[key].casefold()
                for key in ('name', 'automation_id', 'value_preview')))]
    return {**state, 'elements': elements[:int(args.get('limit', 150))]}


def serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'))


def image_result(text: str, png: bytes, state_id: str) -> ToolResult:
    encoded = base64.b64encode(png).decode('ascii')
    return ToolResult(output=[TextPart(text), ImagePart(f'data:image/png;base64,{encoded}',
                      source_type='frame', source_name=state_id)],
                      metadata={'media_policy': MediaPolicy(persist=True, pinned=False, inline_fallback=True)})


def accepts_images(context: ToolContext) -> bool:
    if DELEGATE_MODEL in context.runtime_services:
        return not VISION_EXCLUDED.search(context.runtime_services[DELEGATE_MODEL])
    agent = context.agent
    llm_model = getattr(getattr(agent, 'llm', None), 'model', '')
    config_model = getattr(getattr(agent, 'config', None), 'model', '')
    return not any(VISION_EXCLUDED.search(str(model)) for model in (llm_model, config_model) if model)


class ComputerUseTool(BaseTool):
    needs_context = True
    is_concurrency_safe = False
    request_replay = 'forbid'
    execution_mode = ExecutionMode.DIRECT

    def __init__(self, name: str):
        super().__init__(ToolConfig(timeout=120.0) if name == 'cu_act' else
                         ToolConfig(timeout=125.0) if name == 'cu_delegate' else None)
        self._tool_name = name

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return DESCRIPTIONS[self.tool_name]

    @property
    def parameters(self) -> dict:
        return deepcopy(TOOL_SCHEMAS[self.tool_name])

    def get_parameters_schema(self) -> dict:
        return self.parameters

    def to_schema(self) -> ToolSchema:
        return ToolSchema(name=self.tool_name, description=self.description, parameters=self.parameters)

    def get_full_documentation(self, tool_format: str = 'native') -> str:
        return f'# {self.tool_name}\n\n{self.description}\n\n{DOCS[self.tool_name]}'

    async def _execute(self, args: dict, context: ToolContext | None) -> ToolResult:
        try:
            actor_for(context)
            values = dict(args)
            token = values.pop('_approval_token', None)
            if token is not None and not isinstance(token, str):
                raise BrokerError(422, 'invalid_approval_token', 'blocked')
            validated = validate_args(self.tool_name, values)
            if self.tool_name == 'cu_delegate':
                return await run_delegate(BROKER, context, validated['task'], validated.get('max_steps', 6))
            return await asyncio.to_thread(self._run, validated, context, token)
        except BrokerError as exc:
            return ToolResult(error=str(exc))

    def _run(self, args: dict, context: ToolContext, token: str | None) -> ToolResult:
        if self.tool_name == 'cu_roots':
            return ToolResult(output=roots_text(BROKER.list_roots(context)))
        if self.tool_name == 'cu_observe':
            include_image = args.get('include_image', False)
            limit = args.get('max_elements', 150)
            if url := args.get('url'):
                state, png = BROKER.observe_url(context, url, include_image, limit, token)
            else:
                state, png = BROKER.observe(context, args.get('root_ref'), args.get('hwnd'),
                                            include_image, limit, args.get('query'), args.get('restore', True))
            text = outline(state)
            if png and accepts_images(context):
                return image_result(text, png, state['state_id'])
            return ToolResult(output=text)
        if self.tool_name == 'cu_query':
            state = BROKER.query(context, args['state_id'])
            return ToolResult(output=outline(query_view(state, args)))
        result = BROKER.act(context, args['state_id'], args['actions'], token)
        text, post = action_text(result), result['post_state']
        if DELEGATE_MODEL in context.runtime_services and post and post['has_image'] and accepts_images(context):
            try:
                return image_result(text, BROKER.frame(post['state_id']), post['state_id'])
            except BrokerError as exc:
                if exc.status != 404:
                    raise
                return ToolResult(output=f'{text}\nThe successor frame is no longer available.')
        return ToolResult(output=text)
