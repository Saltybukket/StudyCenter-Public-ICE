"""One persistent local reminder planner/worker, independent from source sync and browsers."""
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, time, timezone

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from .config import VIENNA
from .db import encode, now
from .presentation import attendance_type, personal_state


class ReminderSettings(BaseModel):
    enabled: bool = True
    exam: bool = True
    exercise: bool = True
    lecture: bool = True
    task: bool = True
    registration: bool = True
    exam_minutes: list[int] = Field(default_factory=lambda:[60,30],max_length=5)
    exercise_minutes: list[int] = Field(default_factory=lambda:[30],max_length=5)
    lecture_minutes: list[int] = Field(default_factory=lambda:[30],max_length=5)
    task_minutes: list[int] = Field(default_factory=lambda:[1440,120],max_length=5)
    registration_minutes: list[int] = Field(default_factory=lambda:[10,0],max_length=5)
    sound: bool = True
    unknown_task: bool = False
    unknown_events: bool = False
    evening: time = time(18,0)
    previous_day: time = time(9,0)
    quiet: bool = False
    quiet_start: time = time(22,0)
    quiet_end: time = time(8,0)

    @field_validator('exam_minutes','exercise_minutes','lecture_minutes','task_minutes')
    @classmethod
    def minutes(cls,value):
        if not value or any(n<=0 or n>43200 for n in value):raise ValueError('Ein bis fünf Vorläufe zwischen 1 und 43200 Minuten wählen.')
        return sorted(set(value),reverse=True)

    @field_validator('registration_minutes')
    @classmethod
    def opening_minutes(cls,value):
        if not value or any(n<0 or n>43200 for n in value):raise ValueError('Anmeldestart: ein bis fünf Zeiten zwischen 0 und 43200 Minuten wählen; 0 bedeutet zum Beginn.')
        return sorted(set(value),reverse=True)


class ItemPreferences(BaseModel):
    mode: str = 'standard'
    minutes: list[int] = Field(default_factory=list,max_length=5)
    attendance: str = ''
    personal: bool = False
    at_closing: bool = False

    @field_validator('mode')
    @classmethod
    def mode_choice(cls,v):
        if v not in ('standard','off','custom'):raise ValueError('Standard, aus oder eigene Zeiten wählen.')
        return v

    @field_validator('attendance')
    @classmethod
    def attendance_choice(cls,v):
        if v not in ('','exercise','lecture'):raise ValueError('Anwesenheit als Übung oder Vorlesung festlegen.')
        return v

    @field_validator('minutes')
    @classmethod
    def own_minutes(cls,v,info:ValidationInfo):
        minimum=0 if (info.context or {}).get('registration_start') else 1
        if any(n<minimum or n>43200 for n in v):raise ValueError('Ungültige Vorlaufzeit; 0 ist nur beim Anmeldestart möglich.')
        return sorted(set(v),reverse=True)


def utc(value):return value.astimezone(timezone.utc).isoformat()


def rule_label(rule):
    if rule=='voice:exact':return 'Persönliche Erinnerung'
    if rule=='window:exact':return 'Quiz-/Prüfungsfenster · Abgabeschluss'
    if rule.startswith('window:'):return 'Quiz-/Prüfungsfenster · '+rule.split(':',1)[1]+' min vor Abgabeschluss'
    category,offset=rule.split(':',1)
    if category=='registration':return 'Anmeldestart · zum Beginn' if offset=='0' else 'Anmeldestart · '+offset+' min Vorlauf'
    name={'exam':'Prüfung','exercise':'Übung','lecture':'Vorlesung','task':'Abgabe'}.get(category,category)
    return name+' · persönliche Vortagsmeldung' if offset=='previous-day' else name+' · '+(str(int(offset)//60)+' h' if int(offset)%60==0 else offset+' min')+' Vorlauf'


class NativeToast:
    def send(self,title,message,url,tag,sound):
        if sys.platform!='win32':raise RuntimeError('Native Desktopmeldungen benötigen Windows.')
        from windows_toasts import WindowsToaster,Toast,ToastAudio,AudioSource
        toaster=WindowsToaster('StudyCenter-ICE')
        toast=Toast()
        toast.text_fields=[title,message]
        toast.tag=tag[:16];toast.group='StudyCenter-ICE';toast.launch_action=url
        toast.audio=ToastAudio(AudioSource.Reminder,silent=not sound)
        toaster.show_toast(toast)
        # Windows controls suppression/Do Not Disturb. Accepted by WinRT is not human confirmation.


class ReminderService:
    def __init__(self,db,timing,adapter=None):
        self.db,self.timing,self.adapter=db,timing,adapter or NativeToast()
        self.error='';self.running=False
        self.stop=asyncio.Event()

    def settings(self):
        row=self.db.one("SELECT value FROM app_preferences WHERE name='notifications'")
        return ReminderSettings.model_validate_json(row['value']) if row else ReminderSettings()

    def registration_opening(self,item,related_items=None):
        """Use the typed registration span and resolved opening facts, never an exam/quiz time."""
        value=item['data'];source=item.get('source_payload',value)
        resolution=item.get('evidence_resolution',{})
        opening_record=resolution.get('registration_opening') or {}
        has_opening_record=bool(opening_record.get('date') and (opening_record.get('proofs') or opening_record.get('personal')))
        unresolved=set(resolution.get('unresolved_fields',[])) | set(resolution.get('internal_unresolved_fields',[]))
        unresolved-=set(resolution.get('personal_resolved_fields',[]))
        key=source.get('identity_key') or ''
        if value.get('date_meaning')!='window' or key.endswith(':withdrawal'):
            return None,['Kein belegter Anmeldebeginn; Abmeldung und Quizöffnung sind eigene Sachverhalte.']
        if not has_opening_record and (not source.get('date') or not source.get('end_date')):
            return None,['Anmeldebeginn nicht eindeutig strukturiert belegt; Originalfundstellen prüfen.']
        if item.get('booked_event') or (value.get('group') and value['group'] in item.get('confirmed_groups',[])):
            return None,['Anmeldung bereits durch persönliche Buchungs-/Gruppenbelege bestätigt.']
        exam_key=key[:-len(':registration')] if key.startswith('tugrazonline:exam:') and key.endswith(':registration') else None
        linked=None
        if exam_key:
            rows=related_items if related_items is not None else self.db.items(active_only=True)
            linked=next((i for i in rows if i.get('course_id')==item.get('course_id') and i.get('source_payload',i['data']).get('identity_key')==exam_key),None)
            # The adapter's exact structured offer keeps its official status in the original claim.
            if ((source.get('description') or '').startswith('Bereits angemeldet.')
                    and 'Bereits angemeldet' in source.get('quote','') and 'Nicht angemeldet' not in source.get('quote','')) or linked and linked.get('booked_event'):
                return None,['Prüfungsanmeldung bereits offiziell belegt.']
            if linked and (linked.get('ignored') or linked.get('reminder_profile',{}).get('mode')=='off' or linked['data'].get('source_state')!='active'):
                return None,['Verknüpftes Prüfungsangebot abgewählt oder aufgehoben.']
        watched=personal_state(item)['personally_planned'] or bool(linked and personal_state(linked)['personally_planned'])
        if (exam_key or value.get('obligation')!='required' or 'obligation' in unresolved) and not watched:
            return None,['Anmeldeangebot: erst diesen Eintrag oder die zugehörige Prüfung persönlich auswählen / beobachten.']
        if not item.get('course_id') and not watched:
            return None,['Keine aktive persönliche Kurszuordnung oder persönliche Auswahl.']
        overrides=json.loads(item.get('overrides','{}'))
        opening_overrides=overrides if source.get('end_date') or value.get('end_date') else {}
        issues=[]
        if item.get('date_conflict'):
            opening_conflict=unresolved & {'opening_date','opening_time'}
            if opening_conflict and not all({'opening_date':'date','opening_time':'time'}[f] in opening_overrides for f in opening_conflict):
                return None,['Anmeldebeginn widersprüchlich: Originalfundstellen vergleichen oder persönlich klären.']
            known_opening=has_opening_record or {'opening_date','opening_time'} <= resolution.get('facts',{}).keys()
            closing_only=bool(unresolved) and unresolved <= {'closing_date','closing_time'} and known_opening
            if resolution.get('internal_conflict') and 'internal_unresolved_fields' not in resolution:
                closing_only=False  # No role metadata: do not guess which fact an internal conflict affects.
            if not opening_conflict and not closing_only and not {'date','time'} <= opening_overrides.keys():
                return None,['Anmeldebeginn nicht unabhängig geklärt: Originalfundstellen vergleichen.']
            issues.append('Die Öffnung ist unabhängig geklärt; andere widersprüchliche Angaben bleiben offen.')
        # A single-date item's ordinary time correction concerns its closing.
        # Only an actual span maps date/time to opening; use the role projection otherwise.
        if has_opening_record:
            start=(opening_record['date'],opening_record.get('time'))
            if source.get('end_date') or value.get('end_date'):
                start=(overrides.get('date',start[0]),overrides.get('time',start[1]))
        else:start=(value.get('date'),value.get('time'))
        if ('date' in overrides or 'time' in overrides) and (not has_opening_record or source.get('end_date') or value.get('end_date')):issues.append('Anmeldebeginn mit persönlicher Zeitkorrektur; keine geänderte Quellenzeit.')
        if opening_record.get('personal'):issues.append('Persönlich gewählter Anmeldebeginn; keine offizielle Buchung oder geänderte Originalquelle.')
        return start,issues

    def plans(self,item,current,manual=None,*,settings=None,related_items=None):
        settings=settings if settings is not None else self.settings()
        value=item['data'];opening=value['kind']=='registration' and not manual
        profile=ItemPreferences.model_validate(item.get('reminder_profile',{}),context={'registration_start':value['kind']=='registration'})
        issues=[];jobs=[]
        if item.get('calendar_duplicate_of') and not manual:return [],['Dieselbe Einheit wird am erhaltenen ursprünglichen Eintrag erinnert.']
        temporal_unclear=opening and value.get('source_state')=='unclear' and item.get('evidence_resolution',{}).get('temporal_source_uncertainty')
        if not settings.enabled or profile.mode=='off' or item['ignored'] or value.get('source_state')!='active' and not temporal_unclear or item['status']=='done' and not opening:return [],['Deaktiviert, abgesagt, ignoriert oder erledigt.']
        if not opening and item.get('date_conflict') and not {'date','time','end_date','end_time'} & json.loads(item.get('overrides','{}')).keys():return [],['Widersprüchliche Zielzeit: Quellen oder persönliche Zeit klären.']
        if item.get('course_id') and not item.get('course_active'):return [],['Historischer / inaktiver Kurs.']
        if manual:
            if manual['delivered_at']:return [],['Persönliche Erinnerung bereits ausgegeben.']
            target=datetime.fromisoformat(manual['due_at']).astimezone(VIENNA)
            jobs.append((target,target,'voice:exact'))
        else:
            own=item.get('identity','').startswith('note:')
            choice=personal_state(item)
            personal,offered=choice['personal'],choice['offered']
            closing_choice=profile.at_closing and value['kind']=='exam' and value.get('date_meaning')=='window'
            if offered and value['kind'] in ('exam','event') and not closing_choice:return [],['Angebotener Termin, keine konkrete persönliche Buchung.']
            category=None
            when,clock,closing=self.timing(value)
            if opening:
                start,issues=self.registration_opening(item,related_items)
                if start is None:return [],issues
                category='registration';when,clock=start
            elif closing_choice:
                if not value.get('end_date') or not value.get('end_time'):return [],['Fensterschluss ohne belegte Uhrzeit: keine genaue Schluss-Erinnerung möglich.']
                category='exam'
                issues.append('Frei gewählte Schluss-Erinnerung; keine Einzelpflicht oder bestätigte Teilnahme.')
            elif value['kind']=='task' and closing and (own or value.get('obligation')=='required' or profile.personal):
                category='task'
            elif value['kind']=='exam' and value.get('date_meaning')=='start' and personal:
                category='exam';when,clock=value.get('date'),value.get('time')
            elif value['kind']=='event' and value.get('date_meaning')=='start' and attendance_type(item):
                if not personal:return [],['Persönliche Zuordnung offen; angebotene Slots werden nicht erinnert.']
                if re.search(r'\blab\b|setup.session|\bslot\b',value.get('title',''),re.I) and not (own or profile.personal or item.get('booked_event')):return [],['Konkrete persönliche Slotbuchung fehlt; ein Kurskalender oder Fixplatz genügt nicht.']
                category=attendance_type(item);when,clock=value.get('date'),value.get('time')
            if not category:return [],['Keine passende persönliche Abgabe, Prüfung oder belegte Anwesenheitspflicht.']
            if not getattr(settings,category):return [],['Kategorie ausgeschaltet.']
            if not when:return [],['Datum fehlt.']
            if not opening and value.get('group') and item.get('course_group') and value['group']!=item['course_group']:return [],['Andere Übungsgruppe.']
            if not clock:
                if opening:return [],issues+['Anmeldebeginn nur als Datum bekannt: keine genaue Vorwarnung. Eine persönliche Uhrzeit kann separat festgelegt werden.']
                enabled=settings.unknown_task if category=='task' else settings.unknown_events
                if not enabled:return [],['Uhrzeit nicht genannt: Vortagsregel optional aktivieren; keine genaue Vorwarnung möglich.']
                target=datetime.fromisoformat(when).replace(tzinfo=VIENNA)
                due=datetime.combine(target.date()-timedelta(days=1),settings.previous_day if category=='task' else settings.evening,VIENNA)
                jobs.append((target,due,category+':previous-day'))
                issues.append('Nur Datum bekannt. Vortagsmeldung ist deine Benachrichtigungszeit, keine Quellenfrist.')
            else:
                target=datetime.combine(datetime.fromisoformat(when).date(),time.fromisoformat(clock),VIENNA)
                if target.replace(fold=0).utcoffset()!=target.replace(fold=1).utcoffset():return [],['Uhrzeit in Zeitumstellung; eindeutige persönliche Zeit festlegen.']
                offsets=(profile.minutes if profile.mode=='custom' else [0]) if closing_choice else profile.minutes if profile.mode=='custom' else getattr(settings,category+'_minutes')
                if not offsets:return [],['Eigene Vorlaufzeiten fehlen.']
                jobs=[(target,(target.astimezone(timezone.utc)-timedelta(minutes=n)).astimezone(VIENNA),('window:exact' if n==0 else 'window:'+str(n)) if closing_choice else category+':'+str(n)) for n in offsets]
        result=[]
        for target,due,rule in jobs:
            signature=encode([item['id'],item.get('identity'),utc(target),utc(due),rule])
            identifier=hashlib.sha256(signature.encode()).hexdigest()
            result.append({'id':identifier,'item_id':item['id'],'target_at':utc(target),'due_at':utc(due),'rule':rule,'target_precision':'date' if rule.endswith(':previous-day') else 'datetime'})
        return result,issues

    def reconcile(self,current):
        from .personal import reconcile_relative
        reconcile_relative(self.db)  # Stored rules/units only; never a source request.
        rows=self.db.items(active_only=True)
        manual={r['item_id']:r for r in self.db.rows('SELECT * FROM personal_reminders')}
        settings=self.settings()
        expected=[p for item in rows for p in self.plans(item,current,manual.get(item['id']),settings=settings,related_items=rows)[0]]
        with self.db.connection() as con:
            wanted={p['id'] for p in expected}
            for row in con.execute("SELECT id FROM reminder_jobs WHERE state IN ('pending','error')").fetchall():
                if row['id'] not in wanted:con.execute("UPDATE reminder_jobs SET state='cancelled',updated_at=? WHERE id=?",(now(),row['id']))
            for p in expected:
                con.execute('INSERT OR IGNORE INTO reminder_jobs(id,item_id,target_at,due_at,rule,updated_at) VALUES(?,?,?,?,?,?)',
                            (p['id'],p['item_id'],p['target_at'],p['due_at'],p['rule'],now()))
                con.execute("UPDATE reminder_jobs SET state='pending',updated_at=? WHERE id=? AND state='cancelled'",(now(),p['id']))

    def tick(self,current=None):
        current=current or datetime.now(VIENNA)
        self.reconcile(current);settings=self.settings()
        clock=current.timetz().replace(tzinfo=None)
        quiet=settings.quiet and (settings.quiet_start<=clock<settings.quiet_end if settings.quiet_start<settings.quiet_end else clock>=settings.quiet_start or clock<settings.quiet_end)
        with self.db.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            groups={}
            for job in con.execute("SELECT * FROM reminder_jobs WHERE state='pending' AND due_at<=? ORDER BY due_at DESC",(utc(current),)).fetchall():groups.setdefault(job['item_id'],[]).append(dict(job))
            if not groups:return
            # Re-read while writers are excluded: an imminent edit cannot dispatch the old plan.
            from .personal import relative_current
            items={i['id']:i for i in self.db.items(active_only=True)}
            for item_id,jobs in groups.items():
                item=items.get(item_id);manual=con.execute('SELECT * FROM personal_reminders WHERE item_id=?',(item_id,)).fetchone()
                valid={p['id'] for p in self.plans(item,current,manual,related_items=items.values())[0]} if item and relative_current(item,items) else set()
                for index,job in enumerate(jobs):
                    target=datetime.fromisoformat(job['target_at']);due=datetime.fromisoformat(job['due_at'])
                    fresh=job['id'] in valid
                    opening=job['rule'].startswith('registration:')
                    missed=current-due>timedelta(minutes=15) if opening else target<=current and (job['rule'] not in ('voice:exact','window:exact') or current-due>timedelta(minutes=15))
                    if opening and target<=current:
                        # A wake after the opening can emit just one stage, including when
                        # an earlier tick already caught up another stage after this target.
                        missed=missed or bool(con.execute("SELECT 1 FROM reminder_jobs WHERE item_id=? AND target_at=? AND rule LIKE 'registration:%' AND state='delivered' AND updated_at>=?",(item_id,job['target_at'],job['target_at'])).fetchone())
                    missed=missed or current-due>timedelta(days=1)
                    state='cancelled' if not fresh else 'expired' if missed or index>0 else 'quiet' if quiet else 'delivered'
                    error=None
                    if state=='delivered':
                        try:
                            text=item['data']['title']+' · '+rule_label(job['rule'])+(' · nachgeholt' if current-due>timedelta(minutes=1) else '')
                            if opening:text=(item.get('course_title') or 'Eigene Planung')+' · '+text+' · Beginn '+target.astimezone(VIENNA).strftime('%d.%m.%Y %H:%M')
                            self.adapter.send('StudyCenter-ICE · Erinnerung',text,f'http://127.0.0.1:18791/items/{item_id}',job['id'],settings.sound)
                            if job['rule'].startswith('voice:'):con.execute('UPDATE personal_reminders SET delivered_at=? WHERE item_id=?',(now(),item_id))
                            self.error=''
                        except Exception as failure:
                            state='error';error=str(failure)[:500];self.error=error
                    con.execute('UPDATE reminder_jobs SET state=?,updated_at=?,error=? WHERE id=?',(state,utc(current),error,job['id']))
                    # No automatic retry storm after an OS error or quiet period.

    async def run(self):
        self.running=True
        try:
            while not self.stop.is_set():
                try:await asyncio.to_thread(self.tick)
                except Exception as failure:self.error=str(failure)[:500]
                try:await asyncio.wait_for(self.stop.wait(),timeout=15)
                except TimeoutError:pass
        finally:self.running=False

    def preview(self,item):
        manual=self.db.one('SELECT * FROM personal_reminders WHERE item_id=?',(item['id'],))
        plans,issues=self.plans(item,datetime.now(VIENNA),manual)
        for plan in plans:
            row=self.db.one('SELECT state,error FROM reminder_jobs WHERE id=?',(plan['id'],))
            plan.update(row or {'state':'geplant','error':None})
            if plan['target_precision']=='date':plan['target_at']=None  # Internal cutoff is never exported as a known midnight deadline.
        return plans,issues

    def inbox(self, *, unread=False, page=1, current=None):
        """Bounded delivery history; reading never changes reminder delivery state."""
        cutoff=utc((current or datetime.now(VIENNA))-timedelta(days=90))
        grouped='''FROM reminder_jobs j JOIN items i ON i.id=j.item_id
            LEFT JOIN courses c ON c.id=i.course_id
            LEFT JOIN notification_reads r ON r.job_id=j.id
            WHERE j.state IN ('delivered','error') AND j.updated_at>=?
            GROUP BY j.item_id,j.target_at'''
        unread_clause=' HAVING SUM(CASE WHEN r.job_id IS NULL THEN 1 ELSE 0 END)>0'
        count=self.db.one('SELECT COUNT(*) n FROM (SELECT j.item_id '+grouped+unread_clause+')',(cutoff,))['n']
        rows=self.db.rows('''SELECT j.item_id,j.target_at,MAX(j.updated_at) received_at,
            COALESCE(json_extract(i.overrides,'$.title'),json_extract(i.payload,'$.title')) title,
            c.title course_title, GROUP_CONCAT(j.id) ids,GROUP_CONCAT(j.rule) rules,
            COUNT(*) deliveries,SUM(CASE WHEN j.state='error' THEN 1 ELSE 0 END) errors,
            SUM(CASE WHEN r.job_id IS NULL THEN 1 ELSE 0 END) unread '''+grouped+
            (unread_clause if unread else '')+' ORDER BY received_at DESC,j.item_id DESC LIMIT 21 OFFSET ?',
            (cutoff,(page-1)*20))
        for row in rows:
            row['ids']=row['ids'].split(',')
            row['labels']=list(dict.fromkeys(rule_label(rule) for rule in row.pop('rules').split(',')))
        return {'entries':rows[:20],'has_more':len(rows)>20,'page':page,'unread':count,'days':90}

    def read_notifications(self, ids):
        if not isinstance(ids,list) or not 1<=len(ids)<=200 or any(not isinstance(v,str) or len(v)>150 for v in ids):
            raise ValueError('Ungültige Meldungsauswahl')
        with self.db.connection() as con:
            con.executemany('''INSERT OR IGNORE INTO notification_reads(job_id,read_at)
                SELECT id,? FROM reminder_jobs WHERE id=? AND state IN ('delivered','error')''',
                [(now(),identifier) for identifier in ids])
