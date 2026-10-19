import io
import json
import sys

import httpx

from studycenter_ice import mcp_client


class Stream(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


def test_malformed_stdio_line_cannot_reuse_previous_request_id(monkeypatch):
    incoming = Stream('\n'.join([
        '{broken',
        json.dumps({'jsonrpc': '2.0', 'id': 41, 'method': 'tools/list'}),
        '{broken again',
        json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
        json.dumps({'jsonrpc': '2.0', 'id': 42, 'method': 'tools/list'}),
    ]))
    outgoing = Stream()
    requests = []

    def respond(request):
        message = json.loads(request.content)
        requests.append(message)
        if 'id' not in message:
            return httpx.Response(202)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': {}})

    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(mcp_client, 'credentials', lambda **kwargs: {'codex': 'fixture-token'})
    monkeypatch.setattr(sys, 'argv', ['studycenter_ice.mcp_client'])
    monkeypatch.setattr(sys, 'stdin', incoming)
    monkeypatch.setattr(sys, 'stdout', outgoing)
    mcp_client.main()

    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [row['id'] for row in responses] == [None, 41, None, 42]
    assert [row['error']['code'] for row in responses if 'error' in row] == [-32700, -32700]
    assert len(requests) == 3
