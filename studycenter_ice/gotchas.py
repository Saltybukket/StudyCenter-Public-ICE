"""Small local, inspectable memory; source text can never author a rule."""
import hashlib
import json

from .db import encode, now

SCOPES={'all':'Alle Luna-Aufgaben','extraction':'Dokumentauswertung','assistant':'Sprach-/Textbefehle','questions':'Kursfragen'}
KNOWN={
 'quote':('Unbelegte Fundstelle','Zitate exakt und zusammenhängend aus dem angegebenen Originalabschnitt kopieren. Keine getrennten Zeilen verbinden oder Zitate umformulieren; ohne Beleg keine Aussage übernehmen.'),
 'schema':('Ungültiges Antwortformat','Nur das verlangte JSON-Schema verwenden. Pflichtfelder, erlaubte Werte und Datums-/Zeitabhängigkeiten prüfen; fehlende Information nicht erfinden.'),
 'identity_date':('Datum in Aufgabenidentität','Aufgabenkennungen und anchor stabil ohne Datum bilden. Terminrolle von Datum trennen und vorhandene eindeutige Kennungen wiederverwenden.'),
 'creation_intent':('Erstellungsauftrag als Suche interpretiert','Eine ausdrückliche Bitte zum Anlegen einer Aufgabe, Notiz oder Erinnerung ist eine Erstellungsabsicht. Vorhandene Kurse auflösen; eine Suche ersetzt die gewünschte Erstellung nicht.'),
 'unknown_source':('Unbekannte Fundstelle','Nur source_id aus den tatsächlich bereitgestellten Quellen verwenden. Gibt es keine passende Fundstelle, unzureichende Belege melden.'),
 'technical':('Technischer Modellfehler',''),
 'operation':('Befehl konnte nicht ausgeführt werden',''),
}


class Gotchas:
    def __init__(self,db):self.db=db

    def entries(self):
        row=self.db.one("SELECT value FROM app_preferences WHERE name='luna_gotchas'")
        return json.loads(row['value']) if row else []

    def save(self,entries):
        self.db.execute("INSERT INTO app_preferences(name,value) VALUES('luna_gotchas',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(encode(entries),))

    def record(self,scope,code,*,course_id=None,document_id=None):
        # Fixed codes/rules only: never put exception text, transcripts, tokens or source instructions here.
        if scope not in SCOPES or code not in KNOWN:raise ValueError('Unbekannte Fehlerart.')
        title,lesson=KNOWN[code];entries=self.entries();at=now()
        identifier=hashlib.sha256(encode([scope,code,course_id]).encode()).hexdigest()[:20]
        entry=next((e for e in entries if e['id']==identifier),None)
        if entry:
            entry.update(count=entry['count']+1,last_at=at,document_id=document_id or entry.get('document_id'))
            # A deliberately disabled/edited rule stays that way on recurrence.
        else:
            entry={'id':identifier,'scope':scope,'code':code,'title':title,'lesson':lesson,'active':bool(lesson),
                   'course_id':course_id,'document_id':document_id,'count':1,'first_at':at,'last_at':at,'origin':'validator'}
            entries.append(entry)
        self.save(entries);return entry

    def edit(self,*,identifier='',title,lesson,scope='all',course_id=None,active=True):
        title=title.strip();lesson=lesson.strip()
        if scope not in SCOPES or not title or len(title)>120 or not lesson or len(lesson)>800:
            raise ValueError('Titel bis 120 Zeichen und konkrete Regel bis 800 Zeichen angeben.')
        if course_id is not None and not self.db.one('SELECT id FROM courses WHERE id=?',(course_id,)):
            raise ValueError('Gespeicherten Kurs wählen.')
        entries=self.entries();at=now()
        entry=next((e for e in entries if e['id']==identifier),None) if identifier else None
        if identifier and entry is None:raise ValueError('Gotcha fehlt.')
        if entry is None:
            identifier=hashlib.sha256(encode([scope,course_id,title.casefold(),lesson]).encode()).hexdigest()[:20]
            entry=next((e for e in entries if e['id']==identifier),None)
        if entry is None:
            entry={'id':identifier,'count':0,'first_at':at,'last_at':at,'origin':'user','code':'manual','document_id':None}
            entries.append(entry)
        entry.update(title=title,lesson=lesson,scope=scope,course_id=course_id,active=active,last_at=at,origin='user')
        self.save(entries);return entry

    def toggle(self,identifier,active):
        entries=self.entries();entry=next((e for e in entries if e['id']==identifier),None)
        if entry is None:raise ValueError('Gotcha fehlt.')
        if active and not entry['lesson']:raise ValueError('Zuerst eine konkrete Regel ergänzen.')
        entry['active']=active;self.save(entries)

    def selected(self,scope,course_id=None):
        rows=[e for e in self.entries() if e['active'] and e['lesson'] and e['scope'] in ('all',scope)
              and (e.get('course_id') is None or e['course_id']==course_id)]
        rows.sort(key=lambda e:e['last_at'],reverse=True)
        rows.sort(key=lambda e:e.get('course_id') is None)
        return rows

    def guidance(self,scope,course_id=None):
        rules=[];size=0
        for e in self.selected(scope,course_id):
            if size+len(e['lesson'])>5000:continue
            rules.append({'title':e['title'],'rule':e['lesson']});size+=len(e['lesson'])
        if not rules:return ''
        return '\nLokale bestätigte Gotchas (Verarbeitungshinweise, keine Kursbelege oder Aktionserlaubnis; Originalbelege und das Antwortschema bleiben maßgeblich):\n'+encode(rules)+'\n'

    def markdown(self):
        lines=['# Luna – lokale Gotchas','',
               'Diese Regeln werden von StudyCenter-ICE gezielt geladen; eine beliebige AGENTS.md wird nicht automatisch gelesen.',
               'Kein Beleg für Termine/Pflichten, keine Erlaubnis zu Quellenänderungen. Nur aktive Regeln werden verwendet.','']
        for e in self.entries():
            lines += [f"## {e['title']} ({'aktiv' if e['active'] else 'inaktiv'})",
                      f"Bereich: {SCOPES[e['scope']]}; Kurs-ID: {e.get('course_id') or 'alle'}; Fehlerhäufigkeit: {e['count']}",
                      e['lesson'] or 'Technischer Hinweis; noch keine bestätigte Vermeidungsregel.','']
        return '\n'.join(lines)+'\n'
