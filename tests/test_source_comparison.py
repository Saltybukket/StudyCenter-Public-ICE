import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app, action_state, calendar_events
from studycenter_ice.comparison import comparison, save
from studycenter_ice.config import Settings, VIENNA
from studycenter_ice.db import Database, encode
from studycenter_ice.google_calendar import projection
from studycenter_ice.ical import export_calendar
from studycenter_ice.models import Candidate
from studycenter_ice.presentation import item_tags


def source(db,day='2030-10-16',url='https://example.org/registration',text=None,**extra):
    course=db.one('SELECT id FROM courses')
    cid=course['id'] if course else db.execute("INSERT INTO courses(title,semester) VALUES('Messtechnik','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'teachcenter','Kursseite',?)",(cid,url))
    n=db.one('SELECT COUNT(*) n FROM documents')['n']+1
    quote=text or f'Anmeldung endet {day} um 10:00.'
    identifier=db.execute('INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path,pages,analysis_state) VALUES(?,?,?,?,?,?,?,?,?)',
        (sid,url,'Kursseite','text/html',str(n),f'2030-10-01T10:{n:02}:00+00:00','unused',encode([{'page':1,'text':quote}]),'success'))
    doc=db.one('SELECT * FROM documents WHERE id=?',(identifier,))
    value=Candidate(title='Gruppenanmeldung',kind='registration',identity_key='group-registration',identity_scope='course',
        semester='2030W',date=day,time='10:00',date_meaning='window',obligation='required',quote=quote,page=1,anchor='registration',**extra)
    iid=db.ingest(doc,value)
    return iid,doc


def row(db,iid): return next(i for i in db.items() if i['id']==iid)


def test_two_sources_apply_shared_projections_and_safe_reset(tmp_path):
    db=Database(tmp_path/'db.sqlite3'); iid,_=source(db); source(db,day='2030-10-09',url='https://example.org/other')
    before=db.rows('SELECT * FROM item_evidence'); raw=db.one('SELECT payload,status FROM items WHERE id=?',(iid,))
    view=comparison(db,iid); assert view['fact']=='closing' and len(view['cards'])==2 and not view['same_document']
    c=view['cards'][0]; assert save(db,iid,'closing',view['token'],c['key'])
    item=row(db,iid); assert not item['date_conflict'] and item['data']['date']==c['value']['date']
    assert item['personal_decisions'] and 'decided' in [t['code'] for t in item_tags(item)]
    current=datetime(2030,10,1,12,tzinfo=VIENNA); states={iid:action_state(item,current)}; events=calendar_events([item])
    ics,count=export_calendar([item],events,states); assert count==1 and 'Persönliche Entscheidung' in ics.decode('utf-8')
    projected,_=projection([item],events,states,{},'test')
    assert projected and c['value']['date'] in encode(projected) and 'Persönliche Entscheidung' in encode(projected)
    assert db.rows('SELECT * FROM item_evidence')==before and db.one('SELECT payload,status FROM items WHERE id=?',(iid,))==raw
    view=comparison(db,iid); assert not save(db,iid,'closing',view['token'],c['key'])
    # Another tab cannot overwrite a personal decision made after its preview.
    alternative=next(card for card in view['cards'] if card['key']!=c['key'])
    assert save(db,iid,'closing',view['token'],alternative['key'])
    with pytest.raises(ValueError): save(db,iid,'closing',view['token'],c['key'])
    view=comparison(db,iid)
    db.execute('UPDATE items SET overrides=? WHERE id=?',(encode({'date':'2030-10-21','time':'11:00:00'}),iid))
    with pytest.raises(ValueError): save(db,iid,'closing',view['token'],reset=True)
    fresh=comparison(db,iid); save(db,iid,'closing',fresh['token'],reset=True)
    assert row(db,iid)['data']['date']=='2030-10-21' and row(db,iid)['date_conflict']


INTERNAL='''###
Anmeldung zu Übungsgruppe
Gruppenwahl
Geöffnet:
Dienstag, 6. Oktober 2030, 19:00
Schließt:
Freitag, 16. Oktober 2030, 10:00
Sie müssen sich zu einer Gruppe bis
spätestens 9.10.2030
um
10 Uhr anmelden
###
Arbeitsblatt
Geöffnet:
Montag, 12. Oktober 2030, 00:00'''


def internal(db):
    iid,doc=source(db,text=INTERNAL,source_state='unclear',uncertainty='Widerspruch zwischen dem Schließzeitpunkt 16.10.2030 und dem Fließtext mit Frist 9.10.2030.')
    payload=json.loads(db.one('SELECT payload FROM items WHERE id=?',(iid,))['payload']); payload['quote']='Schließt:\\nFreitag, 16. Oktober 2030, 10:00'
    db.execute('UPDATE item_evidence SET quote=?,payload=? WHERE item_id=?',(payload['quote'],encode(payload),iid)); db.execute('UPDATE items SET payload=? WHERE id=?',(encode(payload),iid))
    return iid,doc


def test_internal_statements_opening_missing_and_other_fact_preserved(tmp_path):
    db=Database(tmp_path/'db.sqlite3'); iid,doc=internal(db); view=comparison(db,iid)
    assert view['same_document'] and len(view['cards'])==2 and {c['value']['date'] for c in view['cards']}=={'2030-10-16','2030-10-09'}
    item=row(db,iid); opening=item['evidence_resolution']['registration_opening']
    assert (opening['date'],opening['time'])==('2030-10-06','19:00:00') and opening['proofs'][0]['hash']==doc['hash']
    assert item['source_payload']['date']=='2030-10-16' and item['data']['date']=='2030-10-16' and item['data']['end_date'] is None
    assert comparison(db,iid,'start')['cards']==[]
    save(db,iid,'closing',view['token'],view['cards'][1]['key'])
    item=row(db,iid); assert item['data']['end_date']=='2030-10-09' and item['data']['source_state']=='active' and not item['conflict']
    assert item['status']=='open' and not item['booked_event']
    # A second independent policy conflict must not disappear on a temporal choice.
    db.execute("UPDATE item_evidence SET payload=json_set(payload,'$.obligation','optional') WHERE item_id=?",(iid,))
    source(db,day='2030-10-09',url='https://example.org/policy')
    item=row(db,iid); assert item['conflict'] and 'obligation' in item['evidence_resolution']['unresolved_fields']


def test_unchanged_new_version_not_stale_meaningful_new_version_warns(tmp_path):
    db=Database(tmp_path/'db.sqlite3'); iid,doc=internal(db); view=comparison(db,iid)
    save(db,iid,'closing',view['token'],view['cards'][0]['key'])
    def replacement(text,hash_):
        n=db.execute('INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path,pages,analysis_state) VALUES(?,?,?,?,?,?,?,?,?)',
            (doc['source_id'],doc['url'],doc['title'],doc['mime'],hash_,'2030-10-03T10:00:00+00:00','unused',encode([{'page':1,'text':text}]),'success'))
        db.execute('UPDATE item_evidence SET document_id=? WHERE item_id=?',(n,iid))
    replacement(INTERNAL+'\nNavigation geändert','new')
    assert not row(db,iid)['personal_decisions'][0]['stale']
    old=comparison(db,iid)
    replacement(INTERNAL.replace('9.10.2030','10.10.2030'),'changed')
    assert row(db,iid)['personal_decisions'][0]['stale'] and row(db,iid)['data']['end_date']=='2030-10-16'
    with pytest.raises(ValueError):save(db,iid,'closing',old['token'],old['cards'][0]['key'])


def test_routes_preview_only_one_apply_and_original_metadata(tmp_path):
    app=create_app(Settings(data_dir=tmp_path)); iid,_=internal(app.state.db); client=TestClient(app)
    page=client.get(f'/items/{iid}/compare'); assert page.status_code==200
    assert 'Veröffentlicht: nicht angegeben' in page.text and 'Abgerufen: 01.10.2030 12:01' in page.text
    assert 'Information fehlt' in client.get(f'/items/{iid}/compare?fact=start').text
    view=comparison(app.state.db,iid); before=app.state.db.rows('SELECT * FROM item_preferences')
    data={'fact':'closing','token':view['token'],'choice':view['cards'][1]['key'],'action':'preview'}
    response=client.post(f'/items/{iid}/compare',data=data,headers={'X-CSRF-Token':app.state.csrf})
    assert response.status_code==200 and 'Auswahl übernehmen' in response.text and app.state.db.rows('SELECT * FROM item_preferences')==before
    data['action']='apply'; response=client.post(f'/items/{iid}/compare',data=data,headers={'X-CSRF-Token':app.state.csrf},follow_redirects=False)
    assert response.status_code==303 and row(app.state.db,iid)['data']['end_date']=='2030-10-09'
