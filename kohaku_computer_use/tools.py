"""KohakuTerrarium tools for a state-scoped desktop lease."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from copy import deepcopy
from typing import get_args

from jsonschema import Draft202012Validator
from kohakuterrarium.llm.message import ImagePart, TextPart
from kohakuterrarium.modules.tool.base import (
    BaseTool,
    ExecutionMode,
    ToolContext,
    ToolResult,
)
from kohakuterrarium.modules.tool.media_policy import MediaPolicy

from .broker import BROKER, BrokerError, actor_for
from .models import ActionName, StateDetail, parse_chord


def schema(properties: dict, required: list[str]) -> dict:
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


def string(limit: int, nonempty: bool) -> dict:
    return {'type': 'string', 'maxLength': limit, 'minLength': int(nonempty)}


def integer(minimum: int, maximum: int) -> dict:
    return {'type': 'integer', 'minimum': minimum, 'maximum': maximum}


BOOL = {'type': 'boolean'}
STATE_ID = string(64, True)
REF = {'type': 'string', 'pattern': '^@e[1-9][0-9]*$', 'maxLength': 12}
EXPECT = schema({'text': string(240, True), 'role': string(100, True), 'gone': BOOL, 'timeout_ms': integer(0, 5000)}, [])
ACTION = schema({
    'action': {'enum': list(get_args(ActionName))}, 'ref': REF,
    'x': integer(-2**31, 2**31 - 1), 'y': integer(-2**31, 2**31 - 1),
    'text': string(4000, False), 'key': string(40, True), 'clear_first': BOOL,
    'direction': {'enum': ['up', 'down', 'left', 'right']}, 'amount': integer(1, 10), 'expect': EXPECT,
}, ['action'])
SCHEMAS = {
    'cu_roots': schema({}, []),
    'cu_observe': schema({'root_ref': {'type': ['string', 'null'], 'pattern': '^@r[1-9][0-9]*$', 'maxLength': 16},
                          'hwnd': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 2**63 - 1},
                          'include_image': BOOL, 'max_elements': integer(1, 512)}, []),
    'cu_query': schema({'state_id': STATE_ID, 'ref': REF, 'text': string(240, True), 'role': string(100, True),
                        'limit': integer(1, 512)}, ['state_id']),
    'cu_act': schema({'state_id': STATE_ID, 'actions': {'type': 'array', 'items': ACTION, 'minItems': 1, 'maxItems': 8}},
                     ['state_id', 'actions']),
}
VALIDATORS = {name: Draft202012Validator(value) for name, value in SCHEMAS.items()}
DESCRIPTIONS = {
    'cu_roots': 'List visible Windows desktop roots and their state-independent window references.',
    'cu_observe': 'Observe a window and acquire a creature lease with state-scoped UI Automation references.',
    'cu_query': 'Filter semantic elements in the current leased snapshot without taking a new screenshot.',
    'cu_act': 'Act on the current leased snapshot and return outcomes and a fresh state; never retry an unknown outcome blindly.',
}
DOCS = {
    'cu_roots': 'Takes no arguments. Use a returned @rN with cu_observe; denylisted roots cannot be inspected.',
    'cu_observe': 'Pass root_ref or hwnd; omitting both selects the foreground root. max_elements defaults to 150; '
                  'include_image defaults to false. Read the state_id, elements and valid_for_act from the result.',
    'cu_query': 'Pass state_id. Filter by ref, exact role (case-insensitive) or text substring; optionally pass limit.',
    'cu_act': 'Pass state_id and actions (1–8 objects, each with action). Prefer @eN ref from that exact state. '
              'invoke, set_text, toggle, select, expand and collapse require ref. scroll takes direction and optional amount. '
              'click, double_click and right_click take ref or absolute screen x/y. type_text takes text and optional '
              'ref and clear_first; set_text takes text; key takes a chord such as Ctrl+A. '
              'An optional expect checks text and/or role, with gone and timeout_ms (at most 5000). '
              'After any attempt use post_state or re-observe; unknown does not imply no effect.',
}
VISION_EXCLUDED = re.compile(r'(?:^|[/.:])deepseek-(?:chat|reasoner)(?:$|[:@])', re.IGNORECASE)


def validate_args(tool: str, args: dict) -> dict:
    error = next(VALIDATORS[tool].iter_errors(args), None)
    if error is not None:
        path = '.'.join(str(part) for part in error.absolute_path) or 'arguments'
        raise BrokerError(422, f'invalid {path} ({error.validator})', 'blocked')
    if tool != 'cu_act':
        for field in ('hwnd', 'max_elements', 'limit'):
            value = args.get(field)
            if value is not None and not isinstance(value, int):
                raise BrokerError(422, f'{field}_must_be_integer', 'blocked')
        return args
    for item in args['actions']:
        name = item['action']
        coords = 'x' in item or 'y' in item
        if coords and ('ref' in item or not {'x', 'y'} <= item.keys() or name not in ('click', 'double_click', 'right_click')):
            raise BrokerError(422, 'click_coordinates_must_be_a_pair_without_ref', 'blocked')
        if name in ('invoke', 'set_text', 'toggle', 'select', 'expand', 'collapse') and 'ref' not in item:
            raise BrokerError(422, 'semantic_action_requires_ref', 'blocked')
        if name in ('click', 'double_click', 'right_click') and not (coords or 'ref' in item):
            raise BrokerError(422, 'click_requires_ref_or_coordinates', 'blocked')
        for field, permitted in [('text', ('set_text', 'type_text')), ('key', ('key',)), ('direction', ('scroll',))]:
            if (name in permitted) != (field in item):
                raise BrokerError(422, f'invalid_{field}_for_action', 'blocked')
        if ('amount' in item and name != 'scroll') or ('clear_first' in item and name not in ('set_text', 'type_text')):
            raise BrokerError(422, 'action_argument_not_applicable', 'blocked')
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
        if 'expect' in item and not ({'text', 'role'} & item['expect'].keys()):
            raise BrokerError(422, 'expect_requires_text_or_role', 'blocked')
        # The JSON Schema integer type also accepts numerically integral floats.
        for field in ('x', 'y', 'amount'):
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


def accepts_images(context: ToolContext) -> bool:
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
        super().__init__()
        self._tool_name = name

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return DESCRIPTIONS[self.tool_name]

    def get_parameters_schema(self) -> dict:
        return deepcopy(SCHEMAS[self.tool_name])

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
            return await asyncio.to_thread(self._run, validated, context, token)
        except BrokerError as exc:
            return ToolResult(error=str(exc))

    def _run(self, args: dict, context: ToolContext, token: str | None) -> ToolResult:
        if self.tool_name == 'cu_roots':
            return ToolResult(output=serialize({'roots': BROKER.list_roots(context)}))
        if self.tool_name == 'cu_observe':
            include_image = args.get('include_image', False)
            state, png = BROKER.observe(context, args.get('root_ref'), args.get('hwnd'),
                                        include_image, int(args.get('max_elements', 150)))
            text = serialize(state)
            if include_image and png and accepts_images(context):
                encoded = base64.b64encode(png).decode('ascii')
                return ToolResult(output=[TextPart(text), ImagePart(f'data:image/png;base64,{encoded}',
                                  source_type='frame', source_name=state['state_id'])],
                                  metadata={'media_policy': MediaPolicy(persist=True, pinned=False, inline_fallback=True)})
            return ToolResult(output=text)
        if self.tool_name == 'cu_query':
            state = BROKER.query(context, args['state_id'])
            return ToolResult(output=serialize(query_view(state, args)))
        return ToolResult(output=serialize(BROKER.act(context, args['state_id'], args['actions'], token)))
