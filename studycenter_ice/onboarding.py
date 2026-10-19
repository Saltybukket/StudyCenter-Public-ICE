"""An optional, resumable tour of the real ICE settings. No account side effects."""
import json
from typing import Literal

from fastapi import Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .db import encode

Step = Literal['welcome', 'courses', 'model', 'preferences', 'components', 'connections', 'finish']
STEPS = (
    ('welcome', 'Willkommen', 'Dein persönlicher Start'),
    ('courses', 'Kurse & Quellen', 'Deine Unterlagen zusammenbringen'),
    ('model', 'KI-Verbindung', 'Assistenz nach deiner Wahl'),
    ('preferences', 'Dein Alltag', 'Meldungen & Darstellung'),
    ('components', 'Optionale Extras', 'Browser, OCR & Sprache'),
    ('connections', 'Verbindungen', 'Nur die Konten, die du brauchst'),
    ('finish', 'Bereit für dein Studium', 'Sicherung & nächste Schritte'),
)
DESTINATIONS = {
    'courses': ('courses', '/sources#course-create'),
    'source': ('courses', '/sources#source-create'),
    'model': ('model', '/model'),
    'notifications': ('preferences', '/settings#notifications'),
    'display': ('preferences', '/settings#display'),
    'components': ('components', '/settings#background'),
    'speech': ('components', '/settings#speech'),
    'mail': ('connections', '/mail'),
    'google': ('connections', '/settings#google'),
    'connections': ('connections', '/settings#connections'),
    'backups': ('finish', '/settings#backups'),
    'operation': ('finish', '/settings#background'),
}


def progress(db):
    row = db.one("SELECT value FROM app_preferences WHERE name='onboarding'")
    try:
        value = json.loads(row['value']) if row else {}
        if not isinstance(value, dict):
            value = {}
    except (ValueError, TypeError):
        value = {}
    step = value.get('step')
    return {
        'step': step if isinstance(step, str) and step in {s[0] for s in STEPS} else 'welcome',
        'status': value.get('status') if value.get('status') in ('active', 'paused', 'done') else 'new',
    }


def first_launch(db):
    return (progress(db)['status'] == 'new'
            and not any(db.one(f'SELECT id FROM {table} LIMIT 1')
                        for table in ('courses', 'items', 'sources', 'documents')))


def register(app, db, settings, render):
    @app.get('/welcome')
    async def welcome(request: Request):
        saved = progress(db)
        index = next(i for i, step in enumerate(STEPS) if step[0] == saved['step'])
        return render(request, 'onboarding.html', steps=STEPS, current=index,
                      tour=saved,
                      setup_courses=db.one('SELECT COUNT(*) n FROM courses')['n'],
                      setup_sources=db.one('SELECT COUNT(*) n FROM sources')['n'],
                      setup_model=settings.model_ready)

    @app.post('/welcome')
    async def save_progress(
        request: Request,
        action: Literal['move', 'open', 'pause', 'complete', 'restart'] = Form(...),
        step: Step = Form(...), destination: str = Form(''),
    ):
        target = '/welcome'
        status = 'active'
        if action == 'open':
            choice = DESTINATIONS.get(destination)
            if choice is None or choice[0] != step:
                raise HTTPException(400, 'Einrichtungsziel unbekannt. Bitte zur Tour zurückkehren.')
            target = choice[1]
        elif action == 'pause':
            status, target = 'paused', '/overview'
        elif action == 'complete':
            status, step, target = 'done', 'finish', '/overview'
        elif action == 'restart':
            step = 'welcome'
        db.execute("INSERT INTO app_preferences(name,value) VALUES('onboarding',?) "
                   "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                   (encode({'step': step, 'status': status}),))
        if request.headers.get('x-requested-with') == 'fetch':
            return JSONResponse({'url': target})
        return RedirectResponse(target, status_code=303)
