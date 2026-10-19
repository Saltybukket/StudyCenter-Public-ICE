"""One source-bound tag vocabulary for cards, agenda, calendar and details."""
import re

TAGS = {
    'submission': ('Pflicht zur Abgabe', 'obligation', 'Eine verpflichtende Abgabe; keine Aussage über Anwesenheit.'),
    'attendance': ('Anwesenheitspflicht', 'obligation', 'Anwesenheit ausdrücklich belegt oder von dir persönlich festgelegt.'),
    'required': ('Verpflichtend', 'obligation', 'Verpflichtender Auftrag; allein daraus folgt keine Anwesenheitspflicht.'),
    'recommended': ('Empfohlen', 'obligation', 'Empfehlung, keine verpflichtende Abgabe oder Teilnahme.'),
    'optional': ('Optional', 'obligation', 'Freiwilliges Angebot.'),
    'unclear': ('Pflichtstatus unklar', 'information', 'Die gespeicherten Belege bestimmen den Pflichtstatus nicht eindeutig.'),
    'booked': ('Buchung belegt', 'personal', 'Dieser konkrete Termin ist als persönliche Buchung belegt; dies bedeutet weder Pflicht noch Erledigt.'),
    'assigned': ('Eigene Einheit belegt', 'personal', 'Konkrete Einheit aus Gruppenplan und bestätigter eigener LV-Gruppe; keine individuelle Slotbuchung.'),
    'calendar': ('Persönlicher Kalendertermin', 'personal', 'Im persönlichen TU-Kalender gefunden; bestätigt allein keinen konkreten Labslot.'),
    'planned': ('Von dir geplant', 'personal', 'Eigene Planung oder persönliche Einstellung; kein offizieller Buchungsbeleg.'),
    'offered': ('Angebotener Termin, nicht gebucht', 'personal', 'Ein angebotener Slot ohne konkrete eigene Buchung.'),
    'unassigned': ('Zuordnung offen', 'personal', 'Für diesen Termin fehlt die eindeutige persönliche Zuordnung.'),
    'conflict': ('Quellen widersprechen sich', 'information', 'Aktuelle Belege enthalten abweichende Angaben; Details vergleichen.'),
    'decided': ('Von dir geklärt', 'personal', 'Persönliche Entscheidung zu einer Quellenangabe; kein offizieller Buchungsnachweis.'),
    'decision-stale': ('Neue Quelle – Entscheidung prüfen', 'information', 'Die bisherige persönliche Entscheidung bleibt wirksam; aktuelle Fundstellen erneut vergleichen.'),
    'date': ('Datum offen', 'information', 'Kein eindeutiges Datum gespeichert.'),
    'time': ('Uhrzeit nicht genannt', 'information', 'Nur ein Datum bekannt; keine präzise Vorwarnzeit berechenbar.'),
    'source': ('Quellenangabe unklar', 'information', 'Der aktuelle Quellenstatus ist unklar; den konkreten Hinweis öffnen.'),
    'cancelled': ('Abgesagt / aufgehoben', 'information', 'Keine aktive Verpflichtung oder Erinnerung.'),
    'open': ('Offen', 'work', 'Dein persönlicher Bearbeitungsstand, unabhängig von der Buchung.'),
    'doing': ('In Arbeit', 'work', 'Dein persönlicher Bearbeitungsstand; bleibt beim Sync erhalten.'),
    'done': ('Erledigt', 'work', 'Von dir als erledigt markiert; offene Abgabe-Erinnerungen entfallen.'),
}


def personal_state(item):
    """User planning is personal, while official booking remains source-bound."""
    value = item['data']
    planned = item.get('identity', '').startswith('note:') or bool(item.get('reminder_profile', {}).get('personal'))
    booked = bool(item.get('booked_event'))
    personal = planned or booked or bool(item.get('personal_event'))
    offered = not personal and (value.get('obligation') in ('optional', 'recommended') or bool(re.search(
        r'keine? persönliche.*(?:buchung|zuteilung)|kein.*persönlicher slot|no personal.*(?:slot|assignment)|alternative termine',
        value.get('uncertainty') or '', re.I)))
    return {'booking_proven': booked, 'personally_planned': planned, 'personal': personal, 'offered': offered}


def attendance_type(item):
    value = item['data']
    if value['kind']!='event':return None
    profile = item.get('reminder_profile', {})
    if profile.get('attendance') in ('exercise', 'lecture'):
        return profile['attendance']
    if item.get('personal_resolution',{}).get('issues'): return None
    if item.get('official_attendance'): return item['official_attendance']
    if value.get('obligation')!='required':return None
    # Read only the actual claim, not an unrelated PDF coverage warning.
    text = ' '.join(str(value.get(k) or '') for k in ('title', 'quote', 'description'))
    if re.search(r'keine?\s+Anwesenheitspflicht|no\s+(?:mandatory|required|compulsory)\s+attendance|attendance\s+(?:is\s+)?(?:optional|not\s+(?:mandatory|required|compulsory))', text, re.I):
        return None
    if not re.search(r'Anwesenheitspflicht|Anwesenheit\s+(?:ist\s+)?(?:verpflichtend|erforderlich)|mandatory\s+attendance|attendance\s+(?:is\s+)?(?:mandatory|required|compulsory)', text, re.I):
        return None
    if re.search(r'Vorlesung|\bVO\b|lecture', text+' '+(item.get('course_title') or ''), re.I):
        return 'lecture'
    if re.search(r'Übung|\bUE\b|\bLU\b|\blab\b|exercise|setup\s+session', text+' '+(item.get('course_title') or ''), re.I):
        return 'exercise'
    return None


def item_tags(item):
    value = item['data']; own = item.get('identity', '').startswith('note:')
    personal = personal_state(item)
    codes = []
    obligation = value.get('obligation', 'unclear')
    codes.append('submission' if obligation == 'required' and value['kind'] == 'task' and value.get('date_meaning') in ('deadline','window')
                 else 'attendance' if attendance_type(item) else obligation)
    if personal['booking_proven']:
        codes.append('booked')
    elif personal['personally_planned']:
        codes.append('planned')
    elif item.get('personal_resolution',{}).get('units'):
        codes.append('assigned')
    elif item.get('personal_event'):
        codes.append('calendar')
    elif value['kind'] in ('event','exam'):
        codes.append('offered' if obligation in ('optional','recommended') or re.search(r'angebot|alternative|keine? persönliche.*buchung',value.get('uncertainty') or '',re.I) else 'unassigned')
    if item.get('conflict'): codes.append('conflict')
    if item.get('personal_decisions'): codes.append('decided')
    if any(d.get('stale') for d in item.get('personal_decisions',[])): codes.append('decision-stale')
    if value.get('source_state') == 'cancelled': codes.append('cancelled')
    elif value.get('source_state') == 'unclear': codes.append('source')
    when = value.get('end_date') if value.get('date_meaning') in ('window','deadline') and value.get('end_date') else value.get('date')
    clock = value.get('end_time') if value.get('date_meaning') in ('window','deadline') and value.get('end_date') else value.get('time')
    if not when: codes.append('date')
    elif not clock: codes.append('time')
    codes.append(item.get('status','open'))
    result = [{'code': c, 'label': TAGS[c][0], 'dimension': TAGS[c][1], 'help': TAGS[c][2]} for c in dict.fromkeys(codes) if c in TAGS]
    uncertainty = value.get('uncertainty') or ''
    # A generic document-reading gap is coverage information, not evidence that every date is wrong.
    if uncertainty and not own and not re.search(r'OCR|Grafik|Bild|Leselücke|nicht geles|unread', uncertainty, re.I):
        result.append({'code':'specific','label':uncertainty[:100], 'dimension':'information','help':uncertainty})
    return result
