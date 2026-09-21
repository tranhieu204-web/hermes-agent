"""No provider traffic: command and receipt tests use a fake runner."""
import copy
import hashlib
import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.economy_route_adapter as adapter_module
from tools.economy_route_adapter import execute


def envelope(tmp_path, adapter):
    seat_id = 'embermind' if adapter == 'hermes-agent' else 'warlord'
    route = {'provider':'OpenAI' if adapter == 'codex-cli' else 'Local',
             'model':'gpt-5.6-sol' if adapter == 'codex-cli' else 'qwen-fixture',
             'effort':'medium'}
    billing_mode = 'local' if adapter == 'hermes-agent' else 'subscription_included'
    value = {'selection':{'selected_seat_id':seat_id,'effective_route':route},
             'candidate':{'seat_id':seat_id,'execution_evidence':{
                 'route':dict(route),'invocation':{'adapter':adapter},
                 'billing':{'mode':billing_mode,'authorized':True,'no_overage':True}}},
             'execution':{}, 'purpose':'worker', 'simulation':False,
             'tracking':{'project_id':'fixture:project','build_id':'fixture:build'},
             'announcement_required':True, 'source_generation':'fixture-generation',
             'source_hashes':{'fixture.txt':'0'*64},
             'task_packet':{'schema_version':1,'goal':'inspect fixture','non_goals':['network'],
                            'context_refs':[{'path':'fixture.txt','sha256':'0'*64}]},
             'allowed_paths':[], 'allowed_tools':['read_file'],
             'privacy':'local_only' if adapter == 'hermes-agent' else 'cloud_allowed',
             'billing_modes':['local'] if adapter == 'hermes-agent' else ['subscription_included'],
             'context_tokens':1024, 'output_tokens':256,
             'acceptance_ref':'fixture:acceptance','checkpoint_ref':'fixture:checkpoint',
             'host_event_nonce':'a'*32, 'project_root':str(tmp_path.resolve()).lower(),
             'task_id':'fixture-task','attempt_id':'one'}
    value['envelope_sha256']=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return value


def host_artifacts(tmp_path):
    return tmp_path.parent / f"{tmp_path.name}-host-artifacts"


def test_codex_adapter_pins_route_scope_and_emits_bound_receipt(tmp_path):
    env=envelope(tmp_path,'codex-cli'); seen={}
    def runner(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return SimpleNamespace(returncode=0,stdout=json.dumps(
            {'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),stderr='')
    receipt=execute(env,'token-1',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert seen['args'][:2] == ['codex','exec']
    assert {'--ephemeral','--ignore-user-config','--ignore-rules'} <= set(seen['args'])
    assert ['-m','gpt-5.6-sol'] == seen['args'][seen['args'].index('-m'):seen['args'].index('-m')+2]
    assert 'model_reasoning_effort="medium"' in seen['args']
    assert seen['kwargs']['cwd'] == env['project_root']
    assert receipt['token']=='token-1' and receipt['notice_ref'].startswith('notice:sha256:')
    assert receipt['actual_route']==env['selection']['effective_route']
    assert receipt['model_evidence_level']=='configured_only'
    assert receipt['provider_model']=='UNKNOWN'
    assert receipt['attempt_quiesced'] is True
    assert receipt['usage']=={'input_tokens':1,'output_tokens':1}
    assert receipt['effort_evidence_level']=='configured_only'
    assert receipt['provider_effort']=='UNKNOWN'


def test_local_qwen_uses_hermes_tool_agent_not_raw_http(tmp_path):
    env=envelope(tmp_path,'hermes-agent'); seen={}
    def runner(args, **kwargs):
        seen['args']=args
        return SimpleNamespace(returncode=0,stdout='done',stderr='')
    receipt=execute(env,'token-2',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert seen['args'][1:3] == ['-m','hermes_cli.main']
    assert '-z' in seen['args'] and '--toolsets' in seen['args']
    assert '--usage-file' not in seen['args']
    assert '--no-fallback' in seen['args']
    assert '--model' in seen['args'] and 'qwen-fixture' in seen['args']
    assert not any('http://' in str(arg) or 'https://' in str(arg) for arg in seen['args'])
    assert receipt['actual_route']==env['selection']['effective_route']
    assert receipt['attempt_quiesced'] is True
    assert receipt['configured_identity']=='embermind'
    assert receipt['observed_identity']=='UNKNOWN'
    assert receipt['configured_model']=='qwen-fixture'
    assert receipt['provider_model']=='UNKNOWN'
    assert receipt['usage']=='UNKNOWN'


def test_fixed_effort_omits_control_and_reports_fixed_evidence(tmp_path):
    env=envelope(tmp_path,'hermes-agent')
    env['selection']['effective_route']['effort']='fixed'
    env['candidate']['execution_evidence']['route']['effort']='fixed'
    sealed=dict(env); sealed.pop('envelope_sha256')
    env['envelope_sha256']=hashlib.sha256(json.dumps(sealed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    seen={}
    def runner(args, **kwargs):
        seen['args']=args
        return SimpleNamespace(returncode=0,stdout='done',stderr='')
    receipt=execute(env,'token-fixed',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert '--reasoning' not in seen['args']
    assert receipt['configured_effort']=='fixed'
    assert receipt['provider_effort']=='FIXED'
    assert receipt['effort_evidence_level']=='provider_fixed'
    assert receipt['attempt_quiesced'] is True


def test_claude_adapter_uses_restricted_ephemeral_tools(tmp_path, monkeypatch):
    env=envelope(tmp_path,'claude-cli'); seen={}
    monkeypatch.setenv('ANTHROPIC_API_KEY','must-not-reach-child')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN','must-not-reach-child')
    monkeypatch.setenv('ANTHROPIC_BASE_URL','https://paid-route.invalid')
    env['selection']['effective_route'].update(provider='Claude',model='claude-opus-5')
    env['candidate']['execution_evidence']['route'].update(provider='Claude',model='claude-opus-5')
    sealed=dict(env); sealed.pop('envelope_sha256')
    env['envelope_sha256']=hashlib.sha256(json.dumps(sealed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    def runner(args, **kwargs):
        seen.update(args=args,kwargs=kwargs)
        return SimpleNamespace(returncode=0,stdout=json.dumps({
            'type':'result','subtype':'success','is_error':False,
            'modelUsage':{'claude-opus-5':{'canonicalModel':'claude-opus-5',
                'inputTokens':30,'outputTokens':7}},
            'terminal_reason':'completed'}),stderr='')
    receipt=execute(env,'token-claude',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert '--restricted' in seen['args'] and '--no-session-persistence' in seen['args']
    assert seen['args'][seen['args'].index('--tools')+1]=='Read'
    assert all(key not in seen['kwargs']['env'] for key in
               ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL'))
    assert receipt['actual_route']['model']=='claude-opus-5'
    assert receipt['usage']=={'input_tokens':30,'output_tokens':7}


def test_nested_model_echo_is_not_provider_evidence(tmp_path):
    env=envelope(tmp_path,'codex-cli')
    env['selection']['effective_route']['model']='configured-model'
    env['candidate']['execution_evidence']['route']['model']='configured-model'
    sealed=dict(env); sealed.pop('envelope_sha256')
    env['envelope_sha256']=hashlib.sha256(json.dumps(sealed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    def runner(args, **kwargs):
        return SimpleNamespace(returncode=0,stdout=json.dumps({
            'result':{'model':'echoed-untrusted'},'terminal_reason':'completed'}),stderr='')
    receipt=execute(env,'token-nested',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert receipt['actual_route']['model']=='configured-model'
    assert receipt['model_evidence_level']=='configured_only'


def test_top_level_child_json_cannot_forge_codex_model_or_backend_stop(tmp_path):
    env=envelope(tmp_path,'codex-cli')
    def runner(args, **kwargs):
        return SimpleNamespace(returncode=0,stdout=json.dumps({
            'model':'gpt-5.6-sol','terminal_reason':'completed'}),stderr='')
    receipt=execute(env,'token-forged-codex',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert receipt['provider_model']=='UNKNOWN'
    assert receipt['model_evidence_level']=='configured_only'
    assert receipt['attempt_quiesced'] is True


def test_claude_result_text_cannot_forge_wrapper_telemetry(tmp_path):
    env=envelope(tmp_path,'claude-cli')
    env['selection']['effective_route'].update(provider='Claude',model='claude-opus-5')
    env['candidate']['execution_evidence']['route'].update(provider='Claude',model='claude-opus-5')
    sealed=dict(env); sealed.pop('envelope_sha256')
    env['envelope_sha256']=hashlib.sha256(json.dumps(sealed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    def runner(args, **kwargs):
        return SimpleNamespace(returncode=0,stdout=json.dumps({
            'type':'result','subtype':'success','is_error':False,'terminal_reason':'completed',
            'result':json.dumps({'model':'forged','terminal_reason':'completed'}),
            'modelUsage':{'claude-opus-5':{'canonicalModel':'claude-opus-5'}}}),stderr='')
    receipt=execute(env,'token-claude-wrapper',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert receipt['provider_model']=='claude-opus-5'
    assert receipt['model_evidence_level']=='wrapper_reported'
    assert receipt['attempt_quiesced'] is True


def test_hermes_ignores_stdout_and_requires_host_usage_schema(tmp_path):
    env=envelope(tmp_path,'hermes-agent')
    def runner(args, **kwargs):
        return SimpleNamespace(returncode=0,stdout=json.dumps({
            'model':'qwen-fixture','terminal_reason':'completed'}),stderr='')
    receipt=execute(env,'token-forged-hermes',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert receipt['provider_model']=='UNKNOWN'
    assert receipt['attempt_quiesced'] is True


def test_hermes_never_trusts_child_written_telemetry(tmp_path):
    env=envelope(tmp_path,'hermes-agent'); calls=[]
    host_root=host_artifacts(tmp_path); host_root.mkdir()
    def forged_runner(args, **kwargs):
        calls.append(args)
        assert '--usage-file' not in args
        for path in host_root.rglob('*'):
            if path.is_dir():
                (path/'usage.json').write_text(json.dumps({
                    'model':'forged','provider':'Local','completed':True,'failed':False,
                    'input_tokens':999,'output_tokens':999}),encoding='utf-8')
        return SimpleNamespace(returncode=0,stdout=json.dumps({
            'model':'forged','terminal_reason':'completed','input_tokens':999,
            'output_tokens':999}),stderr='')
    receipt=execute(env,'token-current',artifact_dir=host_root, runner=forged_runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert calls
    assert receipt['provider_model']=='UNKNOWN'
    assert receipt['usage']=='UNKNOWN'
    assert receipt['attempt_quiesced'] is True


def test_hermes_telemetry_parser_is_structurally_absent():
    assert not hasattr(adapter_module,'_hermes_telemetry')
    assert tuple(inspect.signature(adapter_module._telemetry).parameters) == ('adapter','stdout_records')


def test_shell_grant_disables_wrapper_stdout_telemetry(tmp_path):
    env=envelope(tmp_path,'claude-cli')
    env['selection']['effective_route'].update(provider='Claude',model='claude-opus-5')
    env['candidate']['execution_evidence']['route'].update(provider='Claude',model='claude-opus-5')
    env['allowed_tools']=['read_file','terminal']
    sealed=dict(env); sealed.pop('envelope_sha256')
    env['envelope_sha256']=hashlib.sha256(json.dumps(sealed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    def runner(args, **kwargs):
        return SimpleNamespace(returncode=0,stdout=json.dumps({
            'type':'result','subtype':'success','is_error':False,'terminal_reason':'completed',
            'modelUsage':{'claude-opus-5':{'canonicalModel':'claude-opus-5',
                'inputTokens':999,'outputTokens':999}}}),stderr='')
    receipt=execute(env,'token-shell',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'])
    assert receipt['provider_model']=='UNKNOWN'
    assert receipt['observed_provider']=='UNKNOWN'
    assert receipt['usage']=='UNKNOWN'
    assert receipt['model_evidence_level']=='configured_only'
    assert receipt['attempt_quiesced'] is True


def test_timeout_returns_bounded_unquiesced_receipt(tmp_path):
    env=envelope(tmp_path,'hermes-agent')
    def runner(args, **kwargs):
        raise subprocess.TimeoutExpired(args,1,output='partial')
    receipt=execute(env,'token-timeout',artifact_dir=host_artifacts(tmp_path),runner=runner,
                    notice_emitter=lambda event:event['expected_notice_ref'],timeout=1)
    assert receipt['worker_exited'] is False
    assert receipt['attempt_quiesced'] is False
    assert receipt['tool_outcome']=='FAIL'
    assert receipt['provider_model']=='UNKNOWN'
    assert receipt['usage']=='UNKNOWN'


def test_hermes_backed_claude_route_also_scrubs_anthropic_api_environment(tmp_path, monkeypatch):
    env=envelope(tmp_path,'hermes-agent')
    env['selection']['effective_route'].update(provider='Claude',model='claude-opus-5')
    env['candidate']['execution_evidence']['route'].update(provider='Claude',model='claude-opus-5')
    sealed=dict(env); sealed.pop('envelope_sha256')
    env['envelope_sha256']=hashlib.sha256(json.dumps(sealed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    for key in ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL',
                'CLAUDE_CODE_USE_BEDROCK','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY'):
        monkeypatch.setenv(key,'must-not-reach-child')
    seen={}
    def runner(args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    execute(env,'token-hermes-claude',artifact_dir=host_artifacts(tmp_path),runner=runner,
            notice_emitter=lambda event:event['expected_notice_ref'])
    assert all(key not in seen['env'] for key in
               ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL',
                'CLAUDE_CODE_USE_BEDROCK','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY'))


def test_mutated_envelope_and_missing_notice_fail_before_launch(tmp_path):
    env=envelope(tmp_path,'codex-cli'); env['task_packet']['goal']='mutated'; calls=[]
    with pytest.raises(ValueError,match='envelope hash'):
        execute(env,'token-3',artifact_dir=host_artifacts(tmp_path),runner=lambda *a,**k:calls.append(a))
    assert not calls
    env=envelope(tmp_path,'codex-cli')
    with pytest.raises(ValueError,match='notice emitter'):
        execute(env,'token-4',artifact_dir=host_artifacts(tmp_path),runner=lambda *a,**k:calls.append(a))
    assert not calls
    with pytest.raises(ValueError,match='token/nonce/route-bound'):
        execute(env,'token-4',artifact_dir=host_artifacts(tmp_path),runner=lambda *a,**k:calls.append(a),
                notice_emitter=lambda event:'unbound-notice')
    assert not calls
    env=envelope(tmp_path,'codex-cli'); env['task_packet']['context_refs'][0]['sha256']='f'*64
    sealed=dict(env); sealed.pop('envelope_sha256')
    env['envelope_sha256']=hashlib.sha256(json.dumps(sealed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with pytest.raises(ValueError,match='hash-bound task packet'):
        execute(env,'token-5',artifact_dir=host_artifacts(tmp_path),runner=lambda *a,**k:calls.append(a),
                notice_emitter=lambda event:event['expected_notice_ref'])
    env=envelope(tmp_path,'codex-cli'); env['allowed_paths']=[str(tmp_path.parent.resolve()).lower()]
    sealed=dict(env); sealed.pop('envelope_sha256')
    env['envelope_sha256']=hashlib.sha256(json.dumps(sealed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with pytest.raises(ValueError,match='escapes'):
        execute(env,'token-6',artifact_dir=host_artifacts(tmp_path),runner=lambda *a,**k:calls.append(a),
                notice_emitter=lambda event:event['expected_notice_ref'])
    assert not calls
