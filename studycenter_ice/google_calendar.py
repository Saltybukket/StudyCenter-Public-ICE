"""One-way, consented projection into the app-created Google calendar."""
import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
import uuid
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs, quote

import httpx

from .chatgpt import SecureJSONStore
from .config import VIENNA, local_profile_path
from .db import encode, now

SCOPES = ('https://www.googleapis.com/auth/calendar.app.created',
          'https://www.googleapis.com/auth/calendar.calendarlist.readonly')
API = 'https://www.googleapis.com/calendar/v3'
FIELDS = ('summary','description','start','end','transparency','reminders','extendedProperties')


class GoogleError(RuntimeError):
    pass


class GoogleAuth:
    def __init__(self, data_dir):
        key = hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:16]
        self.path = local_profile_path(f'auth/google-{key}.dpapi', data_dir=data_dir)
        self.store = SecureJSONStore(self.path, service='google-calendar')
        self.gate = asyncio.Lock()
        self.pending = None
        self.server = None
        self.expiry_task = None
        self.generation = 0
        self.callback_tasks = set()

    def read(self):
        return self.store.read()

    def save(self, value):
        self.store.write(value)

    def import_client(self, raw):
        try:
            value = json.loads(raw)['installed']
            if not value['client_id'].endswith('.apps.googleusercontent.com') or not value.get('client_secret'):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise GoogleError('Google-JSON eines OAuth-Desktop-Clients importieren.') from None
        # Do not trust endpoint URLs from an uploaded configuration.
        old = self.read()
        if old.get('client_id') != value['client_id']:
            old = {}
        old.update(client_id=value['client_id'], client_secret=value['client_secret'])
        self.save(old)

    async def token(self):
        async with self.gate:
            value = self.read()
            if value.get('expires_at',0) > time.time()+60:
                return value['access_token']
            if not value.get('refresh_token'):
                raise GoogleError('Google-Anmeldung erforderlich.')
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post('https://oauth2.googleapis.com/token', data={
                    'client_id':value['client_id'], 'client_secret':value['client_secret'],
                    'grant_type':'refresh_token','refresh_token':value['refresh_token']})
            if response.status_code != 200:
                if response.status_code in (400,401):
                    self.save({k:v for k,v in value.items() if k in ('client_id','client_secret')})
                    raise GoogleError('Google-Freigabe abgelaufen/widerrufen; erneut verbinden (Testing ggf. sieben Tage).')
                raise GoogleError(f'Google-Tokenprüfung HTTP {response.status_code}.')
            result = response.json()
            value.update(access_token=result['access_token'],expires_at=time.time()+result['expires_in'])
            if result.get('refresh_token'):value['refresh_token']=result['refresh_token']
            self.save(value)
            return value['access_token']

    async def connect(self, on_connected):
        async with self.gate:
            if self.server:
                raise GoogleError('Google-Anmeldung bereits geöffnet; zuerst dort abschließen.')
            try:
                await self._connect(on_connected)
            except BaseException:
                self.generation += 1
                await self._close_listener()
                raise

    async def _connect(self, on_connected):
        if self.expiry_task:self.expiry_task.cancel()
        value = self.read()
        if not value.get('client_id'):raise GoogleError('Zuerst Google-Desktop-Client-JSON importieren.')
        self.generation += 1
        generation = self.generation
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        self.pending = {'state':state,'verifier':verifier,'expires':time.time()+300}
        claimed_task = None

        async def finish(reader, writer):
            nonlocal claimed_task
            status, message = '400 Bad Request', 'Google-Anmeldung konnte nicht bestätigt werden.'
            try:
                raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=5)
                line = raw.split(b'\r\n',1)[0].decode('ascii')
                method, target, _ = line.split(' ',2)
                args = parse_qs(urlsplit(target).query)
                async with self.gate:
                    pending = self.pending
                    if generation!=self.generation or method!='GET' or urlsplit(target).path!='/callback' or not pending or pending['expires']<time.time() or not secrets.compare_digest(args.get('state',[''])[0],pending['state']):
                        raise GoogleError('Ungültiger Anmelderückweg.')
                    self.pending = None  # One-time authorization code, even on an error.
                    claimed_task = asyncio.current_task()
                if not args.get('code'):raise GoogleError('Google-Freigabe abgelehnt.')
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post('https://oauth2.googleapis.com/token', data={
                        'client_id':value['client_id'],'client_secret':value['client_secret'],'grant_type':'authorization_code',
                        'code':args['code'][0],'redirect_uri':pending['redirect'],'code_verifier':verifier})
                if response.status_code!=200:raise GoogleError('Google-Codeaustausch abgewiesen.')
                result = response.json()
                if not set(SCOPES)<=set(result.get('scope','').split()) or not result.get('refresh_token'):
                    raise GoogleError('Google-Kalenderfreigabe oder Offline-Zugang fehlt; erneut verbinden.')
                async with self.gate:
                    current = self.read()
                    if generation!=self.generation or current.get('client_id')!=value['client_id']:
                        raise GoogleError('Google-Anmeldung wurde inzwischen beendet oder geändert.')
                    current.update(access_token=result['access_token'],refresh_token=result['refresh_token'],expires_at=time.time()+result['expires_in'])
                    self.save(current)
                await on_connected()
                status, message = '200 OK', 'Google verbunden. Vorschau und automatische Spiegelung in StudyCenter bitte lokal freigeben.'
            except Exception as error:
                if isinstance(error,GoogleError):message=str(error)
            body = ('<!doctype html><meta charset="utf-8"><title>StudyCenter</title><p>'+message+'</p>').encode()
            writer.write(f'HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
            await writer.drain()

        async def callback(reader, writer):
            task = asyncio.current_task()
            self.callback_tasks.add(task)
            try:
                await finish(reader, writer)
            finally:
                writer.close()
                self.callback_tasks.discard(task)
                async with self.gate:
                    # A late request from an old listener must not close a new login.
                    if task is claimed_task and generation==self.generation:
                        await self._close_listener()

        self.server = await asyncio.start_server(callback,'127.0.0.1',0,limit=8192)
        redirect = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/callback'
        self.pending['redirect'] = redirect
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        url = 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({'client_id':value['client_id'],'redirect_uri':redirect,
            'response_type':'code','scope':' '.join(SCOPES),'state':state,'code_challenge':challenge,'code_challenge_method':'S256',
            'access_type':'offline','prompt':'consent select_account'})
        webbrowser.open(url)
        async def expire():
            await asyncio.sleep(300)
            await self.close(generation=generation)
        self.expiry_task = asyncio.create_task(expire())

    async def _close_listener(self):
        if self.expiry_task and self.expiry_task is not asyncio.current_task():self.expiry_task.cancel()
        self.expiry_task = None
        if self.server:self.server.close();await self.server.wait_closed()
        self.server = None
        self.pending = None

    async def close(self, *, disconnect=False, generation=None):
        async with self.gate:
            if generation is not None and generation!=self.generation:return
            self.generation += 1
            tasks = [task for task in self.callback_tasks if task is not asyncio.current_task()]
            for task in tasks:task.cancel()
            await self._close_listener()
            if disconnect:
                value = self.read()
                self.save({k:v for k,v in value.items() if k in ('client_id','client_secret')})
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)


def event_id(dataset, identity, role):
    return hashlib.sha256((dataset+'\0'+identity+'\0'+role).encode()).hexdigest()


def fingerprint(payload):
    return hashlib.sha256(encode(payload).encode()).hexdigest()


def owned_fields(event):
    # Google adds offsets and explicit defaults. Compare the managed projection canonically.
    result = {key:event.get(key) for key in FIELDS}
    # Google omits its default Busy value in otherwise unchanged event responses.
    result['transparency'] = event.get('transparency','opaque')
    props=result.get('extendedProperties') or {}
    result['extendedProperties']={'private':{k:v for k,v in props.get('private',{}).items() if k.startswith('studycenter_')}}
    reminders=result.get('reminders')
    if reminders and not reminders.get('overrides'):result['reminders']={'useDefault':reminders.get('useDefault',False)}
    for key in ('start','end'):
        part = result.get(key)
        if part and part.get('dateTime'):
            part = dict(part)
            part['dateTime'] = datetime.fromisoformat(part['dateTime'].replace('Z','+00:00')).astimezone(VIENNA).isoformat()
            part['timeZone'] = 'Europe/Vienna'
            result[key] = part
    return result


def event_patch(desired):
    """PATCH merges nested objects: explicitly remove the previous time format."""
    patch = dict(desired)
    for key in ('start', 'end'):
        part = desired[key]
        # Cancelled-event responses can omit the old fields entirely.
        removed = {'dateTime': None, 'timeZone': None} if 'date' in part else {'date': None}
        patch[key] = {**part, **removed}
    return patch


def error_detail(response):
    # Reasons are protocol identifiers. Whitelist generic messages rather than
    # retaining a response body which may echo private calendar contents.
    try:
        error = response.json().get('error', {})
        reasons = [entry.get('reason', '') for entry in error.get('errors', []) if isinstance(entry, dict)]
        details = list(dict.fromkeys(reason for reason in reasons if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', reason)))[:3]
        message = error.get('message', '')
        if message in ('Invalid start time.', 'Invalid end time.', 'Bad Request', 'Forbidden', 'Not Found'):
            details.append(message)
        return ' · '.join(details)
    except (ValueError, AttributeError, TypeError):
        return ''


def projection(items, events, states, options, dataset, reminder_minutes=None):
    by_id = {str(event['id']):event for event in events}
    chosen = options.get('selection',{})
    selected, skipped = [], []
    for item in items:
        value, state = item['data'],states[item['id']]
        reason = ''
        optional = state['closing'] and value['kind'] in ('exam','registration')
        enabled = chosen.get(item['identity'],True)
        if not enabled:reason='Persönlich vom Export abgewählt.'
        elif item.get('third_party_message'):reason='Übernommene private Dritt-Nachricht; bleibt lokal.'
        elif item.get('course_active')==0:reason='Historischer/inaktiver Kurs.'
        elif value.get('semester') and item.get('course_semester') and value['semester']!=item['course_semester']:reason='Historische/andere Semesterangabe.'
        elif value.get('group') and item.get('course_group') and value['group']!=item['course_group']:reason='Andere Übungsgruppe.'
        elif item['ignored'] or item['status']=='done' or value['source_state']!='active':reason='Ausgeblendet, erledigt oder abgesagt.'
        elif item.get('date_conflict',item['conflict']):reason='Widersprüchliche Quellenzeit.'
        elif state['offered'] and not optional:reason='Unbestätigter angebotener Slot.'
        elif not (state['personal'] or state['appointment'] or state['closing'] and value['obligation']=='required'
                  or optional or item['identity'].startswith('note:')):reason='Keine persönliche Planung / konkrete Frist.'
        elif value['date_meaning'] in ('none','unclear','recommended'):reason='Kein eindeutig belegter Zeitpunkt.'
        event = by_id.get(str(item['id']))
        if not reason and not event and not (optional and state['action_date']):reason='Kein Kalenderzeitpunkt / zusammengeführte Dublette.'
        if item.get('calendar_duplicate_of'):reason='Zusammengeführte Kalenderdublette.'
        if reason:
            skipped.append({'id':item['id'],'identity':item['identity'],'title':value['title'],'course':item.get('course_title') or 'Eigene Planung','reason':reason})
            continue
        closing = state['closing']
        start = state['action_date'] + ('T'+state['action_time'] if state['action_time'] else '') if closing else event['start']
        end = None if closing else event.get('end')
        marker = 'T' in start and not end
        if marker and not options.get('minute_marker',True):
            skipped.append({'id':item['id'],'identity':item['identity'],'title':value['title'],'course':item.get('course_title') or 'Eigene Planung','reason':'Punkt ohne belegtes Ende; Darstellungsminute abgewählt.'})
            continue
        role = 'closing' if closing else 'start'
        def temporal(text):
            if 'T' not in text:return {'date':text}
            return {'dateTime':datetime.fromisoformat(text).replace(tzinfo=VIENNA).isoformat(),'timeZone':'Europe/Vienna'}
        if not end:
            end = (datetime.fromisoformat(start)+timedelta(minutes=1)).isoformat() if marker else (date.fromisoformat(start)+timedelta(days=1)).isoformat()
        description = (item.get('course_title') or 'Eigene Planung')+'\n'+state['action_label']
        summary=re.sub(r'https?://\S+','',value.get('description',''))[:360].strip()
        if summary:description+='\n'+summary
        description += '\n'+('Von dir geplant; keine offizielle Buchung.' if state['personally_planned'] else 'Buchung belegt.' if state['booking_proven'] else 'Konkrete Frist / Lehrtermin; keine Teilnahmebehauptung.')
        if optional:description+='\nOptionaler Fristmarker; keine Pflicht jedes einzelnen Quiz / keine Anmeldung behauptet.'
        if closing and value.get('end_date'):
            description+='\nÖffnung: '+value['date']+(' '+value['time'] if value.get('time') else '')
        if marker:description+='\nExakte Zeit: '+start+'. Eine Minute dient nur der Darstellung, keine echte Dauer.'
        if 'T' not in start:description+='\nUhrzeit nicht genannt.'
        # Local evidence references, never entire mail/PDF contents or private signed URLs.
        description+='\nBelege lokal: /items/'+str(item['id'])
        if item.get('personal_decisions'):
            description+='\nPersönliche Entscheidung – von dir geklärt; keine offizielle Buchung.'
            if any(d.get('stale') for d in item['personal_decisions']): description+='\nNeue Quelle – Entscheidung prüfen.'
        reminders = {'useDefault':False}
        if options.get('reminders') and 'T' in start:
            minutes = reminder_minutes(item) if reminder_minutes else []
            if minutes:reminders['overrides']=[{'method':'popup','minutes':m} for m in sorted(set(minutes)) if 0<=m<=40320][:5]
        payload = {'summary':(('Frist: ' if closing else '')+value['title'])[:240], 'description':description,
                   'start':temporal(start),'end':temporal(end),'transparency':'transparent' if closing or marker or 'T' not in start else 'opaque',
                   'reminders':reminders,'extendedProperties':{'private':{'studycenter_dataset':dataset,'studycenter_identity':item['identity'],'studycenter_role':role}}}
        selected.append({'id':item['id'],'identity':item['identity'],'role':role,'event_id':event_id(dataset,item['identity'],role),
                         'title':value['title'],'course':item.get('course_title') or 'Eigene Planung','label':state['action_label'],'marker':marker,'optional':optional,'payload':payload})
    return selected,skipped


class GoogleCalendar:
    def __init__(self, db, settings, projection_provider):
        self.db,self.settings,self.provider=db,settings,projection_provider
        self.auth=GoogleAuth(settings.data_dir)
        self.gate=asyncio.Lock()
        self.stop=asyncio.Event()
        self.error=''
        self.retry_at=0
        self.failures=0
        self.http=None  # Injectable transport for isolated behavior tests.

    def initialize(self, restored=False):
        options=self.options()
        if not options.get('dataset'):options['dataset']=str(uuid.uuid4())
        if restored:options.update(enabled=False,restore_paused=True,consent=False)
        self.save(options)
        self.retry_at=options.get('retry_at',0)

    def options(self):
        row=self.db.one("SELECT value FROM app_preferences WHERE name='google_calendar'")
        return json.loads(row['value']) if row else {'enabled':False,'consent':False,'minute_marker':True,'reminders':False}

    def save(self, options):
        self.db.execute("INSERT OR REPLACE INTO app_preferences(name,value) VALUES('google_calendar',?)",(encode(options),))

    def public(self):
        options=self.options()
        try:secret=self.auth.read()
        except Exception:secret={}
        return {**options,'client_configured':bool(secret.get('client_id')),'connected':bool(secret.get('refresh_token')),
                'error':self.error or options.get('error',''),'busy':self.gate.locked(),
                'credential_storage':self.auth.store.storage_status(),
                'conflicts':self.db.rows("SELECT * FROM google_events WHERE state='conflict'")}

    async def request(self, method, path, **kwargs):
        token=await self.auth.token()
        headers=kwargs.pop('headers',{}) | {'Authorization':'Bearer '+token}
        if self.http:response=await self.http.request(method,API+path,headers=headers,**kwargs)
        else:
            async with httpx.AsyncClient(timeout=20) as client:
                response=await client.request(method,API+path,headers=headers,**kwargs)
        if response.status_code==401:
            value=self.auth.read();value['expires_at']=0;self.auth.save(value)
            raise GoogleError('Google-Zugang abgewiesen; erneuter Versuch mit frischem Token.')
        if response.status_code not in (200,201,204,404,409,410,412):
            detail = error_detail(response)
            raise GoogleError(f'Google-Kalender HTTP {response.status_code} ({method})'+(': '+detail if detail else '.'))
        return response

    async def identify(self):
        options=self.options()
        matches=[];page=None
        for _ in range(20):
            if self.stop.is_set():return options
            response=await self.request('GET','/users/me/calendarList',params={'maxResults':250,**({'pageToken':page} if page else {})})
            if self.stop.is_set():return options
            result=response.json()
            for calendar in result.get('items',[]):
                if calendar.get('primary'):options['account']=calendar.get('summary','Autorisiertes Google-Konto')
                if calendar.get('description')=='StudyCenter dataset '+options['dataset'] and calendar.get('accessRole')=='owner':matches.append(calendar['id'])
            page=result.get('nextPageToken')
            if not page:break
        else:raise GoogleError('Kalenderliste unvollständig; kein neuer Kalender angelegt.')
        if len(matches)>1:raise GoogleError('Mehrere eigene Zielkalender gefunden; eindeutige Zuordnung fehlt.')
        if matches:options['calendar_id']=matches[0];options['creating']=False
        elif options.get('calendar_id'):
            raise GoogleError('Gespeicherter app-eigener Zielkalender nicht in diesem Konto bestätigt; Schreibabgleich pausiert.')
        self.save(options)
        return options

    async def target(self):
        options=await self.identify()
        if self.stop.is_set():return None
        if options.get('calendar_id'):return options['calendar_id']
        if options.get('creating'):raise GoogleError('Unklarer Kalender-Anlegeversuch; Ziel erneut prüfen. Kein zweiter Kalender angelegt.')
        options['creating']=True;self.save(options)
        response=await self.request('POST','/calendars',json={'summary':'StudyCenter','description':'StudyCenter dataset '+options['dataset'],'timeZone':'Europe/Vienna'})
        if response.status_code!=200:raise GoogleError('Eigenen Google-Zielkalender konnte ich nicht bestätigen.')
        options['calendar_id']=response.json()['id'];options['creating']=False;self.save(options)
        return options['calendar_id']

    def preview(self):
        return self.provider(self.options())

    async def sync(self):
        if self.gate.locked() or self.stop.is_set():return
        async with self.gate:
            options=self.options()
            if not options.get('enabled') or not options.get('consent') or options.get('restore_paused'):return
            try:
                calendar=options.get('calendar_id') or await self.target()
                if self.stop.is_set():return
                # The original calendar must still be our app-created calendar on reconnect.
                if not options.get('calendar_id'):options=self.options()
                selected,skipped=self.preview()
                wanted={(entry['identity'],entry['role']):entry for entry in selected}
                rows=self.db.rows('SELECT * FROM google_events WHERE calendar_id=?',(calendar,))
                for row in rows:
                    if self.stop.is_set():return
                    if row['state'] in ('keep','conflict'):continue
                    if (row['identity'],row['role']) not in wanted:
                        # Only explicit cancellation / completion / deselection, never absent partial source results.
                        item=self.db.one('SELECT * FROM items WHERE identity=?',(row['identity'],))
                        superseded=any(identity==row['identity'] for identity,role in wanted)
                        if item and (superseded or item['ignored'] or item['status']=='done' or json.loads(item['payload'])['source_state']=='cancelled'
                                     or options.get('selection',{}).get(row['identity']) is False):
                            await self.publish(calendar,row,None)
                for entry in selected:
                    if self.stop.is_set():return
                    row=self.db.one('SELECT * FROM google_events WHERE calendar_id=? AND identity=? AND role=?',(calendar,entry['identity'],entry['role']))
                    if row and row['state']=='keep':continue
                    await self.publish(calendar,row,entry)
                if self.stop.is_set():return
                self.error='';self.failures=0;self.retry_at=0
                options=self.options();options.update(last_success=now(),error='',selected=len(selected),omitted=len(skipped));self.save(options)
            except (GoogleError,httpx.HTTPError,OSError,ValueError) as error:
                self.failures+=1;self.retry_at=time.time()+min(1800,15*2**min(self.failures,7))
                self.error=str(error) if isinstance(error,GoogleError) else 'Google momentan nicht erreichbar; lokale Änderungen bleiben vorgemerkt.'
                options=self.options();options.update(error=self.error,retry_at=self.retry_at);self.save(options)
                if isinstance(error,GoogleError) and not self.auth.read().get('refresh_token'):
                    options['enabled']=False;self.save(options)

    async def publish(self, calendar, row, entry, retry=True):
        identity,role,identifier=(entry['identity'],entry['role'],entry['event_id']) if entry else (row['identity'],row['role'],row['event_id'])
        path='/calendars/'+quote(calendar,safe='')+'/events/'+identifier
        if row and row['state']=='deleted' and entry is None:return
        response=await self.request('GET',path)
        if self.stop.is_set():return  # No new write after shutdown was requested.
        remote=response.json() if response.status_code==200 else None
        if response.status_code not in (200,404,410):raise GoogleError('Google-Ereignis nicht eindeutig gelesen.')
        tombstone=bool(remote and remote.get('status')=='cancelled' and row)
        if remote and not tombstone and remote.get('extendedProperties',{}).get('private',{}).get('studycenter_dataset')!=self.options()['dataset']:
            raise GoogleError('Ereignisidentität gehört nicht zu diesem StudyCenter-Datensatz.')
        desired=entry['payload'] if entry else None
        if row and row['state'] in ('synced','conflict') and (not remote or tombstone):
            self.receipt(calendar,identity,role,identifier,json.loads(row['payload']),None,'conflict','Google-Ereignis extern entfernt; Entscheidung nötig.')
            return
        if remote:
            old=json.loads(row['payload']) if row else desired
            if tombstone and row['state']=='deleted' and desired:
                # Reactivate only our previously confirmed deletion, using the same event ID.
                response=await self.request('PATCH',path,headers={'If-Match':remote['etag']},json={**event_patch(desired),'status':'confirmed'})
                if response.status_code==412:
                    self.receipt(calendar,identity,role,identifier,old,remote['etag'],'conflict','Google während Wiederaufnahme verändert.');return
                if response.status_code!=200:raise GoogleError('Google-Wiederaufnahme nicht bestätigt.')
                self.receipt(calendar,identity,role,identifier,desired,response.json().get('etag'),'synced');return
            # Recover an ambiguous successful write by its stable ID and desired fields.
            if row and row['state']=='pending' and desired and owned_fields(remote)==owned_fields(desired):
                return self.receipt(calendar,identity,role,identifier,desired,remote.get('etag'),'synced')
            if row and owned_fields(remote)!=owned_fields(old) or not row and owned_fields(remote)!=owned_fields(desired):
                self.receipt(calendar,identity,role,identifier,old,remote.get('etag'),'conflict','Google-Version extern verändert; Entscheidung nötig.')
                return
            if desired and owned_fields(remote)==owned_fields(desired):
                return self.receipt(calendar,identity,role,identifier,desired,remote.get('etag'),'synced')
            # Persist the previous confirmed payload; don't treat a failed PATCH as confirmation.
            self.receipt(calendar,identity,role,identifier,old,remote.get('etag'),'pending')
            patch=desired
            if desired:
                props=remote.get('extendedProperties',{})
                patch={**event_patch(desired),'extendedProperties':{**props,'private':{**props.get('private',{}),**desired['extendedProperties']['private']}}}
            response=await self.request('PATCH' if desired else 'DELETE',path,headers={'If-Match':remote['etag']},**({'json':patch} if desired else {}))
            if response.status_code==412:
                self.receipt(calendar,identity,role,identifier,old,remote['etag'],'conflict','Google während der Änderung verändert.')
                return
            if response.status_code not in (200,204):raise GoogleError('Google-Änderung nicht bestätigt.')
        elif desired:
            self.receipt(calendar,identity,role,identifier,desired,None,'pending')
            response=await self.request('POST','/calendars/'+quote(calendar,safe='')+'/events',json={'id':identifier,**desired})
            if response.status_code==409:
                if self.stop.is_set():return  # Keep the pending stable ID for recovery.
                if not retry:raise GoogleError('Stabile Google-Ereignis-ID belegt, aber nicht lesbar; kein neues Ereignis erzeugt.')
                return await self.publish(calendar,self.db.one('SELECT * FROM google_events WHERE calendar_id=? AND identity=? AND role=?',(calendar,identity,role)),entry,retry=False)
            if response.status_code not in (200,201):raise GoogleError('Google-Erstellung nicht bestätigt.')
        if not desired:
            self.receipt(calendar,identity,role,identifier,json.loads(row['payload']),None,'deleted')
        else:
            self.receipt(calendar,identity,role,identifier,desired,response.json().get('etag'),'synced')

    def receipt(self, calendar, identity, role, identifier, payload, etag, state, error=''):
        self.db.execute('INSERT OR REPLACE INTO google_events VALUES(?,?,?,?,?,?,?,?,?)',
                        (calendar,identity,role,identifier,encode(payload),fingerprint(payload),etag,state,error))

    async def resolve(self, identity, role, keep):
        async with self.gate:
            options=self.options();calendar=options.get('calendar_id')
            row=self.db.one("SELECT * FROM google_events WHERE calendar_id=? AND identity=? AND role=? AND state='conflict'",(calendar,identity,role))
            if not row:raise GoogleError('Kein offener Google-Konflikt für diesen Eintrag.')
            if keep:self.db.execute("UPDATE google_events SET state='keep' WHERE calendar_id=? AND identity=? AND role=?",(calendar,identity,role));return
            response=await self.request('GET','/calendars/'+quote(calendar,safe='')+'/events/'+row['event_id'])
            if response.status_code==200:
                # Explicit consent to replace the newly read owned fields; next PATCH remains conditional.
                self.receipt(calendar,identity,role,row['event_id'],owned_fields(response.json()),response.json().get('etag'),
                             'deleted' if response.json().get('status')=='cancelled' else 'synced')
            elif response.status_code==404:
                self.db.execute("UPDATE google_events SET state='pending' WHERE calendar_id=? AND identity=? AND role=?",(calendar,identity,role))
            else:raise GoogleError('Google-Konflikt konnte nicht neu gelesen werden.')
        await self.sync()

    async def run(self, writable):
        signature=None;last=0
        while not self.stop.is_set():
            options=self.options()
            if options.get('enabled') and options.get('consent') and writable() and time.time()>=self.retry_at:
                current=fingerprint({'items':self.db.rows('SELECT id,payload,status,ignored,overrides,pinned FROM items'),
                                     'courses':self.db.rows('SELECT id,active,group_name FROM courses'),
                                     'personal':self.db.rows('SELECT * FROM item_preferences'),
                                     'evidence':self.db.one('SELECT count(*) n,sum(current) c,max(id) i FROM item_evidence'),
                                     'options':{k:v for k,v in options.items() if k not in ('last_success','error','selected','omitted','retry_at')}})
                if current!=signature or self.failures or time.time()-last>=300:
                    await self.sync();signature=current;last=time.time()
            try:await asyncio.wait_for(self.stop.wait(),timeout=5)
            except TimeoutError:pass
        await self.auth.close()
        if self.auth.expiry_task:self.auth.expiry_task.cancel()
