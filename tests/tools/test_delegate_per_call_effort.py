"""Trusted per-work-unit routing must not inherit unrelated parent/global effort."""
from types import SimpleNamespace

import pytest

from tools.delegate_tool_config import _resolve_child_runtime


@pytest.mark.parametrize('effort,expected', [
    ('medium', {'enabled': True, 'effort': 'medium'}),
    (False, {'enabled': False}),
    ('', {'enabled': True, 'effort': 'low'}),
    (None, {'enabled': True, 'effort': 'low'}),
])
def test_per_call_effort_owns_child_runtime_without_parent_mutation(effort, expected):
    parent = SimpleNamespace(model='parent-fixture', provider='fixture', base_url='http://127.0.0.1:1',
                             reasoning_config={'enabled': True, 'effort': 'high'})
    result = _resolve_child_runtime(
        parent, {'reasoning_effort': 'low'}, None, model='child-fixture',
        override_provider='fixture', override_base_url=None, override_api_key=None,
        override_api_mode='chat_completions', override_acp_command=None, override_acp_args=None,
        routing_cfg={'reasoning_effort': effort, 'fallback_model': []},
    )
    assert result['model'] == 'child-fixture'
    assert result['reasoning_config'] == expected
    assert parent.reasoning_config == {'enabled': True, 'effort': 'high'}
