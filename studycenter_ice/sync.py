import asyncio
import json
from pathlib import Path

from .browser import BrowserUnavailable, LoginRequired
from .collectors import PublicCollector, TeachCenterCollector, crawl, discover_courses, archived_discussion
from .db import encode, now
from .documents import DocumentStore, chunks
from .llm import ModelAdapter
from .chatgpt import ChatGPTAuth, CredentialStore
from .tugrazonline import collect_course, TUGRAZonlineError
from .gitlab import collect_repository, GitLabError
from .mail import collect_mail, collect_webmail, MailError, MailCredentials


class SyncService:
    def __init__(self, db, settings, browser):
        self.db, self.settings, self.browser = db, settings, browser
        self.store = DocumentStore(db, settings)
        self.auth = ChatGPTAuth(store=CredentialStore(data_dir=settings.data_dir))
        settings.chatgpt = self.auth
        self.model = ModelAdapter(db, settings, self.auth)
        self.gate = asyncio.Lock()
        self.task = None
        self.import_followup = None
        self.run_id = None
        self.stage = "Bereit"
        self.progress = None

    @property
    def active(self):
        return bool(self.task and not self.task.done())

    async def start(self, kind="manual", day=None, source_id=None):
        async with self.gate:
            if self.active:
                return self.run_id
            run_id = self.db.reserve_run(kind, day)
            if run_id is None:
                return None
            self.run_id = run_id
            self.progress = {'sources_completed': 0, 'sources_total': None, 'source': None}
            self.stage = 'Sync vorbereiten'
            self.task = asyncio.create_task(self._run(run_id, kind, source_id) if source_id is not None else self._run(run_id, kind))
            return run_id

    async def imported(self):
        if not self.active:
            return await self.start('import')
        if not self.import_followup or self.import_followup.done():
            async def followup():
                while self.active:
                    await asyncio.shield(self.task)
                await self.start('import')
            self.import_followup = asyncio.create_task(followup())
        return self.run_id

    async def _run(self, run_id, kind, source_id=None):
        results = []
        state = "success"
        public = PublicCollector()
        sessions = {}
        try:
            if kind == "manual":
                configured = self.db.rows("SELECT s.* FROM sources s LEFT JOIN courses c ON c.id=s.course_id WHERE s.active=1 AND (c.active=1 OR s.course_id IS NULL)")
                services = {s['kind'] for s in configured if s['kind'] in ('teachcenter','tugrazonline')
                            or s['kind']=='mail' and json.loads(s['config']).get('transport')=='webmail'}
                if source_id is not None:
                    services = {s['kind'] for s in configured if s['id']==source_id and s['kind'] in services}
                if services:
                    self.stage = 'Vorhandene TU-Sitzungen lesend prüfen'
                    sessions = await self.browser.probe(sorted(services))
                    results.append({'sessions':sessions})
            if kind not in ('import', 'analysis') and source_id is None and sessions.get('teachcenter',{}).get('state','authenticated') == 'authenticated':
                self.stage = "TeachCenter-Kursliste prüfen"
                try:
                    count = await discover_courses(self.browser, self.db, self.settings)
                    discovery = f"{count} persönliche Kurse gelesen; neue Kurse bitte aktivieren und zuordnen."
                except (LoginRequired, BrowserUnavailable) as error:
                    discovery = str(error)
                    state = 'partial'
                except Exception as error:
                    discovery = f"Kursliste unvollständig ({type(error).__name__}); manuelle URL möglich."
                    state = 'partial'
                results.append({"discovery": discovery})
            sources = self.db.rows("""SELECT s.* FROM sources s LEFT JOIN courses c ON c.id=s.course_id
                WHERE s.active=1 AND (c.active=1 OR s.course_id IS NULL)
                ORDER BY CASE WHEN EXISTS(SELECT 1 FROM documents d WHERE d.source_id=s.id
                AND d.mime='application/pdf' AND d.analysis_state!='success') THEN 0 ELSE 1 END,s.id""")
            if kind == 'import':
                sources = [s for s in sources if s['kind']=='manual']
            if source_id is not None:
                sources = [s for s in sources if s['id']==source_id]
            self.progress = {'sources_completed': 0, 'sources_total': len(sources), 'source': None}
            for source in sources:
                self.progress['source'] = source['title']
                self.stage = f"Abruf: {source['title']}"
                check = sessions.get(source['kind'])
                if check and check['state'] != 'authenticated':
                    self.db.execute('UPDATE sources SET state=?,message=? WHERE id=?',
                        ('login_required' if check['state']=='login_required' else 'partial',check['reason'],source['id']))
                    results.append({'source':source['id'],'session':check,'complete':False})
                    state = 'partial'
                    self.progress['sources_completed'] += 1
                    continue
                if kind != 'analysis':
                    self.db.execute("UPDATE sources SET last_attempt=?,state='running',message='' WHERE id=?", (now(), source["id"]))
                try:
                    if kind == 'analysis':
                        remaining = len(json.loads(source['crawl_state']).get('queue', []))
                        result = {'source': source['id'], 'complete': not remaining, 'fetched': 0,
                                  'remaining': remaining, 'message': 'Gespeicherte Dokumente auswerten; kein erneuter Quellenabruf.'}
                    elif source["kind"] == "public":
                        result = await crawl(source, public, self.db, self.store)
                    elif source["kind"] == "teachcenter":
                        async with self.browser.crawl_context() as context:
                            result = await crawl(source, TeachCenterCollector(context), self.db, self.store)
                    elif source['kind'] == 'tugrazonline':
                        async with self.browser.crawl_context() as context:
                            result = await collect_course(self.db.source(source['id']),context,self.db,self.store)
                    elif source['kind'] == 'gitlab':
                        result = await collect_repository(source, self.db, self.store)
                    elif source['kind'] == 'mail':
                        if json.loads(source['config']).get('transport') == 'webmail':
                            config = json.loads(source['config'])
                            cursor = json.loads(source['crawl_state'])
                            if config.get('include_all') and cursor.get('collection_complete') and self.db.one("""SELECT count(*) n FROM documents
                                WHERE (source_id=? OR id IN (SELECT value FROM json_each(?))) AND (processing_state!='success' OR analysis_state!='success')""",
                                (source['id'],encode(cursor.get('document_ids',[]))))['n']:
                                result = {'source':source['id'],'complete':True,'searches_pending':0,'message':'Alle konfigurierten Ordner gelesen; gespeicherte Auswertungen fortsetzen.'}
                            else:
                                async with self.browser.crawl_context() as context:
                                    result = await collect_webmail(source,context,self.db,self.store,self.browser)
                        else:
                            result = await collect_mail(source, self.db, self.store, MailCredentials(data_dir=self.settings.data_dir))
                    else:
                        self.db.execute("UPDATE sources SET state='success',last_success=? WHERE id=?", (now(), source["id"]))
                        result = {"source": source["id"], "complete": True}
                    results.append(result)
                    if not result["complete"]:
                        state = "partial"
                except Exception as error:
                    login = isinstance(error, LoginRequired)
                    message = str(error) if isinstance(error, (LoginRequired, BrowserUnavailable, TUGRAZonlineError, GitLabError, MailError)) else f"Abruf fehlgeschlagen ({type(error).__name__})"
                    self.db.execute("UPDATE sources SET state=?,message=? WHERE id=?", ("login_required" if login else "failed", message, source["id"]))
                    result = {"source": source["id"], "complete": False, "message": message}
                    results.append(result)
                    state = "partial"
                self.db.execute("UPDATE sync_runs SET results=? WHERE id=?", (encode(results), run_id))
                # Resume latest document versions separately from download success; even when login expired.
                linked = json.loads(self.db.source(source['id'])['crawl_state']).get('document_ids',[]) if source['kind']=='mail' and json.loads(source['config']).get('include_all') else []
                documents = self.db.rows("""SELECT d.* FROM documents d WHERE (source_id=? OR id IN (SELECT value FROM json_each(?))) AND
                  (d.mime='message/rfc822' OR d.id=(SELECT d2.id FROM documents d2 WHERE d2.source_id=d.source_id AND d2.url=d.url ORDER BY fetched_at DESC,id DESC LIMIT 1))
                  AND analysis_state!='archived' AND (processing_state!='success' OR analysis_state!='success')
                  ORDER BY CASE WHEN url IN (SELECT value FROM json_each(?)) THEN 0 ELSE 1 END,
                  CASE analysis_state WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                  CASE WHEN ?='teachcenter' AND mime='text/html' THEN 0 WHEN mime='application/pdf' THEN 1 ELSE 2 END,
                  julianday(published_at),d.id""", (source["id"], encode(linked), encode(json.loads(source['config']).get('priority_urls', [])), source['kind']))
                # A labelled introduction resource can have the opaque filename "view.php".
                # Prioritized PDFs carry course organization just as HTML pages do.
                if source['kind'] == 'teachcenter':
                    preferred = json.loads(self.db.source(source['id'])['config']).get('priority_urls', [])
                    documents.sort(key=lambda d: (d['url'] not in preferred,
                                                  d['mime'] != 'application/pdf' if d['url'] in preferred else False))
                budget = max(1, min(30, int(json.loads(source['config']).get('max_analysis_documents', 3))))
                result['analysis_attempted'] = 0
                result['analysis_replayed'] = 0
                if source['kind']=='mail' and not json.loads(source['config']).get('allow_model_analysis',False):
                    result['analysis_message'] = 'Mailtexte und Anhänge lokal gespeichert; Modellauswertung unter TU-Mail ausdrücklich aktivieren.'
                    documents = []
                if source['kind']=='mail':
                    allowed_ids = json.loads(source['config']).get('model_document_ids')
                    if allowed_ids is not None:
                        documents = [d for d in documents if d['id'] in allowed_ids]
                for index, document in enumerate(documents, 1):
                    config = json.loads(source['config'])
                    if source['kind'] == 'teachcenter' and (document['url'] in config.get('excluded_urls', []) or
                            (document['mime'] == 'text/html' and archived_discussion(
                            Path(document['original_path']).read_bytes(), document['url'], config.get('forum_since')))):
                        self.db.execute("UPDATE documents SET analysis_state='archived',error=? WHERE id=?",
                                        ('Ausdrückliches Archivmaterial; lokal erhalten, nicht an das Modell gesendet.', document['id']))
                        continue
                    # HTML parsing is inexpensive. Identical rendered content can be replayed
                    # without consuming the model-call budget or starving unread subpages.
                    if result['analysis_attempted'] >= budget and document['mime'] != 'text/html':
                        continue
                    self.stage = f"Dokument {index}/{len(documents)}: {document['title']} · {max(0,len(documents)-index)} weitere offen"
                    if document['processing_state'] in ('success', 'partial') and json.loads(document['pages']):
                        processed = document  # Same version: reuse text; keep OCR gaps for an explicit re-read.
                    else:
                        processed = await asyncio.to_thread(self.store.process, document["id"])
                    progress = json.loads(processed['progress'])
                    segments = chunks(json.loads(processed['pages']))
                    cached = bool(segments) and all(str(k) in progress for k in range(len(segments)))
                    if result['analysis_attempted'] >= budget and not cached:
                        continue
                    await self.model.analyze(processed, max_chunks=3)
                    result['analysis_replayed' if cached else 'analysis_attempted'] += 1
                    latest = self.db.one("SELECT processing_state,analysis_state FROM documents WHERE id=?", (document["id"],))
                    if latest["processing_state"] != "success" or latest["analysis_state"] != "success":
                        state = "partial"
                stats = self.db.one("""SELECT count(*) documents,
                    sum(CASE WHEN mime='application/pdf' THEN 1 ELSE 0 END) pdfs,
                    sum(CASE WHEN mime='text/html' THEN 1 ELSE 0 END) html_pages,
                    sum(CASE WHEN analysis_state!='archived' AND (analysis_state!='success' OR processing_state!='success') THEN 1 ELSE 0 END) pending,
                    sum(CASE WHEN analysis_state='archived' THEN 1 ELSE 0 END) archived
                    FROM documents d WHERE (source_id=? OR id IN (SELECT value FROM json_each(?))) AND (d.mime='message/rfc822' OR d.id=(SELECT d2.id FROM documents d2
                    WHERE d2.source_id=d.source_id AND d2.url=d.url ORDER BY fetched_at DESC,id DESC LIMIT 1))""", (source['id'], encode(linked)))
                result.update({k: v or 0 for k,v in stats.items()})
                if result['pending']:
                    state = 'partial'
                elif source['kind']=='mail' and json.loads(source['config']).get('include_all'):
                    cursor = json.loads(self.db.source(source['id'])['crawl_state'])
                    cursor.pop('collection_complete',None)
                    self.db.execute('UPDATE sources SET crawl_state=? WHERE id=?',(encode(cursor),source['id']))
                self.db.execute('UPDATE sync_runs SET results=? WHERE id=?', (encode(results),run_id))
                self.progress['sources_completed'] += 1
        except asyncio.CancelledError:
            state = "interrupted"
            raise
        except Exception as error:
            state = "failed"
            results.append({"error": f"Sync fehlgeschlagen ({type(error).__name__})"})
        finally:
            self.stage = 'Sync abschließen · Abrufbrowser schließen'
            if self.progress:
                self.progress['source'] = None
            try:
                await public.close()
            except Exception as error:
                state = 'failed'
                results.append({'error': f'Abrufclient nicht vollständig beendet ({type(error).__name__})'})
            finally:
                try:
                    await self.browser.close_idle()
                except Exception as error:
                    state = 'failed'
                    results.append({'error': f'TU-Browser nicht vollständig beendet ({type(error).__name__})'})
                finally:
                    self.db.execute("UPDATE sync_runs SET state=?,finished_at=?,results=? WHERE id=?", (state, now(), encode(results), run_id))
                    self.stage = "Bereit"

    async def close(self):
        if self.import_followup and not self.import_followup.done():
            self.import_followup.cancel()
            try:
                await self.import_followup
            except asyncio.CancelledError:
                pass
        if self.active:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
