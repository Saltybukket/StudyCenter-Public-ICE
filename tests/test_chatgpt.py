import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app
from studycenter_ice.chatgpt import API, ISSUER, ChatGPTAuth, ChatGPTError, CredentialStore
from studycenter_ice.config import Settings
from studycenter_ice.db import Database, encode
from studycenter_ice.documents import DocumentStore
from studycenter_ice.llm import ModelAdapter, completed_text


async def test_oauth_validation_protected_storage_and_refresh_rotation(tmp_path):
    store = CredentialStore(tmp_path / 'accounts.dpapi')
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {'kid': 'test-key'}
    metadata = {'issuer': ISSUER, 'authorization_endpoint': ISSUER+'/api/accounts/authorize',
                'token_endpoint': ISSUER+'/api/accounts/oauth/token', 'jwks_uri': ISSUER+'/.well-known/jwks.json',
                'revocation_endpoint': ISSUER+'/oauth/revoke'}
    mode, refreshes = 'valid', []
    def tokens():
        claims = {'iss': ISSUER, 'aud': 'oaiapp_test', 'sub': 'person', 'email': 'person@example.org',
                  'iat': int(time.time()), 'exp': int(time.time())+3600, 'nonce': auth.pending['nonce']}
        if mode == 'nonce': claims['nonce'] = 'wrong'
        if mode == 'audience': claims['aud'] = 'other'
        if mode == 'expiry': claims['exp'] = int(time.time())-5
        signing = rsa.generate_private_key(public_exponent=65537, key_size=2048) if mode == 'signature' else key
        return {'access_token': 'access-initial', 'refresh_token': 'refresh-initial', 'token_type': 'Bearer', 'expires_in': 3600,
                'scope': 'openid offline_access resource.invoke chatgpt.tokens.use.direct',
                'id_token': jwt.encode(claims, signing, algorithm='RS256', headers={'kid': 'test-key'})}
    # callback consumes pending before exchange, so preserve its nonce in this fake server.
    nonce = ''
    def server(request):
        if request.url.path.endswith('openid-configuration'): return httpx.Response(200, json=metadata)
        if request.url.path.endswith('jwks.json'): return httpx.Response(200, json={'keys': [jwk]})
        form = parse_qs(request.content.decode())
        if form.get('grant_type') == ['authorization_code']:
            saved = auth.pending
            auth.pending = {'nonce': nonce}
            result = tokens()
            auth.pending = saved
            assert form['client_id'] == ['oaiapp_test'] and form['resource'] == [API]
            return httpx.Response(200, json=result)
        if form.get('grant_type') == ['refresh_token']:
            refreshes.append(form['refresh_token'][0])
            if mode == 'transient': return httpx.Response(503, json={'error': 'temporarily_unavailable'})
            if mode == 'revoked': return httpx.Response(400, json={'error': 'invalid_grant'})
            assert 'scope' not in form and form['client_id'] == ['oaiapp_test']
            return httpx.Response(200, json={'access_token': 'access-rotated', 'refresh_token': 'refresh-rotated', 'token_type': 'Bearer', 'expires_in': 3600})
        assert form['token_type_hint'] == ['refresh_token']
        return httpx.Response(200)
    auth = ChatGPTAuth(store, lambda: httpx.AsyncClient(transport=httpx.MockTransport(server)))
    for mode in ('nonce', 'audience', 'expiry', 'signature', 'valid'):
        url = await auth.begin('http://127.0.0.1:18791/auth/callback')
        params = parse_qs(urlsplit(url).query)
        nonce = params['nonce'][0]
        host = store.read()['host_id']
        assert params['client_id'] == ['dynamic_agent_client'] and params['code_challenge_method'] == ['S256']
        with pytest.raises(ChatGPTError, match='state'):
            await auth.callback({'state': 'forged', 'code': 'x', 'client_id': 'oaiapp_test'})
        assert auth.pending  # An unsolicited request does not consume a valid attempt.
        callback = {'state': params['state'][0], 'code': 'code', 'client_id': 'oaiapp_test'}
        if mode != 'valid':
            with pytest.raises(ChatGPTError, match='invalid_id_token'): await auth.callback(callback)
            assert not auth.status()['connected']
        else:
            await auth.callback(callback)
            with pytest.raises(ChatGPTError, match='state'): await auth.callback(callback)
    if store.storage_status()['persistent'] and store.backend == 'dpapi':
        assert b'access-initial' not in store.path.read_bytes()
    else:
        assert not store.path.exists()
    data = store.read(); data['profiles'][0]['expires_at'] = 0; store.write(data)
    mode = 'transient'
    with pytest.raises(ChatGPTError, match='temporarily_unavailable'): await auth.access_token()
    assert store.read()['profiles'][0]['refresh_token'] == 'refresh-initial'
    mode = 'valid'
    assert await asyncio.gather(auth.access_token(), auth.access_token()) == ['access-rotated']*2
    assert refreshes == ['refresh-initial', 'refresh-initial']  # One transient try, one serialized success.
    assert store.read()['profiles'][0]['refresh_token'] == 'refresh-rotated'
    await auth.logout()
    assert not auth.status()['connected'] and store.read()['host_id'] == host
    url = await auth.begin('http://127.0.0.1:18791/auth/callback', 'oaiapp_test')
    assert parse_qs(urlsplit(url).query)['client_id'] == ['oaiapp_test']
    assert 'agent_name_hint' not in parse_qs(urlsplit(url).query)


async def test_responses_sse_requires_completed_even_after_valid_json():
    async def lines(events, final_blank=True):
        yield ': keepalive'
        for event in events:
            raw = json.dumps(event)
            # Two data lines split at a comma; each frame is dispatched only on blank line.
            left, right = raw.split(',', 1) if ',' in raw else (raw, '')
            yield 'data: '+left+(',' if right else '')
            if right: yield 'data: '+right
            if final_blank: yield ''
    delta = {'type':'response.output_text.delta', 'delta':'{"items":[]}'}
    complete = {'type':'response.completed', 'response':{'status':'completed'}}
    assert await completed_text(lines([delta, complete])) == '{"items":[]}'
    for terminal, expected in [(None, 'without_response_completed'),
        ({'type':'response.incomplete','response':{'status':'incomplete'}}, 'response_incomplete'),
        ({'type':'response.failed','response':{'error':{'code':'subscription_sharing_usage_limit_exceeded'}}}, 'subscription_sharing_usage_limit_exceeded')]:
        with pytest.raises(ChatGPTError, match=expected):
            await completed_text(lines([delta]+([terminal] if terminal else [])))
    assert await completed_text(lines([complete | {'response':{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'final'}]}]}}], False)) == 'final'


async def test_oauth_extraction_evidence_shared_views_and_idempotent_replay(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, model_provider='chatgpt', oauth_model='gpt-6-luna')
    db = Database(tmp_path/'studycenter_ice.sqlite3')
    cid = db.execute("INSERT INTO courses(title,semester) VALUES('Prüfkurs','2026W')")
    sid = db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Prüfquelle','https://example.org/course/')",(cid,))
    store = DocumentStore(db, settings)
    quote = 'A2 muss bis 12. November 2026 abgegeben werden.'
    doc_id = store.save(sid, 'https://example.org/course/a2', 'A2', 'text/plain', quote.encode())
    doc = store.process(doc_id)
    output = {'items':[{'title':'A2 abgeben','kind':'task','identity_scope':'course','identity_key':'A2',
              'date':'2026-11-12','date_meaning':'deadline','obligation':'required','quote':quote,'page':1,'anchor':'A2'}]}
    ungrounded = False
    class Auth:
        models = [{'slug':'gpt-6-luna','display_name':'GPT-6 Luna'}]
        async def access_token(self): return 'test-oauth'
    def server(request):
        body = json.loads(request.content)
        assert str(request.url) == API+'/responses'
        assert request.headers['authorization'] == 'Bearer test-oauth'
        assert body['store'] is False and body['stream'] is True and isinstance(body['input'],list)
        assert set(body) == {'model','instructions','input','store','stream'}
        value = json.loads(json.dumps(output))
        if ungrounded: value['items'][0]['quote'] = 'Nicht in der Quelle'
        value['items'][0]['group'] = 'CCGEG008' if json.loads(body['input'][0]['content'])['context'].get('previous_identities') else None
        event = {'type':'response.completed','response':{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]}}
        return httpx.Response(200, content=('data: '+json.dumps(event)+'\n\n').encode())
    original_client = httpx.AsyncClient
    monkeypatch.setattr('studycenter_ice.llm.httpx.AsyncClient', lambda **kw: original_client(transport=httpx.MockTransport(server), **kw))
    adapter = ModelAdapter(db, settings, Auth())
    await adapter.analyze(doc, allow_probe=True)
    assert db.one('SELECT analysis_state FROM documents')['analysis_state'] == 'success'
    before = db.items()[0]['id']
    db.execute("UPDATE items SET status='done',pinned=1 WHERE id=?",(before,))
    db.execute("UPDATE documents SET analysis_state='pending',progress='{}' WHERE id=?",(doc_id,))
    await adapter.analyze(db.one('SELECT * FROM documents'), allow_probe=True)
    assert len(db.items()) == 1 and db.items()[0]['id'] == before and db.items()[0]['status'] == 'done'
    app = create_app(settings)
    with TestClient(app) as client:
        assert 'A2 abgeben' not in client.get('/board').text
        assert 'A2 abgeben' in client.get('/archive').text
        assert 'A2 abgeben' in client.get(f'/items/{before}').text
        assert client.get('/api/calendar').json()[0]['id'] == str(before)
    ungrounded = True
    with pytest.raises(ValueError, match='Unbelegte Fundstelle'):
        await adapter.extract(json.loads(doc['pages']), {'course':'Prüfkurs'})
    # Correct every invalid table citation in the one existing retry.
    rows = ['12.11.26 | HS B / 11:00 Uhr', '19.11.26 | HS B / 11:00 Uhr']
    invalid = [r.replace(' | ', ' ') for r in rows]
    calls = []
    async def table_response(instructions, payload):
        calls.append(instructions)
        entries = []
        for n, row in enumerate(invalid if len(calls)==1 else rows):
            entries.append(output['items'][0] | {'identity_key':f'T{n}', 'anchor':f'T{n}', 'quote':row})
        return json.dumps({'items':entries})
    adapter.responses = table_response
    result = await adapter.extract([{'page':1,'anchor':'table','text':'\n'.join(rows),'gaps':[]}], {'course':'Prüfkurs'})
    assert len(calls)==2 and all(row in calls[1] for row in invalid)
    assert [item.quote for item in result.items]==rows
