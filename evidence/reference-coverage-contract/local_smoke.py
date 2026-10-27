"""Independent synthetic-DTO/storage/envelope smoke; no pytest or backend execution.
Uses available system dependencies, not the frozen environment; not qualification evidence.
"""
import json, os, tempfile
from pathlib import Path
from svweave.adapters.slang.ingest import ingest
from svweave.application.options import WorkspaceOptions
from svweave.application.semantic_common import build_envelope, require_build
from svweave.config.context import WorkspaceContext
from svweave.config.trusted import policy_path_for
from svweave.domain.envelope import Envelope, Pagination, ReferenceCoverage
from svweave.domain.ids import sha256_hex, compute_build_id
from svweave.domain.states import Freshness
from svweave.manifest.resolver import resolve_profile
from svweave.snapshot.capture import capture_snapshot
from svweave.storage.db import Database
from svweave.storage.semantic import dumps

checks = 0
def check(ok, label):
    global checks
    assert ok, label
    checks += 1

def loc(start=7, end=8):
    return dict(file='a.sv', start=start, end=end, line=1, col=start+1,end_line=1,end_col=end+1)

def dto(ref_count=2):
    return dict(schema_version='svw-extract-1', extractor_version='x', pyslang_version='12',
      status={'compiled':True}, driver_messages=[], diagnostics=[], dependencies={},
      declarations=[dict(idx=0,kind='module',name='a',qualified_name='a',loc=loc(),full=None,cu=0,library=None,type=None,attrs={})],
      references=[dict(kind='value',loc=loc(i,i+1),target_idx=0,target_name=None,status='resolved_static',reason=None,context={},container_idx=None) for i in range(ref_count)],
      instances=[],port_bindings=[],assertions=[],specializations=[],inactive=[],gaps=[],unsupported_nodes=[],stats={})

with tempfile.TemporaryDirectory(prefix='svw-coverage-') as tmp:
    root=Path(tmp)/'ws';root.mkdir()
    os.environ['XDG_CONFIG_HOME']=str(Path(tmp)/'xdg')
    (root/'a.sv').write_text('module a; endmodule\n')
    (root/'svweave.yaml').write_text(json.dumps({'schema_version':'1.0','project':{'id':'t','root':'.','default_profile':'p'},'source_roots':[{'id':'project','path':'.','include':['**'],'exclude':[]}], 'profiles':{'p':{'cwd':'.','source_inputs':[{'kind':'file','path':'a.sv'}],'compilation_units':{'mode':'single'},'top_modules':['a']}}}))
    pp=policy_path_for(root/'svweave.yaml');pp.parent.mkdir(parents=True)
    pp.write_text(json.dumps({'schema_version':'1.0','allowed_roots':[str(root)],'writable_paths':[],'approval_required':True,'network_enabled':False,'editing_enabled':False}))
    ctx=WorkspaceContext.load(root); nm=resolve_profile(ctx,'p',{}); snap=capture_snapshot(ctx,nm)
    tc=sha256_hex('tc'); bid=compute_build_id(snap.snapshot_id,nm.config_id,tc)
    build=dict(build_id=bid,workspace_id='t',profile='p',snapshot_id=snap.snapshot_id,config_id=nm.config_id,toolchain_id=tc,semantic_schema_version='1',completeness='COMPLETE',diagnostic_outcome='NO_ERRORS')
    ing=ingest(dto(),snap,bid,'j',['a'])
    cov=ReferenceCoverage.model_validate(ing.summary['reference_coverage'])
    check(ing.completeness=='COMPLETE' and cov.status=='PARTIAL','complete independent')
    check(not cov.absence_proof_supported,'absence hard false')
    check(cov.features['packed_dimension_expressions']=='UNSUPPORTED','dimensions')
    check(cov.scopes['unloaded_library_members']=='NOT_SEARCHED','library')
    report=dict(build_id=bid,job_id='j',extractor_version='x',pyslang_version='12',scope='a',tops=dumps(['a']),unresolved_count=0,dynamic_count=0,reasons='[]',limitations=dumps(ing.limitations),stats=dumps(ing.summary),dependency_report='{}',source_map='{}')
    with Database(ctx.state_dir/'svweave.db') as db:
        db.upsert_workspace('t',{'project':'project'},sha256_hex('p'));db.record_snapshot(snap)
        db.stage_build(build,{**ing.rows,'build_reports':[report]})
    opts=WorkspaceOptions(root=root); sid=ing.rows['declarations'][0]['symbol_id']
    def coverage(env):
        c=env.analysis_coverage.reference_coverage
        check(not c.absence_proof_supported,'query absence hard false')
        return c
    with Database(ctx.state_dir/'svweave.db') as db:
        for fr in Freshness:
            env=build_envelope(ctx,db,require_build(db,bid),'r',{},freshness=fr,reference_query='references',pagination=Pagination(truncated=True))
            check('result_truncated' in coverage(env).blockers,'truncation blocker')
            if fr!=Freshness.CURRENT: check('freshness_'+fr.value.lower() in coverage(env).blockers,'freshness blocker')
        for stats in ({},{'reference_coverage':{'contract_version':'2'}},[]):
            db.conn.execute('UPDATE build_reports SET stats=? WHERE build_id=?',(dumps(stats),bid))
            env=build_envelope(ctx,db,require_build(db,bid),'r',{},freshness=Freshness.CURRENT)
            check(coverage(env).status=='UNKNOWN','old stats unknown')
        old=env.model_dump(mode='json');old['schema_version']='1.0';del old['analysis_coverage']['reference_coverage']
        check(Envelope.model_validate(old).schema_version=='1.0','old envelope')
    for scope in ('project','external'):
        d=dto(0); d['unsupported_nodes']=[dict(kind='Specparam',scope=scope,count=2,sample=loc(),detail='d')]
        d['dropped_refs']=[dict(scope=scope,count=3,reason='unrecordable')]
        ing2=ingest(d,snap,bid,'j',['a'])
        c=ReferenceCoverage.model_validate(ing2.summary['reference_coverage'])
        check(ing2.completeness==('PARTIAL' if scope=='project' else 'COMPLETE'),'unsupported verdict')
        check(getattr(c.observations,'unsupported_nodes_'+scope)==2,'unsupported origin')
        check(getattr(c.observations,'dropped_references_'+scope)==3,'dropped origin')
print(f'PASS: {checks} independent pure Python/ingest/storage/envelope smoke assertions; no backend or pytest invoked')
