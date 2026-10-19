"""Bounded offline fixtures; live TU-mail access is verified separately."""
import json
from email.message import EmailMessage
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import pymupdf
import pytest

from studycenter_ice.app import calendar_events, create_app
from studycenter_ice.browser import readonly_mail_request
from studycenter_ice.config import Settings
from studycenter_ice.db import Database, encode
from studycenter_ice.documents import DocumentStore
from studycenter_ice.mail import MailError, collect, matches, message_pages
from studycenter_ice.llm import ModelAdapter
from studycenter_ice.models import Candidate, Extraction
from fastapi.testclient import TestClient


def message(uid=1, subject='CON: Lab 1', body='Lab 1 deadline: 12 November 2026.', parent=None, pdf=None):
    mail = EmailMessage()
    mail['From'] = 'Course <con@example.test>'
    mail['Subject'] = subject
    mail['Date'] = f'{uid + 1} Oct 2026 12:00:00 +0200'
    mail['Message-ID'] = f'<mail-{uid}@example.test>'
    if parent:
        mail['In-Reply-To'] = parent
        mail['References'] = parent
    mail.set_content(body)
    if pdf:
        mail.add_attachment(pdf, maintype='application', subtype='pdf', filename='Lab 1.pdf')
    return mail.as_bytes()


class Inbox:
    def __init__(self, messages, validity=b'7', fail_uid=None):
        self.messages, self.validity, self.fail_uid = messages, validity, fail_uid
        self.calls = []
        self.logged_out = False

    def select(self, folder, readonly):
        assert folder == '"INBOX"' and readonly is True
        return 'OK', []

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return name, [self.validity]

    def uid(self, command, *arguments):
        self.calls.append((command, arguments))
        if command == 'SEARCH':
            assert 'SINCE' in arguments and 'UID' in arguments
            return 'OK', [b' '.join(str(uid).encode() for uid in self.messages)]
        assert command == 'FETCH'
        uid, fields = arguments
        assert 'BODY.PEEK[' in fields
        raw = self.messages[int(uid)]
        if fields == '(BODY.PEEK[])':
            if int(uid) == self.fail_uid:
                return 'NO', []
            return 'OK', [(b'BODY[]', raw)]
        return 'OK', [(f'RFC822.SIZE {len(raw)}'.encode(), raw.split(b'\n\n', 1)[0] + b'\n\n')]

    def logout(self):
        self.logged_out = True


def setup(tmp_path, budget=30):
    settings = Settings(data_dir=tmp_path, llm_base_url='https://example.test', llm_model='fixture')
    db = Database(tmp_path / 'studycenter_ice.sqlite3')
    course = db.execute("INSERT INTO courses(title,semester) VALUES('CON','2026W')")
    source = db.execute("INSERT INTO sources(course_id,kind,title,url,config) VALUES(?,'mail','Course mail','imap://example.test/INBOX',?)",
                        (course, encode({'folder':'INBOX','since':'2026-10-01','keywords':['CON'],'senders':[], 'max_pages':budget})))
    return settings, db, source, DocumentStore(db, settings)


async def test_mail_reads_filter_cursor_pdf_repeat_and_failed_uid(tmp_path, monkeypatch):
    settings, db, sid, store = setup(tmp_path, 2)
    pdf = pymupdf.open()
    page = pdf.new_page(); page.insert_text((40,40), 'Lab 1 deadline: 12 November 2026.')
    pdf_bytes = pdf.tobytes(); pdf.close()
    raw = message(pdf=pdf_bytes)
    reply = message(3, parent='<mail-1@example.test>', body='Lab 1 now due 15 November 2026.')
    mailbox = Inbox({1:raw, 2:message(2, subject='Conference invitation'), 3:reply})
    credentials = SimpleNamespace(read=lambda: {'host':'example.test'})
    def run(client):
        return collect(db.source(sid), db, store, credentials, lambda values:client)
    first = run(mailbox)
    assert first['messages_downloaded']==1 and first['filtered_out']==1 and first['remaining_messages']==1
    assert mailbox.logged_out
    assert not any(args[0]=='2' and args[1]=='(BODY.PEEK[])' for command,args in mailbox.calls if command=='FETCH')
    documents = db.rows('SELECT * FROM documents ORDER BY id')
    assert len(documents)==2
    original = next(d for d in documents if d['mime']=='application/pdf')
    assert store.process(original['id'])['processing_state']=='success'
    assert json.loads(original['mail_metadata'])['filename']=='Lab 1.pdf'
    from pathlib import Path
    assert Path(original['original_path']).read_bytes()==pdf_bytes
    failed = Inbox(mailbox.messages, fail_uid=3)
    with pytest.raises(MailError, match='vollständig'):
        run(failed)
    assert failed.logged_out and json.loads(db.source(sid)['crawl_state'])['last_uid']==2
    second = run(Inbox(mailbox.messages))
    assert second['messages_downloaded']==1 and second['complete']
    mails = db.rows("SELECT * FROM documents WHERE mime='message/rfc822' ORDER BY id")
    assert mails[0]['url']==mails[1]['url']
    assert json.loads(mails[1]['mail_metadata'])['parents']==[mails[0]['id']]
    db.execute("UPDATE documents SET analysis_state='success' WHERE id=?", (mails[0]['id'],))
    before = [d['id'] for d in db.rows('SELECT id FROM documents ORDER BY id')]
    assert run(Inbox(mailbox.messages))['messages_downloaded']==0
    assert run(Inbox(mailbox.messages, validity=b'8'))['uidvalidity_reset']
    assert before==[d['id'] for d in db.rows('SELECT id FROM documents ORDER BY id')]
    assert db.one('SELECT analysis_state FROM documents WHERE id=?',(mails[0]['id'],))['analysis_state']=='success'

    # Same original storage through the real Webmail collector, bounded list/raw reads.
    from studycenter_ice.mail import collect_webmail
    config={'transport':'webmail','folder':'INBOX','since':'2026-10-01','keywords':['CON'],'senders':[], 'max_pages':2}
    db.execute('UPDATE sources SET config=?,crawl_state=? WHERE id=?',(encode(config),'{}',sid))
    async def bootstrap(context,browser=None):
        return {'HordeCore.conf':{'TOKEN':'fixture','URI_AJAX':'/services/ajax.php/imp/'},
                'DimpCore.conf':{'qsearchid':'virtual-search','FLAG_SEEN':'seen'}}
    monkeypatch.setattr('studycenter_ice.mail.webmail_bootstrap',bootstrap)
    class Response:
        status=200
        headers={'content-type':'text/plain'}
        def __init__(self,content):self.content=content
        async def text(self):return self.content
        async def body(self):return self.content
    originals={i:message(i,pdf=pdf_bytes) for i in range(1,4)}
    class Requests:
        changed=False
        reads=0
        async def post(self,url,form,**kwargs):
            assert url.endswith('/services/ajax.php/imp/viewPort')
            vp=json.loads(form['viewport']); assert vp['view']=='virtual-search' and vp['qsearchmbox']=='SU5CT1g'
            assert not any(k in vp for k in ('msgload','sortby','applyfilter','qsearchfilter'))
            start,end=map(int,vp['slice'].split(':'))
            data={str(i):{'flag':['seen'] if self.changed and self.reads else []} for i in range(start,min(end,3)+1)}
            return Response('/*-secure-'+encode({'tasks':{'imp:viewport':{'totalrows':3,'data':data,'rowlist':{k:int(k) for k in data}}}})+'*/')
        async def get(self,url,**kwargs):
            params=parse_qs(url.split('?',1)[1]);assert params['actionID']==['view_source']
            self.reads+=1
            return Response(originals[int(params['buid'][0])])
    request=Requests();context=SimpleNamespace(request=request)
    live=await collect_webmail(db.source(sid),context,db,store)
    assert not live['complete'] and live['flags_checked']==2 and live['unseen_checked']==2
    assert json.loads(db.source(sid)['crawl_state'])['offset']==2
    assert (await collect_webmail(db.source(sid),context,db,store))['complete']
    document_ids=[d['id'] for d in db.rows('SELECT id FROM documents ORDER BY id')]
    await collect_webmail(db.source(sid),context,db,store)
    await collect_webmail(db.source(sid),context,db,store)
    assert document_ids==[d['id'] for d in db.rows('SELECT id FROM documents ORDER BY id')]
    request.reads=0;request.changed=True
    with pytest.raises(MailError,match='Flags'):
        await collect_webmail(db.source(sid),context,db,store)
    assert json.loads(db.source(sid)['crawl_state'])['offset']==0

    # A local mail read never implies permission to send its text to a model.
    from contextlib import asynccontextmanager
    from studycenter_ice.sync import SyncService
    class LocalBrowser:
        visible=False
        login_mode=False
        async def finish_login(self):pass
        async def probe(self,services):return {'mail':{'state':'authenticated'}}
        async def close_idle(self):pass
        @asynccontextmanager
        async def crawl_context(self):yield context
    async def local_only(*args,**kwargs):return {'source':sid,'complete':True}
    monkeypatch.setattr('studycenter_ice.sync.collect_webmail',local_only)
    service=SyncService(db,settings,LocalBrowser())
    async def must_not_send(*args,**kwargs):raise AssertionError('Mail sent without explicit source setting')
    monkeypatch.setattr(service.model,'analyze',must_not_send)
    run_id=db.reserve_run('manual')
    await service._run(run_id,'manual',sid)
    result=next(r for r in json.loads(db.one('SELECT results FROM sync_runs WHERE id=?',(run_id,))['results']) if r.get('source')==sid)
    assert result['analysis_attempted']==0 and 'aktivieren' in result['analysis_message'] and result['pending']>0

    selected=db.one("SELECT id FROM documents WHERE source_id=? AND analysis_state!='success' ORDER BY id DESC LIMIT 1",(sid,))['id']
    config.update(allow_model_analysis=True,model_document_ids=[selected],max_analysis_documents=30)
    db.execute('UPDATE sources SET config=? WHERE id=?',(encode(config),sid))
    calls=[]
    async def selected_only(document,**kwargs):calls.append(document['id'])
    monkeypatch.setattr(service.model,'analyze',selected_only)
    await service._run(db.reserve_run('manual'),'manual',sid)
    assert calls==[selected]  # Unapproved messages remain local even when a new batch arrives.

    # The approved whole-mailbox source reuses exact originals across folders.
    from studycenter_ice.mail import save_mail
    all_sid=db.execute("INSERT INTO sources(kind,title,url,config) VALUES('mail','All mail','https://example.test/all',?)",
        (encode({'include_all':True,'folders':['INBOX','INBOX.Sent'],'since':'2026-08-01'}),))
    known=save_mail(db.source(all_sid),db,store,originals[1],'webmail.tugraz.at','INBOX',None,None,{'unsupported':[]})
    assert known in document_ids
    before_count=db.one('SELECT count(*) n FROM documents')['n']
    assert save_mail(db.source(all_sid),db,store,originals[1],'webmail.tugraz.at','INBOX.Sent',None,None,{'unsupported':[]})==known
    assert db.one('SELECT count(*) n FROM documents')['n']==before_count
    class AllRequests(Requests):
        async def post(self,url,form,**kwargs):
            import base64
            vp=json.loads(form['viewport'])
            assert vp['qsearchfield']=='from' and vp['qsearch']=='@'
            folder=base64.urlsafe_b64decode(vp['qsearchmbox']+'='*(-len(vp['qsearchmbox'])%4)).decode()
            data={'1':{'date':'02.10.2026','flag':[]},'2':{'date':'01.07.2026','flag':[]}} if folder=='INBOX' else {}
            return Response('/*-secure-'+encode({'tasks':{'imp:viewport':{'totalrows':len(data),'data':data,'rowlist':{k:int(k) for k in data}}}})+'*/')
    all_requests=AllRequests()
    all_result=await collect_webmail(db.source(all_sid),SimpleNamespace(request=all_requests),db,store)
    assert all_result['complete'] and all_result['messages_downloaded']==1 and all_result['filtered_out']==1
    assert all_requests.reads==1 and all_result['flags_checked']==2
    assert json.loads(db.source(all_sid)['crawl_state'])['document_ids']==[known]+[d['id'] for d in db.rows("SELECT id FROM documents WHERE json_extract(mail_metadata,'$.parent_document')=?",(known,))]


async def test_mail_existing_extraction_thread_update_views_and_status(tmp_path, monkeypatch):
    settings, db, sid, store = setup(tmp_path)
    url = 'imap://example.test/course-1/thread/test'
    def save(uid, deadline, published):
        raw = message(uid, body=f'Lab 1 deadline: {deadline} November 2026.')
        doc = store.process(store.save(sid, url, 'CON Lab 1', 'message/rfc822', raw, published,
                                       mail_metadata={'message_id':f'<{uid}>','thread':'test','subject':'CON Lab 1'}))
        return doc
    older = save(1,12,'2026-10-02T12:00:00+02:00')
    newer = save(2,15,'2026-10-03T12:00:00+02:00')
    adapter = ModelAdapter(db, settings)
    async def output(instructions, payload):
        text = json.loads(payload)['document_segments'][0]['text']
        day = '15' if '15 November' in text else '12'
        item = Candidate(title='Lab 1', kind='task', identity_scope='course',identity_key='Lab 1:deadline',
            date=f'2026-11-{day}',date_meaning='deadline',obligation='required',
            quote=f'Lab 1 deadline: {day} November 2026.',page=1,anchor='lab-1-deadline')
        return Extraction(items=[item]).model_dump_json()
    monkeypatch.setattr(adapter,'chat_completions', output)
    await adapter.analyze(newer)
    item = db.items()[0]
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?",(encode({'description':'My note'}),item['id']))
    await adapter.analyze(older)  # A delayed old message cannot undo the newer deadline.
    await adapter.analyze(newer)
    assert len(db.items())==1
    item = db.items()[0]
    assert item['data']['date']=='2026-11-15' and item['status']=='doing' and item['pinned']
    assert item['data']['description']=='My note'
    assert calendar_events([item])[0]['start']=='2026-11-15' and calendar_events([item])[0]['allDay']
    async def hallucination(instructions,payload):
        value = json.loads(await output(instructions,payload))
        value['items'][0]['quote']='An invented date not in the mail.'
        return encode(value)
    monkeypatch.setattr(adapter,'chat_completions', hallucination)
    with pytest.raises(ValueError, match='Unbelegte'):
        await adapter.extract(json.loads(newer['pages']),{})
    async def auth_message(instructions,payload):
        assert '123456' not in payload and 'https://example.test/reset?token=secret' not in payload
        return Extraction(items=[]).model_dump_json()
    monkeypatch.setattr(adapter,'chat_completions',auth_message)
    await adapter.extract([{'page':1,'anchor':'message','text':'Your Lenovo Code: 123456 https://example.test/reset?token=secret','gaps':[]}],
                          {'mail':{'subject':'Your Lenovo Code'}})
    with TestClient(create_app(settings)) as client:
        assert 'Lab 1' in client.get('/?days=60').text and 'Lab 1' in client.get('/board').text
        assert client.get('/api/calendar').json()[0]['id']==str(item['id'])
        assert 'Message-ID' in client.get(f"/documents/{newer['id']}").text


def test_webmail_auth_callback_list_reads_and_mutations_blocked():
    list_url='https://webmail.tugraz.at/services/ajax.php/imp/listMailboxes'
    assert readonly_mail_request(list_url,'POST','token=fixture&all=1&unsub=1')[0]
    assert not readonly_mail_request(list_url,'POST','token=fixture&all=1&expall=1')[0]
    host='https://webmail.tugraz.at'
    assert readonly_mail_request(host+'/saml/sp/profile/post/acs','POST','SAMLResponse=fixture')[0]
    assert readonly_mail_request(host+'/my.policy','POST','auth=fixture')[0]
    body=urlencode({'viewport':encode({'rangeslice':'0:20','sortby':1,'sortdir':0,'delhide':1,'applyfilter':1,'qsearchfilter':'fixture'}), 'msgload':'7', 'token':'fixture'})
    allowed, clean = readonly_mail_request(host+'/services/ajax.php/imp/dynamicInit','POST',body)
    data=parse_qs(clean)
    assert allowed and 'msgload' not in data and data['token']==['fixture']
    assert json.loads(data['viewport'][0])=={'rangeslice':'0:20'}
    for action in ('showMessage','flagMessages','deleteMessages','sendMessage','savePref'):
        assert not readonly_mail_request(host+'/services/ajax.php/imp/'+action,'POST',body)[0]
    assert not readonly_mail_request(host+'/imp/compose.php')[0]
    assert not readonly_mail_request(host+'/imp/view.php?actionID=view_source')[0]  # Browser UI cannot open preview.
    assert not matches('conference invitation',['CON']) and matches('Re: CON Lab',['CON'])
    mail=EmailMessage();mail.set_content('Full plain body');mail.add_alternative('<p>Duplicate HTML</p>',subtype='html')
    assert 'Full plain body' in message_pages(mail.as_bytes())[0]['text']
    assert 'Duplicate HTML' not in message_pages(mail.as_bytes())[0]['text']

    from studycenter_ice.llm import mail_plan_candidate
    from studycenter_ice.models import Candidate
    fake=SimpleNamespace(source=lambda sid:{'course_id':1},rows=lambda *args:[])
    plan={'source_id':1,'pages':encode([{'page':1,'text':'Standardgruppe. Dies ist eine automatisierte Nachricht von TUGRAZonline'}])}
    offered=Candidate(title='Lab slot',kind='event',date='2026-10-30',time='16:00:00',end_date='2026-10-30',end_time='18:00:00',
        identity_scope='course',identity_key='tugrazonline:wrong-uid',quote='Standardgruppe',page=1,anchor='lab-slot')
    note=mail_plan_candidate(offered,plan,fake)
    assert note.kind=='notice' and note.date==offered.date and note.identity_key is None
    assert calendar_events([{'id':1,'ignored':0,'status':'open','conflict':0,'data':note.model_dump(mode='json')}])==[]
    known=offered.model_dump(mode='json')|{'identity_key':'tugrazonline:actual-uid','group':'Standardgruppe'}
    fake.rows=lambda *args:[{'id':7,'payload':encode(known)}]
    admitted=mail_plan_candidate(offered,plan,fake)
    assert admitted.kind=='event' and admitted.identity_key=='tugrazonline:actual-uid'
    # An unrelated calendar UID or a planned end time must never create a booking.
    changed=offered.model_copy(update={'end_time':__import__('datetime').time(18,15),'source_state':'unclear'})
    assert mail_plan_candidate(changed,plan,fake).kind=='notice'
    fake.rows=lambda *args:[{'id':7,'payload':encode(known|{'source_state':'cancelled'})}]
    assert mail_plan_candidate(offered,plan,fake).kind=='notice'
    fake.source=lambda sid:{'course_id':None}
    newsletter=plan|{'mail_metadata':encode({'subject':'CSBME News 1/2026'})}
    announcement=offered.model_copy(update={'kind':'exam'})
    assert mail_plan_candidate(announcement,newsletter,fake).kind=='notice'
