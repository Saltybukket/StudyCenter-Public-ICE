"""RFC 5545 download, using the existing grounded calendar projection."""
import hashlib
from datetime import date, datetime, timedelta, timezone

from .config import VIENNA


def escape(value):
    return str(value).replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def offset(value):
    minutes = int(value.total_seconds() / 60)
    return ('+' if minutes >= 0 else '-') + f'{abs(minutes)//60:02}{abs(minutes)%60:02}'


def timezone_lines(first, last):
    # Explicit transitions from installed tzdata, including the offset preceding the first event.
    lines = ['BEGIN:VTIMEZONE', 'TZID:Europe/Vienna']
    current = datetime(max(1, first - 1), 1, 1, tzinfo=timezone.utc)
    finish = datetime(min(9999, last + 1), 12, 31, tzinfo=timezone.utc)
    initial = current.astimezone(VIENNA)
    lines += ['BEGIN:STANDARD', 'DTSTART:' + initial.strftime('%Y%m%dT%H%M%S'),
              'TZOFFSETFROM:' + offset(initial.utcoffset()), 'TZOFFSETTO:' + offset(initial.utcoffset()), 'END:STANDARD']
    while current < finish:
        following = current + timedelta(days=1)
        before, after = current.astimezone(VIENNA), following.astimezone(VIENNA)
        if before.utcoffset() != after.utcoffset():
            low, high = current, following
            while (high - low).total_seconds() > 1:
                middle = low + timedelta(seconds=int((high - low).total_seconds() // 2))
                if middle.astimezone(VIENNA).utcoffset() == before.utcoffset():
                    low = middle
                else:
                    high = middle
            kind = 'DAYLIGHT' if after.dst() else 'STANDARD'
            onset = high.replace(tzinfo=None) + before.utcoffset()
            lines += ['BEGIN:' + kind, 'DTSTART:' + onset.strftime('%Y%m%dT%H%M%S'),
                      'TZOFFSETFROM:' + offset(before.utcoffset()), 'TZOFFSETTO:' + offset(after.utcoffset()), 'END:' + kind]
        current = following
    return lines + ['END:VTIMEZONE']


def export_calendar(items, events, states, start=None, end=None):
    selected = []
    by_id = {event['id']: event for event in events}
    for item in items:
        value, state = item['data'], states[item['id']]
        own_note = item.get('identity', '').startswith('note:')
        if (not state['relevant'] or state['offered'] or value['source_state'] != 'active'
                or item.get('date_conflict', item['conflict']) or value['date_meaning'] in ('none', 'unclear', 'recommended')):
            continue
        if not (state['personal'] or state['appointment'] or state['closing'] and value['obligation'] == 'required' or own_note):
            continue
        event = by_id.get(str(item['id']))
        if not event:
            continue
        if state['closing']:
            event = {**event, 'start': state['action_date'] + ('T' + state['action_time'] if state['action_time'] else ''),
                     'allDay': not state['action_time']}
            event.pop('end', None)  # A closing deadline is a point, even if its opening time is unknown.
        first = date.fromisoformat(event['start'][:10])
        final = date.fromisoformat(event.get('end', event['start'])[:10])
        if event['allDay'] and event.get('end'):
            final -= timedelta(days=1)
        if start and final < start or end and first > end:
            continue
        selected.append((item, event, state))
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//StudyCenter-ICE//Local Calendar 1//DE', 'CALSCALE:GREGORIAN', 'X-WR-CALNAME:StudyCenter-ICE', 'X-WR-TIMEZONE:Europe/Vienna']
    timed = [event for _, event, _ in selected if not event['allDay']]
    if timed:
        years = [int(event[key][:4]) for event in timed for key in ('start', 'end') if key in event]
        lines += timezone_lines(min(years), max(years))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    for item, event, state in selected:
        # Identity is independent of titles, dates, filters and machine location; retained by backups.
        uid = hashlib.sha256(item['identity'].encode()).hexdigest() + '@studycenter_ice.local'
        changed = datetime.fromisoformat(item['changed_at']).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        lines += ['BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:' + stamp, 'LAST-MODIFIED:' + changed, 'CLASS:PRIVATE', 'SUMMARY:' + escape(event['title'])]
        for key, property_name in (('start', 'DTSTART'), ('end', 'DTEND')):
            if key not in event:
                continue  # No fabricated end time or duration.
            if event['allDay']:
                lines.append(property_name + ';VALUE=DATE:' + event[key].replace('-', ''))
            else:
                lines.append(property_name + ';TZID=Europe/Vienna:' + datetime.fromisoformat(event[key]).strftime('%Y%m%dT%H%M%S'))
        description = (item.get('course_title') or 'Eigener Eintrag') + ' · ' + state['action_label'] + '\n' + item['data']['description']
        description += '\nFundstellen und Bearbeitung: http://127.0.0.1:18791/items/' + str(item['id'])
        if item.get('personal_decisions'):
            description += '\nPersönliche Entscheidung – von dir geklärt; keine offizielle Buchung.'
            if any(d.get('stale') for d in item['personal_decisions']): description += '\nNeue Quelle – Entscheidung prüfen.'
        lines += ['DESCRIPTION:' + escape(description), 'CATEGORIES:' + escape(state['action_label']),
                  'TRANSP:' + ('OPAQUE' if state['appointment'] and not state['closing'] else 'TRANSPARENT'), 'END:VEVENT']
    lines.append('END:VCALENDAR')
    # Fold at 75 octets without splitting UTF-8 codepoints; continuation includes its leading space.
    folded = []
    for line in lines:
        part = ''
        for character in line:
            if len((part + character).encode('utf-8')) > 75:
                folded.append(part)
                part = ' '
            part += character
        folded.append(part)
    return ('\r\n'.join(folded) + '\r\n').encode('utf-8'), len(selected)
