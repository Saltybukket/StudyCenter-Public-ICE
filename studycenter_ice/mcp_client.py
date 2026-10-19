"""Thin stdio-to-local-HTTP bridge. Never starts another app or buffers offline writes."""
import json
import os
import sys
from .mcp import credentials
from .config import default_data_dir


def main():
    import httpx
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    client=sys.argv[1] if len(sys.argv)>1 else 'codex'
    token=os.environ.get('STUDYCENTER_ICE_MCP_TOKEN') or credentials(data_dir=default_data_dir()).get(client)
    if not token:raise SystemExit('StudyCenter-ICE MCP-Zugang fehlt. App starten und lokale Verbindung einrichten.')
    with httpx.Client(timeout=60,headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'}) as connection:
        for line in sys.stdin:
            try:
                message=json.loads(line)
            except json.JSONDecodeError:
                print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Ungültiges JSON.'}}),flush=True)
                continue
            try:
                response=connection.post('http://127.0.0.1:18791/mcp',json=message)
                response.raise_for_status()
                if response.status_code!=202:print(response.text,flush=True)
            except Exception as error:
                if isinstance(message,dict) and 'id' in message:print(json.dumps({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32603,'message':'StudyCenter-ICE nicht erreichbar / Anfrage abgewiesen: '+type(error).__name__}}),flush=True)


if __name__=='__main__':main()
