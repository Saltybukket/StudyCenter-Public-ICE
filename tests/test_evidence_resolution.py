import json
import hashlib

from studycenter_ice.db import Database, claims_conflict, effective_evidence, encode
from studycenter_ice.models import Candidate


def claim(**changes):
    return Candidate(title='A1',kind='task',identity_key='A1',identity_scope='course',
                     date='2026-10-27',date_meaning='deadline',quote='A1 bis 27. Oktober',
                     page=1,anchor='A1',**changes).model_dump(mode='json')


def proof(value, document=1, published=None):
    return {'payload':encode(value),'document_id':document,'page':value['page'],
            'quote':value['quote'],'published_at':published}


def test_release_span_and_closing_only_deadline_share_one_fact():
    deadline=claim(time='23:59:00',obligation='required')
    span=deadline|{'date':'2026-10-12','time':None,'end_date':'2026-10-27',
                   'end_time':None,'quote':'Ausgabe AB1'}
    assert not claims_conflict([deadline,span])
    selected,conflict,date_conflict,detail=effective_evidence([proof(deadline),proof(span)],deadline)
    assert (selected['date'],selected['end_date'],selected['end_time'])==('2026-10-12','2026-10-27','23:59:00')
    assert not conflict and not date_conflict
    assert 'closing_time' in detail['facts']
    assert claims_conflict([deadline,span|{'end_date':'2026-10-28'}])
    assert claims_conflict([deadline,deadline|{'time':'12:00:00'}])


def test_disputed_opening_retains_valid_closing_only_payload():
    first=claim()|{'date':'2026-10-12','time':'08:00:00','end_date':'2026-10-27','end_time':'23:59:00'}
    other=first|{'date':'2026-10-13'}
    selected,conflict,date_conflict,_=effective_evidence([proof(first,1),proof(other,2)],first)
    assert conflict and date_conflict
    assert (selected['date'],selected['time'],selected['end_date'],selected['end_time'])==('2026-10-27','23:59:00',None,None)
    Candidate(**selected)  # No dangling end clock without an end date.


def test_latest_source_statement_per_fact_keeps_older_independent_precision():
    old=claim(time='10:00:00',obligation='required')
    latest=old|{'date':'2026-10-29','time':None,'is_update':False,'obligation':'unclear'}
    selected,conflict,date_conflict,detail=effective_evidence([
        proof(old,1,'2026-10-10'),proof(latest,2,'2026-10-12')],old)
    assert selected['date']=='2026-10-29' and selected['time'] is None
    assert selected['obligation']=='required' and not conflict and not date_conflict
    assert detail['superseded_fields']==['closing_date']
    assert detail['facts']['closing_date'][0]['document_id']==2


def test_fetch_order_unknown_publication_ties_and_governing_reference_stay_open():
    old=claim()
    new=old|{'date':'2026-10-29'}
    for first,second in [(None,None),('2026-10-12','2026-10-12'),('2026-10-12',None)]:
        selected,conflict,date_conflict,_=effective_evidence([proof(old,1,first),proof(new,2,second)],old)
        assert conflict and date_conflict
    referenced=new|{'description':'Die Termine im Laborkalender sind maßgeblich.'}
    assert effective_evidence([proof(old,1,'2026-10-10'),proof(referenced,2,'2026-10-12')],old)[1]
    authoritative=proof(old,1,'2026-10-10')|{'title':'Laborkalender'}
    selected,conflict,_,_=effective_evidence([authoritative,proof(referenced,2,'2026-10-12')],old)
    assert not conflict and selected['date']==old['date']
    # Contradictory claims in the same newest source cannot supersede one another.
    assert effective_evidence([proof(old,2,'2026-10-12'),proof(new,2,'2026-10-12')],old)[1]


def test_latest_source_internal_registration_contradiction_remains_visible():
    value=claim()|{'kind':'registration','date_meaning':'window',
        'uncertainty':'Widerspruch zwischen dem Schließzeitpunkt 16.10. und dem Fließtext mit Frist 9.10.'}
    _,conflict,date_conflict,detail=effective_evidence([proof(value)],value)
    assert conflict and date_conflict and detail['internal_conflict']


def test_newer_explicit_deadline_clears_only_the_matching_older_internal_ambiguity():
    older=claim()|{'uncertainty':'Widerspruch zwischen den Fristen 27. Oktober und 29. Oktober.'}
    latest=older|{'date':'2026-10-29','uncertainty':None}
    assert not effective_evidence([proof(older,1,'2026-10-10'),proof(latest,2,'2026-10-12')],older)[1]
    same_date=latest|{'date':older['date']}
    selected,conflict,_,detail=effective_evidence([proof(older,1,'2026-10-10'),proof(same_date,2,'2026-10-12')],older)
    assert not conflict and selected['uncertainty'] is None and detail['superseded_internal_conflicts'][0]['document_id']==1
    assert effective_evidence([proof(older,1,None),proof(latest,2,'2026-10-12')],older)[1]
    ambiguous_clock=older|{'time':'10:00:00','uncertainty':'Widerspruch zwischen den Schließzeiten 10:00 und 12:00.'}
    assert effective_evidence([proof(ambiguous_clock,1,'2026-10-10'),proof(latest,2,'2026-10-12')],older)[1]
    latest['time']='12:00:00'
    assert not effective_evidence([proof(ambiguous_clock,1,'2026-10-10'),proof(latest,2,'2026-10-12')],older)[1]
    latest['uncertainty']='Widersprüchliche Schließzeitpunkte im aktuellen Original.'
    assert effective_evidence([proof(older,1,'2026-10-10'),proof(latest,2,'2026-10-12')],older)[1]


def test_mirrored_teachcenter_policy_interpretation_is_unclear_without_erasing_different_context():
    quote='the necessary preparation steps, which should be done before the actual setup session'
    recommended=claim()|{'date':None,'date_meaning':'none','quote':quote,'obligation':'recommended'}
    required=recommended|{'obligation':'required'}
    left=proof(recommended,1)|{'source_id':7,'kind':'teachcenter','url':'https://tc.tugraz.at/main/course/view.php?id=1'}
    right=proof(required,2)|{'source_id':7,'kind':'teachcenter','url':'https://tc.tugraz.at/main/course/section.php?id=2'}
    sentence='More information about '+quote+' can be found in the Administrative Information and Setup document.'
    read=lambda document_id,page:sentence
    selected,conflict,_,detail=effective_evidence([left,right],required,context_lookup=read)
    assert not conflict and selected['obligation']=='unclear'
    assert 'Pflichtgrad unklar' in selected['uncertainty']
    assert detail['extraction_ambiguities'][0]['document_ids']==[1,2]
    different=lambda document_id,page:sentence if document_id==1 else 'Mandatory preparation: '+sentence
    assert effective_evidence([left,right],required,context_lookup=different)[1]
    different=lambda document_id,page:sentence if document_id==1 else 'Preparation is mandatory. '+sentence
    assert effective_evidence([left,right],required,context_lookup=different)[1]
    explicit=required|{'quote':'Preparation is mandatory for every participant.'}
    assert effective_evidence([left,proof(explicit,2)|{k:right[k] for k in ('source_id','kind','url')}],explicit,context_lookup=read)[1]
    unrelated=right|{'source_id':8}
    assert effective_evidence([left,unrelated],required,context_lookup=read)[1]


def test_old_hash_recrawl_cannot_reverse_newer_same_url_and_projection_preserves_state(tmp_path):
    from studycenter_ice.config import claim_data_directory
    claim_data_directory(tmp_path)
    db=Database(tmp_path/'data.sqlite3')
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('MT','2026W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Quelle','https://example.org')",(cid,))
    def document(digest,published=None):
        identifier=db.execute('''INSERT INTO documents(source_id,url,title,mime,hash,published_at,fetched_at,original_path)
            VALUES(?,'https://example.org/a1','A1','text/plain',?,?,'2026-10-01T00:00:00+00:00','unused')''',
            (sid,hashlib.sha256(digest.encode()).hexdigest(),published))
        return db.one('SELECT * FROM documents WHERE id=?',(identifier,))
    older,newer=document('old'),document('new')
    first=claim()
    identifier=db.ingest(older,Candidate(**first))
    db.execute("UPDATE items SET status='done',pinned=1,overrides=? WHERE id=?",(encode({'date':'2026-11-01'}),identifier))
    db.ingest(newer,Candidate(**(first|{'date':'2026-10-29'})))
    db.ingest(older,Candidate(**first))  # Delayed analysis carries its original observation.
    assert db.one('SELECT current FROM item_evidence WHERE document_id=?',(older['id'],))['current']==0
    before={table:db.rows('SELECT * FROM '+table) for table in ('items','item_evidence','documents')}
    projected=db.items()[0]
    assert projected['source_payload']['date']=='2026-10-29'
    assert projected['data']['date']=='2026-11-01' and projected['status']=='done' and projected['pinned']
    assert before=={table:db.rows('SELECT * FROM '+table) for table in before}
    assert len(json.loads(db.one('SELECT history FROM items WHERE id=?',(identifier,))['history']))==1
    # A new fetch of the exact URL can genuinely revert to its previously seen bytes.
    from studycenter_ice.config import Settings
    from studycenter_ice.documents import DocumentStore
    store=DocumentStore(db,Settings(data_dir=tmp_path))
    refetched=store.save(sid,older['url'],'A1','text/plain',b'old')
    assert refetched==older['id']
    db.ingest(db.one('SELECT * FROM documents WHERE id=?',(refetched,)),Candidate(**first))
    projected=db.items()[0]
    assert projected['source_payload']['date']=='2026-10-27'
    assert projected['data']['date']=='2026-11-01' and projected['status']=='done' and projected['pinned']
    assert db.one('SELECT current FROM item_evidence WHERE document_id=?',(newer['id'],))['current']==0
    assert len(db.rows('SELECT * FROM item_evidence'))==2
    # Known publication order still protects a newer statement after an old mail/hash is refetched.
    db.execute("UPDATE documents SET published_at='2026-10-10' WHERE id=?",(older['id'],))
    db.execute("UPDATE documents SET published_at='2026-10-12' WHERE id=?",(newer['id'],))
    db.ingest(db.one('SELECT * FROM documents WHERE id=?',(newer['id'],)),Candidate(**(first|{'date':'2026-10-29'})))
    store.save(sid,older['url'],'A1','text/plain',b'old')
    db.ingest(db.one('SELECT * FROM documents WHERE id=?',(older['id'],)),Candidate(**first))
    assert db.items()[0]['source_payload']['date']=='2026-10-29'
    # Own notes are edited in their payload, not re-derived from import evidence.
    db.execute("UPDATE items SET identity='note:personal',payload=?,overrides='{}' WHERE id=?",
               (encode(first|{'date':'2026-11-02'}),identifier))
    assert db.items()[0]['data']['date']=='2026-11-02'


def test_projection_reinterprets_stale_conflicts_without_rewriting_evidence(tmp_path):
    db=Database(tmp_path/'data.sqlite3')
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('MT','2026W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Quelle','https://example.org')",(cid,))
    doc_id=db.execute('''INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path)
        VALUES(?,'https://example.org/a1','A1','text/plain','hash','2026-10-14','unused')''',(sid,))
    doc=db.one('SELECT * FROM documents WHERE id=?',(doc_id,))
    value=claim(obligation='required')
    identifier=db.ingest(doc,Candidate(**value))
    db.ingest(doc,Candidate(**(value|{'date':'2026-10-12','end_date':'2026-10-27','quote':'Ausgabe AB1'})))
    db.execute('UPDATE items SET conflict=1 WHERE id=?',(identifier,))
    before=db.rows('SELECT * FROM item_evidence')
    projected=db.items()[0]
    assert not projected['conflict'] and not projected['date_conflict']
    assert projected['data']['date']=='2026-10-12' and projected['data']['end_date']=='2026-10-27'
    assert before==db.rows('SELECT * FROM item_evidence')
