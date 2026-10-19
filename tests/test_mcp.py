import json
import pytest
from uuid import uuid4
from types import SimpleNamespace
from fastapi.testclient import TestClient
from studycenter_ice.app import create_app
from studycenter_ice.config import Settings
from studycenter_ice.db import encode


@pytest.mark.asyncio
async def test_source_current_reading_has_bounded_metadata_and_keeps_history(tmp_path):
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    source=db.execute("INSERT INTO sources(kind,title,url) VALUES('manual','Original','https://example.org')")
    original=tmp_path/'original.pdf';original.write_bytes(b'Preserved original fixture')
    old='OLD GRID TABLE '+('obsolete table cell ' * 5000)
    historical_id='a'*64
    page={'page':1,'anchor':'page-1','text':'Current corrected text','gaps':['gap '+('x'*9000)]*30,
          'ocr_text':'DUPLICATE OCR '*8000,'ocr_gaps':['internal OCR details'],
          'native_text':'INTERNAL NATIVE '*8000,'visual_transcription':{'text':'INTERNAL TRANSCRIPTION '*8000},
          'grid_filter':{'applied':True,'lines':224,'spacing':[6.12,6.12],
                         'reason':'Raster background '+('x'*9000),'internal_text':old},
          'reading_options':{'ignore_grid':True,'internal_text':old},
          'reading_history':[{'page':1,'anchor':'page-1','text':old,'gaps':[],
                              'reading_id':historical_id,'saved_at':'2030-10-01'}]}
    doc=db.execute('INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path,pages) VALUES(?,?,?,?,?,?,?,?)',
                   (source,'https://example.org/pdf','Original','application/pdf','abc','2030-10-01',str(original),encode([page])))
    before=db.one('SELECT * FROM documents WHERE id=?',(doc,))
    result=await app.state.gateway.call('get_source',{'document_id':doc,'page':1})
    public=result['page']
    assert public['text']==page['text'] and result['total_length']==len(page['text'])
    assert not result['has_more'] and not result['truncated'] and result['next_offset'] is None
    assert len(encode(result))<5000
    assert not {'reading_history','ocr_text','ocr_gaps','native_text','visual_transcription'} & public.keys()
    assert public['grid_filter']['applied'] and public['grid_filter']['lines']==224
    assert public['grid_filter']['spacing']==[6.12,6.12] and 'internal_text' not in public['grid_filter']
    assert public['gaps_total']==30 and len(public['gaps'])<=10
    assert public['reading_history_count']==1
    client=TestClient(app)
    history=client.get(public['historical_reader_url'])
    assert history.status_code==200 and 'OLD GRID TABLE' in history.text
    assert client.get(f'/documents/{doc}/original').content==original.read_bytes()
    assert db.one('SELECT * FROM documents WHERE id=?',(doc,))==before
    end=await app.state.gateway.call('get_source',{'document_id':doc,'offset':len(page['text'])})
    assert end['page']['text']=='' and not end['has_more'] and not end['truncated'] and end['next_offset'] is None
    assert 'OLD GRID TABLE' not in encode(end) and 'DUPLICATE OCR' not in encode(end)


def test_mcp_real_routes_auth_scope_typed_write_replay_sources_and_preservation(tmp_path,monkeypatch):
    settings=Settings(data_dir=tmp_path)
    app=create_app(settings);db=app.state.db
    monkeypatch.setattr('studycenter_ice.mcp.credentials',lambda **kwargs:{'codex':'fixture-secret','reader':'reader-secret'})
    app.state.reminders.adapter=SimpleNamespace(send=lambda *args:None)
    async def no_model(*args):raise AssertionError('Typed MCP operations must not call Luna')
    app.state.assistant.adapter.responses=no_model
    with TestClient(app) as client:
        def rpc(name,args={},token='fixture-secret'):
            response=client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':name,'params':args},headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'})
            assert response.status_code==200,response.text
            return response.json()['result']
        assert client.post('/mcp',json={}).status_code==401
        initialized=rpc('initialize',{'protocolVersion':'2025-06-18'})
        assert initialized['serverInfo']['name']=='StudyCenter-ICE'
        assert len(rpc('tools/list')['tools'])==8
        identifier=str(uuid4())
        args={'name':'write','arguments':{'request_id':identifier,'action':'create_task','title':'Eigener MCP Test','date':'2030-10-23','time':'09:00:00'}}
        result=rpc('tools/call',args)['structuredContent'];item_id=result['item_id']
        assert result['created'] and result['entry']['precision']=='Europe/Vienna'
        assert rpc('tools/call',args)['structuredContent']==result
        assert db.one('SELECT count(*) n FROM items')['n']==1
        before=db.rows('SELECT * FROM items')
        details=rpc('tools/call',{'name':'get_item','arguments':{'item_id':item_id}})['structuredContent']
        assert details['item']['id']==item_id and details['data']['time']=='09:00:00'
        changed_args={'name':'write','arguments':args['arguments']|{'title':'Anderer Auftrag'}}
        assert rpc('tools/call',changed_args)['isError'] and db.rows('SELECT * FROM items')==before
        bad={'name':'write','arguments':args['arguments']|{'request_id':str(uuid4()),'sql':'DELETE FROM items'}}
        assert rpc('tools/call',bad)['isError'] and db.rows('SELECT * FROM items')==before
        db.execute("INSERT INTO app_preferences VALUES('connections',?)",(encode({'codex':{'read':True,'write':True},'reader':{'read':True,'write':False}}),))
        assert all(t['annotations']['readOnlyHint'] for t in rpc('tools/list',token='reader-secret')['tools'])
        assert 'error' in client.post('/mcp',json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':args},headers={'Authorization':'Bearer reader-secret'}).json()
        assert db.rows('SELECT * FROM items')==before
        course=db.execute("INSERT INTO courses(title) VALUES('Prüfkurs')")
        source=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Unterlage','https://example.org')",(course,))
        doc=db.execute('INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path,pages) VALUES(?,?,?,?,?,?,?,?)',(source,'https://example.org/pdf','PDF','application/pdf','abc','2030-10-01','unused',encode([{'page':16,'anchor':'page-16','text':'Originale organisatorische Frist','method':'ocr'}])))
        proof=rpc('tools/call',{'name':'get_source','arguments':{'document_id':doc,'page':16}})['structuredContent']
        assert proof['page']['page']==16 and proof['url'].endswith('?page=16')
        undo=rpc('tools/call',{'name':'undo_creation','arguments':{'request_id':identifier,'confirm':True}})['structuredContent']
        assert undo['undone'] and db.one('SELECT ignored FROM items WHERE id=?',(item_id,))['ignored']==1
