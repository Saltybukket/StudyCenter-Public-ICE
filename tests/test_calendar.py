from studycenter_ice.app import calendar_events
from studycenter_ice.models import Candidate


def test_ics_grounded_dates_personal_scope_stable_uid_and_utf8(tmp_path):
    from datetime import date, datetime
    from studycenter_ice.app import action_state, create_app
    from studycenter_ice.config import Settings, VIENNA
    from studycenter_ice.ical import export_calendar
    from fastapi.testclient import TestClient

    def item(identifier, **changes):
        extras = {key: changes.pop(key) for key in ('personal_event','ignored','conflict','status','date_conflict','course_group') if key in changes}
        defaults = dict(title='Überprüfung, mit; Beleg ' + 'ä' * 100, kind='event', date='2026-10-24', time='14:00', date_meaning='start', obligation='required', quote='Belegter eigener Termin', anchor='calendar', page=1)
        if changes.get('kind') in ('task','registration'):
            defaults.pop('time')
        value = Candidate(**(defaults | changes)).model_dump(mode='json')
        return {'id':identifier,'identity':'stable-identity-'+str(identifier),'data':value,'status':'open','ignored':0,'conflict':0,'course_id':1,'course_title':'CON','changed_at':'2026-10-01T12:00:00+00:00', **extras}
    rows = [item(1, end_date='2026-10-24', end_time='16:00', personal_event=True),
            item(2, kind='task',date='2026-10-12',time='08:00',end_date='2026-10-27',date_meaning='window',obligation='required'),
            item(3, time=None,end_date='2026-10-26',personal_event=True),
            item(4, obligation='optional'), item(5, ignored=1), item(6, source_state='cancelled'),
            item(7, uncertainty='Keine persönliche Zuteilung, angebotener Slot'), item(8, date_conflict=True),
            item(9),  # No known end: must not invent duration.
            item(10, kind='registration',date='2026-10-15',end_date='2026-10-28',end_time='23:59',date_meaning='window',obligation='required')]
    states = {i['id']:action_state(i, datetime(2026,10,12,tzinfo=VIENNA)) for i in rows}
    raw, count = export_calendar(rows, calendar_events(rows), states)
    assert count == 5
    assert all(len(line) <= 75 for line in raw.split(b'\r\n'))
    text = raw.decode().replace('\r\n ', '')
    assert 'DTSTART;TZID=Europe/Vienna:20261024T140000' in text
    assert 'DTEND;TZID=Europe/Vienna:20261024T160000' in text
    assert 'DTSTART;VALUE=DATE:20261027' in text and '20261012T080000' not in text
    assert 'DTEND;VALUE=DATE:20261027' in text  # 24th to 26th inclusive.
    assert 'DTSTART;TZID=Europe/Vienna:20261028T235900' in text
    assert 'SUMMARY:Überprüfung\\, mit\\; Beleg' in text
    assert 'DTSTART:20261025T030000\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100' in text  # Vienna DST transition.
    events = text.split('BEGIN:VEVENT\r\n')[1:]
    no_end = next(e for e in events if 'DTSTART;TZID=Europe/Vienna:20261024T140000' in e and 'DTEND' not in e)
    assert 'DURATION' not in no_end
    again, _ = export_calendar(rows, calendar_events(rows), states)
    uids = lambda value: [line for line in value.decode().replace('\r\n ', '').split('\r\n') if line.startswith('UID:')]
    assert uids(raw) == uids(again) and len(set(uids(raw))) == count
    filtered, n = export_calendar(rows, calendar_events(rows), states, date(2026,10,27), date(2026,10,27))
    assert n == 1 and uids(filtered)[0] in uids(raw)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get('/calendar/export.ics?start=&end=').status_code == 200
        assert client.get('/calendar/export.ics?start=2026-10-20&end=2026-10-10').status_code == 400


def test_calendar_preserves_spans_unknown_start_and_local_time_precision():
    def item(id, **values):
        candidate = Candidate(title='Belegter Termin',kind='task',quote='Von Angabe bis Abgabe',page=1,anchor='range',**values)
        return {'id':id,'data':candidate.model_dump(mode='json'),'status':'doing','ignored':0,'conflict':0,'course_title':'CON'}
    rows = [item(1,date='2026-10-12',end_date='2026-10-27',date_meaning='deadline'),
            item(2,date='2026-10-27',date_meaning='deadline'),
            item(3,date='2026-10-13',time='18:00',end_date='2026-10-13',end_time='19:00',date_meaning='start'),
            item(4,date='2026-10-20',date_meaning='deadline',source_state='cancelled')]
    events = calendar_events(rows)
    assert [e['id'] for e in events] == ['1','2','3']
    assert events[0]['allDay'] and events[0]['start'] == '2026-10-27' and 'end' not in events[0]
    assert events[0]['extendedProps']['sourceDate'] == '2026-10-12'  # Gantt retains the work window.
    assert events[0]['extendedProps']['sourceEndDate'] == '2026-10-27'  # Inclusive source date, not renderer boundary.
    assert events[0]['extendedProps']['status'] == 'doing'
    assert events[1]['extendedProps']['sourceDate'] == '2026-10-27'
    assert events[1]['extendedProps']['sourceEndDate'] is None and 'end' not in events[1]
    assert events[1]['extendedProps']['dateMeaning'] == 'deadline'
    assert events[2]['start'] == '2026-10-13T18:00:00' and events[2]['end'] == '2026-10-13T19:00:00'
    assert events[2]['extendedProps']['sourceTime'] == '18:00:00'


def test_calendar_asset_urls_change_with_content_and_html_is_fresh(tmp_path, monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from studycenter_ice import app as module
    from studycenter_ice.config import Settings

    with TestClient(module.create_app(Settings(data_dir=tmp_path / 'data'))) as client:
        page = client.get('/calendar')
        assert page.headers['cache-control'] == 'no-store'
        url = re.search(r'src="(/static/calendar.js\?v=[a-f0-9]{16})"', page.text)[1]
        assert url == module.static_url('calendar.js')
        script = client.get(url).text
        assert "slotMinTime:'08:00:00', slotMaxTime:'22:00:00'" in script
        assert 'headerToolbar:false' in script
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    assets = tmp_path / 'studycenter_ice' / 'static'
    assets.mkdir(parents=True)
    asset = assets / 'calendar.js'
    asset.write_text('old calendar')
    old = module.static_url('calendar.js')
    asset.write_text('new calendar')
    assert module.static_url('calendar.js') != old


def test_timeline_excludes_done_overdue_inventory_and_caps_day_rows(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from fastapi.testclient import TestClient
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings, VIENNA

    now = datetime.now(VIENNA)
    future = (now.date() + timedelta(days=1)).isoformat()
    def row(identifier, title, **changes):
        status = changes.pop('status', 'open')
        value = Candidate(title=title, kind='task', date=future, date_meaning='deadline',
                          obligation='required', quote='Eigene belegte Planung', page=1, anchor=str(identifier)).model_dump(mode='json')
        value.update(changes)
        return {'id':identifier, 'data':value, 'status':status, 'ignored':0, 'conflict':0,
                'pinned':0, 'identity':f'note:{identifier}', 'course_id':None, 'course_title':None, 'seen':0}
    rows = [row(n, f'Offene Abgabe {n}') for n in range(1, 8)]
    rows += [row(100, 'Schon erledigt', status='done'), row(101, 'Vergangene Abgabe', date=(now.date()-timedelta(days=1)).isoformat())]
    rows += [row(n, f'Undatierter Bestand {n}', date=None, date_meaning='none') for n in range(200,900)]
    rows += [row(999, 'Unbestätigt angebotener Slot', kind='event', date_meaning='start',
                 uncertainty='Angebotener Slot, keine persönliche Zuteilung')]
    rows[-1]['identity'] = 'source:offered-slot'
    app = create_app(Settings(data_dir=tmp_path))
    monkeypatch.setattr(app.state.db, 'items', lambda **kwargs: rows)
    with TestClient(app) as client:
        response = client.get('/')
    assert response.status_code == 200
    html = response.text
    assert html.count('data-day=') == 15 and html.count('data-timeline-item=') == 3
    assert '4 weitere im Kalender' in html and '1 offene vergangene Fristen' in html
    assert all(text not in html for text in ('Schon erledigt','Vergangene Abgabe','Undatierter Bestand','Unbestätigt angebotener Slot'))
    assert '/entries?view=updates' in html and '/entries?view=undated' in html and '/archive' in html
    assert len(response.content) < 40000


def test_timeline_lookback_includes_completed_past_not_offers_and_keeps_filters(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from bs4 import BeautifulSoup
    from fastapi.testclient import TestClient
    from studycenter_ice import app as module
    from studycenter_ice.config import Settings, VIENNA
    fixed = datetime(2030, 10, 31, 12, tzinfo=VIENNA)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(fixed.timestamp(), tz)
    monkeypatch.setattr(module, 'datetime', FixedDatetime)
    def row(identifier, days, status='open', **changes):
        value = Candidate(title='Eintrag '+str(identifier), kind='task',
                          date=(fixed.date()+timedelta(days=days)).isoformat(),
                          date_meaning='deadline', obligation='required',
                          quote='Belegter Eintrag', anchor=str(identifier), page=1).model_dump(mode='json')
        value.update(changes)
        return dict(id=identifier, data=value, status=status, ignored=0, conflict=0,
                    pinned=0, identity='source:'+str(identifier), course_id=1, course_title='CON')
    rows = [row(1, 1), row(2, -20, 'done'), row(3, -5), row(4, -31), row(5, 2, 'done'),
            row(6, -8, source_state='cancelled'),
            row(7, -6, kind='event', date_meaning='start',
                uncertainty='Angebotener Slot, keine persönliche Zuteilung')]
    before = [dict(i, data=dict(i['data'])) for i in rows]
    app = module.create_app(Settings(data_dir=tmp_path))
    monkeypatch.setattr(app.state.db, 'items', lambda **kwargs: rows)
    client = TestClient(app)
    def view(query):
        response = client.get('/'+query)
        assert response.status_code == 200
        return BeautifulSoup(response.text, 'html.parser')
    for query, expected, first in [('', {1}, '2030-10-31'),
                                  ('?past=7', {1,3}, '2030-10-24'),
                                  ('?past=30', {1,2,3}, '2030-10-01'),
                                  ('?past=999', {1,2,3}, '2030-10-01'),
                                  ('?past=-1', {1}, '2030-10-31')]:
        dom = view(query)
        assert {int(i['data-timeline-item']) for i in dom.select('[data-timeline-item]')} == expected
        assert dom.select_one('[data-day]')['data-day'] == first
        assert dom.select_one('#time-jump')['min'] == first
        if 2 in expected:
            assert 'Erledigt' in dom.select_one('[data-timeline-item="2"]').get_text()
    dom = view('?past=30&days=7&course=1&kind=task&include_inactive=true')
    assert len(dom.select('[data-day]')) == 38
    assert dom.select_one('input[name="past"]')['value'] == '30'
    for link in dom.select('.timeline-ranges a'):
        assert 'course=1' in link['href'] and 'kind=task' in link['href'] and 'include_inactive=true' in link['href']
    assert rows == before


async def test_timeline_initial_today_centered_for_ranges_and_lookback(tmp_path, monkeypatch):
    from pathlib import Path
    from urllib.parse import urlsplit
    import pytest
    from datetime import datetime
    from fastapi.testclient import TestClient
    from playwright.async_api import async_playwright
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings, VIENNA
    root = Path(__file__).resolve().parents[1]
    if (root/'.local/browsers').exists():
        monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(root/'.local/browsers'))
    client = TestClient(create_app(Settings(data_dir=tmp_path)))
    centered = """() => {
        const view=document.querySelector('.time-scroll'), day=document.querySelector('.time-day.is-today');
        if(!view || !day)return false;
        const v=view.getBoundingClientRect(),d=day.getBoundingClientRect();
        return Math.abs(d.left+d.width/2-v.left-view.clientLeft-view.clientWidth/2)<2;
    }"""
    async with async_playwright() as driver:
        if not Path(driver.chromium.executable_path).exists(): pytest.skip('Project Chromium not installed')
        browser = await driver.chromium.launch(headless=True)
        page = await browser.new_page(reduced_motion='reduce')
        async def serve(route):
            url = urlsplit(route.request.url)
            assert url.hostname == 'studycenter_ice.test'
            response = client.get(url.path+('?' + url.query if url.query else ''))
            await route.fulfill(status=response.status_code, body=response.content,
                                content_type=response.headers.get('content-type','application/octet-stream'))
        await page.route('**/*', serve)
        for width in (1280,390):
            await page.set_viewport_size({'width':width,'height':900})
            for query in ('?days=7','?days=30','?past=7&days=14','?past=30&days=30'):
                await page.goto('http://studycenter_ice.test/'+query)
                await page.wait_for_function(centered)
                assert await page.locator('#time-jump').input_value() == datetime.now(VIENNA).date().isoformat()
            await page.locator('.time-scroll').evaluate("e => e.scrollBy({left:-400,behavior:'instant'})")
            assert not await page.evaluate(centered)
            await page.get_by_role('button',name='Heute',exact=True).click()
            await page.wait_for_function(centered)
        await browser.close()


def test_week_projection_removes_work_window_bars_preserving_source_spans():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest

    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js needed for the pure calendar projection check')
    script = Path(__file__).parents[1] / 'studycenter_ice' / 'static' / 'calendar.js'
    probe = """
const fs=require('fs'),vm=require('vm');
const sandbox={document:{addEventListener(){}}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const events=[
 {id:'registration',start:'2026-10-01T08:00:00',end:'2026-10-27T23:59:00',allDay:false,extendedProps:{itemKind:'registration',dateMeaning:'window',sourceDate:'2026-10-01',sourceTime:'08:00:00',sourceEndDate:'2026-10-27',sourceEndTime:'23:59:00',closingDate:'2026-10-27',closingTime:'23:59:00'}},
 {id:'work',start:'2026-10-27',allDay:true,extendedProps:{itemKind:'task',dateMeaning:'deadline',sourceDate:'2026-10-01',sourceEndDate:'2026-10-27',closingDate:'2026-10-27',closingTime:null}},
 {id:'period',start:'2026-10-01T10:00:00',end:'2026-10-27T18:00:00',allDay:false,extendedProps:{itemKind:'event',dateMeaning:'window',sourceDate:'2026-10-01',sourceEndDate:'2026-10-27',personal:false}},
 {id:'personal',start:'2026-10-15T14:00:00',end:'2026-10-15T16:00:00',allDay:false,extendedProps:{itemKind:'event',dateMeaning:'start',sourceDate:'2026-10-15',sourceEndDate:'2026-10-15',personal:true}},
 {id:'unknown-end',start:'2026-10-16T14:00:00',allDay:false,extendedProps:{itemKind:'exam',dateMeaning:'start',sourceDate:'2026-10-16',personal:true}}
];
const original=JSON.stringify(events);const result=sandbox.calendarWeekEvents(events);
process.stdout.write(JSON.stringify({result,unchanged:original===JSON.stringify(events)}));
"""
    completed = subprocess.run([node, '-e', probe, str(script)], capture_output=True, text=True, check=True)
    output = json.loads(completed.stdout)
    events = {event['id']:event for event in output['result']}
    assert output['unchanged'] and set(events) == {'registration','work','personal','unknown-end'}
    assert events['registration']['start'] == '2026-10-27T23:59:00' and 'end' not in events['registration']
    assert events['work']['start'] == '2026-10-27' and events['work']['allDay'] and 'end' not in events['work']
    assert events['work']['extendedProps']['sourceDate'] == '2026-10-01'
    assert events['personal']['end'] == '2026-10-15T16:00:00'
    assert 'end' not in events['unknown-end']


def test_calendar_shorthand_keeps_course_identity_and_full_source_titles():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest

    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js needed for calendar display shorthand')
    script = Path(__file__).parents[1] / 'studycenter_ice' / 'static' / 'calendar.js'
    titles = ['Elektronische Schaltungstechnik 2 · Vorlesung',
              'Computer Organization and Networks · Lehrveranstaltung',
              'Wahrscheinlichkeitstheorie für Informatikstudien · Übung',
              'Entwurf von Echtzeitsystemen (VO)', 'Messtechnik (UE)',
              'Übungsgruppe zur Schaltungstechnik',
              'Sehr langer unbekannter Kursname mit einer konkreten Aufgabe und weiteren Angaben',
              'Messtechnik (VO) · Vorlesung', 'Entwurf von Echtzeitsystemen (UE) · Übung',
              'Physik für Electrical and Electronics Engineering · Vorlesung']
    probe = """
const fs=require('fs'),vm=require('vm');
const sandbox={document:{addEventListener(){}}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const titles=JSON.parse(process.argv[2]),before=JSON.stringify(titles);
process.stdout.write(JSON.stringify({labels:titles.map(t=>sandbox.calendarShortLabel(t)),unchanged:before===JSON.stringify(titles)}));
"""
    result = subprocess.run([node, '-e', probe, str(script), json.dumps(titles)], capture_output=True, text=True, encoding='utf-8', check=True)
    output = json.loads(result.stdout)
    assert output['unchanged']
    assert output['labels'][:5] == ['ES2 · VO', 'CON · LV', 'WFI · UE', 'EES (VO)', 'MT (UE)']
    assert output['labels'][5] == titles[5]  # Never replace a term inside a longer word.
    assert output['labels'][6].startswith('Sehr langer unbekannter Kursname')
    assert output['labels'][6].endswith('…') and len(output['labels'][6]) <= 62
    assert output['labels'][7:] == ['MT · VO', 'EES · UE', 'Physik · VO']


def test_calendar_detail_payload_preserves_unknown_duration_and_booking_scope():
    value = Candidate(title='Elektronische Schaltungstechnik 2 · Vorlesung', kind='event',
                      description='Ort laut gespeichertem Beleg: HS i7', date='2026-10-15', time='10:15',
                      date_meaning='start', quote='Persönlicher Kalendertermin', page=1, anchor='calendar').model_dump(mode='json')
    row = {'id':1,'data':value,'status':'open','ignored':0,'conflict':0,
           'personal_event':True,'course_title':'Elektronische Schaltungstechnik 2'}
    event = calendar_events([row])[0]
    assert event['title'] == value['title'] and event['url'] == '/items/1'
    assert event['extendedProps']['description'] == value['description']
    assert event['extendedProps']['personal'] and not event['extendedProps']['bookingProven']
    assert event['extendedProps']['sourceEndTime'] is None and 'end' not in event


def test_course_palette_stays_distinct_and_keeps_calendar_identity():
    import colorsys
    import re
    from studycenter_ice.app import COURSE_COLORS, course_color

    assert len(COURSE_COLORS) == len(set(COURSE_COLORS)) == 8
    for color in COURSE_COLORS:
        assert re.fullmatch(r'#[0-9a-f]{6}', color)
        rgb = tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        hue, lightness, saturation = colorsys.rgb_to_hls(*rgb)
        assert 100 <= hue * 360 <= 340  # Course accents exclude brown/ochre hues.
        assert .25 <= saturation <= .65 and .3 <= lightness <= .6
    assert course_color(None) == COURSE_COLORS[0]
    assert course_color(1) == course_color('9') == COURSE_COLORS[1]
    value = Candidate(title='Belegter eigener Termin', kind='event', date='2026-10-15', time='10:15',
                      end_date='2026-10-15', end_time='11:45', date_meaning='start',
                      quote='Kalenderquelle', page=1, anchor='calendar').model_dump(mode='json')
    row = {'id':42,'data':value,'status':'open','ignored':0,'conflict':0,'personal_event':True,'course_id':9}
    event = calendar_events([row])[0]
    assert event['extendedProps']['courseColor'] == COURSE_COLORS[1]
    assert (event['id'],event['title'],event['url'],event['start'],event['end']) == (
        '42',value['title'],'/items/42','2026-10-15T10:15:00','2026-10-15T11:45:00')
    assert row['data'] == value  # Display colors never rewrite the stored source values.
