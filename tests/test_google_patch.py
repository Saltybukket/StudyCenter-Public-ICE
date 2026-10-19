import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from studycenter_ice.config import Settings
from studycenter_ice.db import Database
from studycenter_ice.google_calendar import GoogleCalendar, GoogleError, event_id, owned_fields


@pytest.mark.parametrize('to_all_day', [False, True])
@pytest.mark.parametrize('reactivate,hidden_times', [(False, False), (True, False), (True, True)])
async def test_precision_change_clears_old_nested_fields_and_retains_receipt(tmp_path, to_all_day, reactivate, hidden_times):
    settings = Settings(data_dir=tmp_path)
    db = Database(tmp_path / 'studycenter_ice.sqlite3')
    service = GoogleCalendar(db, settings, lambda _: ([], []))
    service.initialize()
    dataset = service.options()['dataset']
    day = {'start': {'date': '2030-10-27'}, 'end': {'date': '2030-10-28'}}
    timed = {'start': {'dateTime': '2030-10-27T23:59:00+01:00', 'timeZone': 'Europe/Vienna'},
             'end': {'dateTime': '2030-10-28T00:00:00+01:00', 'timeZone': 'Europe/Vienna'}}
    common = {'summary': 'Fixture', 'description': 'Fixture', 'transparency': 'transparent',
              'reminders': {'useDefault': False}, 'extendedProperties': {'private': {'studycenter_dataset': dataset}}}
    old = common | (timed if to_all_day else day)
    desired = copy.deepcopy(common | (day if to_all_day else timed))
    before = copy.deepcopy(desired)
    identifier = event_id(dataset, 'fixture', 'closing')
    remote = copy.deepcopy(old) | {'etag': 'old', 'colorId': '7', 'status': 'cancelled' if reactivate else 'confirmed'}
    service.receipt('owned', 'fixture', 'closing', identifier, old, 'old', 'deleted' if reactivate else 'synced')
    service.auth = SimpleNamespace(token=AsyncMock(return_value='fixture'))
    writes = []

    def merge(current, patch):
        for key, value in patch.items():
            if value is None:
                current.pop(key, None)
            elif isinstance(value, dict):
                merge(current.setdefault(key, {}), value)
            else:
                current[key] = value

    def transport(request):
        if request.method == 'GET':
            if hidden_times and remote['status'] == 'cancelled':
                return httpx.Response(200, json={'id': identifier, 'status': 'cancelled', 'etag': remote['etag']})
            return httpx.Response(200, json=remote)
        assert request.method == 'PATCH' and request.headers['If-Match'] == 'old'
        patched = copy.deepcopy(remote)
        merge(patched, json.loads(request.content))
        if any(bool(patched[k].get('date')) == bool(patched[k].get('dateTime')) for k in ('start', 'end')):
            return httpx.Response(400, json={'error': {'message': 'Invalid start time.', 'errors': [{'reason': 'invalid'}]}})
        remote.clear(); remote.update(patched | {'etag': 'new'})
        writes.append(request.method)
        return httpx.Response(200, json=remote)

    entry = {'identity': 'fixture', 'role': 'closing', 'event_id': identifier, 'payload': desired}
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service.http = client
        await service.publish('owned', db.one('SELECT * FROM google_events'), entry)
        await service.publish('owned', db.one('SELECT * FROM google_events'), entry)
    assert writes == ['PATCH']
    assert owned_fields(remote) == owned_fields(desired)
    assert remote['colorId'] == '7' and remote['status'] == 'confirmed'
    receipt = db.one('SELECT * FROM google_events')
    assert receipt['state'] == 'synced' and receipt['event_id'] == identifier
    assert json.loads(receipt['payload']) == before == desired


async def test_google_error_reports_safe_reason_without_returning_private_body():
    service = GoogleCalendar(None, SimpleNamespace(data_dir='.'), None)
    service.auth = SimpleNamespace(token=AsyncMock(return_value='fixture'))
    def transport(request):
        return httpx.Response(400, json={'error': {'message': 'Private event name person@example.org',
            'errors': [{'reason': 'invalid'}], 'private_payload': 'do not expose'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service.http = client
        with pytest.raises(GoogleError, match='invalid') as caught:
            await service.request('PATCH', '/fixture', json={})
    assert 'Private' not in str(caught.value) and 'example.org' not in str(caught.value)
