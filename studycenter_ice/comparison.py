"""Personal choices over retained source facts; never rewrite source assertions."""
import hashlib
import json
import re
from datetime import date, time
from typing import Literal

from pydantic import BaseModel, Field

from .db import claim_facts, encode, now

FACTS = {'closing': 'Wann endet die Anmeldung?', 'opening': 'Wann beginnt die Anmeldung?',
         'start': 'Wann beginnt der Termin?', 'end': 'Wann endet der Termin?',
         'obligation': 'Welche Pflichtangabe gilt für dich?'}


class SourceDecision(BaseModel):
    item_id: int
    fact: Literal['opening', 'closing', 'start', 'end', 'obligation']
    value: dict
    proofs: list[dict] = Field(default_factory=list)
    at: str
    reason: str = Field(default='', max_length=500)
    basis: str
    mode: Literal['source', 'manual'] = 'source'


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def normalized(text):
    return ' '.join(text.replace('\\n', '\n').split())


def dated(text):
    numeric = re.search(r'(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(20\d{2})', text)
    months = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember']
    named = re.search(r'(\d{1,2})\.\s*('+'|'.join(months)+r')\s+(20\d{2})', text, re.I)
    match = numeric or named
    if not match: return None
    try:
        day = date(int(match[3]), int(match[2]) if numeric else next(i+1 for i,m in enumerate(months) if m.casefold()==match[2].casefold()), int(match[1])).isoformat()
        clock = re.search(r'(\d{1,2}):(\d{2})|\b(\d{1,2})\s+Uhr', text[match.end():])
        return {'date': day, 'time': time(int(clock[1] or clock[3]), int(clock[2] or 0)).isoformat() if clock else None}
    except ValueError: return None


def registration_statements(value, proof, text):
    """Only a source-bound registration section, never an unrelated quiz window."""
    if value.get('kind')!='registration': return []
    quote = normalized(proof['quote'])
    # Preserve actual whitespace in displayed snippets; model quotes can contain literal \\n.
    compact = normalized(text)
    if not quote or quote not in compact: return []
    # Scope to the nearest actual heading around this retained quote.
    position = text.find(proof['quote'].replace('\\n', '\n'))
    if position < 0:
        position = next((m.start() for m in re.finditer(r'Schließt\s*:', text) if normalized(text[m.start():m.start()+len(quote)+60]).startswith(quote)), -1)
    if position < 0: return []
    start = text.rfind('###', 0, position)
    end = text.find('###', position)
    section = text[max(0,start):end if end>=0 else len(text)]
    if len(section)>6000 or not re.search(r'Anmeldung|Gruppenwahl', section, re.I): return []
    result=[]
    for label, fact, pattern in [
        ('Schließfeld', 'closing', r'Schließt\s*:\s*[^\n]+'),
        ('Öffnungsfeld', 'opening', r'Geöffnet\s*:\s*[^\n]+'),
        ('Fließtext', 'closing', r'Sie\s+müssen\s+sich\s+zu\s+einer\s+Gruppe\s+bis\s+spätestens\s+\d{1,2}\.\s*\d{1,2}\.\s*20\d{2}\s+um\s+\d{1,2}(?::\d{2})?\s+Uhr\s+anmelden')]:
        for match in re.finditer(pattern, section, re.I):
            value=dated(match[0])
            if value: result.append((fact,value,match[0],label))
    return result


def options(item, proofs, lookup):
    result=[]
    for p in proofs:
        claim=json.loads(p['payload']); facts=claim_facts(claim)
        for fact in FACTS:
            if fact=='obligation':
                value={'obligation':facts['obligation']} if 'obligation' in facts else None
            else:
                day=facts.get(fact+'_date'); clock=facts.get(fact+'_time')
                value={'date':day,'time':clock[1] if clock else None} if day else None
            if value:
                result.append(card(p,fact,value,p['quote'],'Quellenangabe'))
        if claim.get('kind')=='registration':
            for fact,value,quote,label in registration_statements(claim,p,lookup(p['document_id'],p['page'])):
                result.append(card(p,fact,value,quote,label))
    # Prefer the literal original snippet over a model's equivalent escaped quote.
    unique={}
    for c in result:
        key=(c['document_id'],c['page'],c['fact'],encode(c['value']))
        if key not in unique or c['label']!='Quellenangabe': unique[key]=c
    return list(unique.values())


def card(p,fact,value,quote,label):
    result={k:p.get(k) for k in ('document_id','page','hash','revision','published_at','fetched_at','mime','kind','title','url')}
    result.update(fact=fact,value=value,quote=quote,label=label)
    result['key']=digest([fact,value,p.get('source_id'),p.get('url'),normalized(quote)])
    return result


def basis(cards,fact):
    return digest(sorted({c['key'] for c in cards if c['fact']==fact}))


def project_value(data,fact,value):
    data=dict(data)
    if fact=='obligation': data['obligation']=value['obligation']; return data
    closing=data.get('date_meaning') in ('window','deadline')
    if fact=='opening':
        if closing and not data.get('end_date') and data.get('date'):
            data['end_date'],data['end_time']=data['date'],data.get('time')
        data.update(date=value['date'],time=value.get('time'),date_meaning='window')
    elif fact=='closing':
        if closing and data.get('end_date'): data.update(end_date=value['date'],end_time=value.get('time'))
        else: data.update(date=value['date'],time=value.get('time'),date_meaning='window' if data['kind']=='registration' else 'deadline')
    elif fact=='start': data.update(date=value['date'],time=value.get('time'),date_meaning='start')
    elif fact=='end': data.update(end_date=value['date'],end_time=value.get('time'))
    return data


def project(item,proofs,lookup,profile):
    decisions=profile.get('source_decisions',{})
    detail=item.setdefault('evidence_resolution',{})
    needs_registration=bool(detail.get('internal_conflict') and item['data'].get('kind')=='registration')
    if not decisions and not needs_registration: return
    cards=options(item,proofs,lookup)
    # A uniquely quoted opening remains independent of a disputed closing.
    openings=[c for c in cards if c['fact']=='opening']
    if openings and len({encode(c['value']) for c in openings})==1:
        detail['registration_opening']=openings[0]['value']|{'proofs':openings}
        if decisions and item['data'].get('kind')=='registration' and not item['data'].get('end_date') and any(f in decisions for f in ('opening','closing')):
            item['data']=project_value(item['data'],'opening',openings[0]['value'])
    temporal_only=bool(detail.get('internal_unresolved_fields')) and all(f.startswith(('closing_','opening_')) for f in detail['internal_unresolved_fields']) and not detail.get('unresolved_fields')
    detail['temporal_source_uncertainty']=temporal_only
    active=[]; resolved=[]
    for fact,raw in decisions.items():
        try: decision=SourceDecision.model_validate(raw)
        except ValueError: continue
        if decision.item_id!=item['id'] or decision.fact!=fact: continue
        stale=decision.basis!=basis(cards,fact)
        item['data']=project_value(item['data'],fact,decision.value)
        if fact=='opening': detail['registration_opening']=decision.value|{'proofs':decision.proofs,'personal':True}
        active.append(decision.model_dump()|{'stale':stale})
        resolved.extend([fact] if fact=='obligation' else [fact+'_date',fact+'_time'])
    item['personal_decisions']=active
    detail['personal_resolved_fields']=resolved
    detail['unresolved_fields']=[f for f in detail.get('unresolved_fields',[]) if f not in resolved]
    internal=detail.get('internal_unresolved_fields',[])
    detail['internal_unresolved_fields']=[f for f in internal if f not in resolved]
    detail['internal_conflict']=bool(detail['internal_unresolved_fields']) if internal else detail.get('internal_conflict',False)
    if resolved:
        item['date_conflict']=any(f.endswith(('_date','_time')) for f in detail['unresolved_fields']+detail['internal_unresolved_fields']) or bool(detail['internal_conflict'] and not internal)
        item['conflict']=bool(detail['unresolved_fields'] or detail['internal_conflict'] or item.get('personal_resolution',{}).get('issues'))
        if not item['conflict'] and item['data'].get('source_state')=='unclear' and internal and 'source_state' not in detail['unresolved_fields']:
            # The retained source's sole uncertainty was this temporal contradiction.
            item['data']['source_state']='active'
        if not item['conflict']: item['data']['uncertainty']=None
    # Later manual edits always win. Resetting a decision cannot remove those edits.
    item['data'].update(json.loads(item['overrides']))


def comparison(db,item_id,fact=None,connection=None):
    if connection is None:
        with db.connection() as con:
            con.execute('BEGIN')
            return comparison(db,item_id,fact,con)
    items=db._items(False,connection) if connection else db.items()
    item=next((i for i in items if i['id']==item_id),None)
    if item is None: raise LookupError('Eintrag nicht gefunden')
    proofs=db.rows('''SELECT e.*,d.title,d.hash,d.revision,d.url,d.mime,d.published_at,d.fetched_at,d.source_id,s.kind
        FROM item_evidence e JOIN documents d ON d.id=e.document_id JOIN sources s ON s.id=d.source_id
        WHERE e.item_id=? AND e.current=1 ORDER BY e.id''',(item_id,),connection=connection)
    cards=options(item,proofs,db._evidence_context(connection))
    available=[f for f in FACTS if any(c['fact']==f for c in cards)]
    detail=item.get('evidence_resolution',{})
    unresolved=detail.get('unresolved_fields',[])+detail.get('internal_unresolved_fields',[])
    default=next((f for f in FACTS if f in unresolved or any(x.startswith(f+'_') for x in unresolved)),None)
    fact=fact or default or ('closing' if item['data']['kind']=='registration' else 'start')
    if fact not in FACTS: raise ValueError('Ungültiger Sachverhalt')
    selected=[c for c in cards if c['fact']==fact]
    decision=next((d for d in item.get('personal_decisions',[]) if d['fact']==fact),None)
    return {'item':item,'fact':fact,'question':FACTS[fact],'available':available,'cards':selected,
            'same_document':len({c['document_id'] for c in selected})==1,'basis':basis(cards,fact),
            'token':digest([basis(cards,fact),item['overrides'],decision]),
            'decision':decision}


def save(db,item_id,fact,token,choice=None,manual=None,reason='',reset=False):
    with db.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        view=comparison(db,item_id,fact,con)
        if view['token']!=token: raise ValueError('Die Beleglage oder deine Korrektur hat sich geändert. Bitte neu vergleichen.')
        row=con.execute('SELECT value FROM item_preferences WHERE item_id=?',(item_id,)).fetchone()
        profile=json.loads(row['value']) if row else {}
        decisions=profile.setdefault('source_decisions',{})
        if reset:
            if fact not in decisions: return False
            del decisions[fact]
        else:
            selected=next((c for c in view['cards'] if c['key']==choice),None)
            if manual is None and selected is None: raise ValueError('Die ausgewählte Fundstelle ist nicht mehr aktuell.')
            value=manual if manual is not None else selected['value']
            if fact=='obligation':
                if value.get('obligation') not in ('required','recommended','optional','unclear'): raise ValueError('Pflichtangabe ungültig')
            else:
                value={'date':date.fromisoformat(value['date']).isoformat(),'time':time.fromisoformat(value['time']).isoformat() if value.get('time') else None}
            from .models import Candidate
            Candidate.model_validate(project_value(view['item']['data'],fact,value))
            decision=SourceDecision(item_id=item_id,fact=fact,value=value,proofs=[selected] if selected else [],
                at=now(),reason=reason.strip(),basis=view['basis'],mode='manual' if manual is not None else 'source').model_dump()
            old=decisions.get(fact,{})
            if all(old.get(k)==decision[k] for k in ('item_id','fact','value','reason','basis','mode')): return False
            decisions[fact]=decision
        con.execute('INSERT INTO item_preferences(item_id,value) VALUES(?,?) ON CONFLICT(item_id) DO UPDATE SET value=excluded.value',(item_id,encode(profile)))
        return True
