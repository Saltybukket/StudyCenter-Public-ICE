"""Hosted tunnel adapter is deliberately unavailable in this public build.

A public installation has no preassigned remote tunnel, runtime credential,
client binary, or authority to discover/control another process.
"""
import asyncio
from pathlib import Path

from .config import local_profile_path

UNSUPPORTED = 'Hosted-MCP/Tunnel ist in StudyCenter-ICE nicht eingerichtet. Lokales MCP ist verfügbar.'


def local_directory(data_dir=None):
    return local_profile_path('tunnel', data_dir=data_dir)


class TunnelService:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else local_directory()
        self.gate = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.wanted = False
        self.error = ''
        self.state = {'state': UNSUPPORTED, 'supported': False, 'connected': False,
                      'openai': False, 'mcp': False, 'managed': False, 'external': False,
                      'pid': 0, 'ui_url': '', 'last_poll': 0}

    def settings(self):
        return {'auto_start': False, 'executable': ''}

    def public(self):
        return self.state | self.settings() | {'tunnel_id': '', 'key_saved': False,
                                             'error': self.error, 'wanted': False}

    async def refresh(self):
        return dict(self.state)

    async def configure(self, executable, auto_start, key=''):
        raise ValueError(UNSUPPORTED)

    async def start(self, takeover=False, automatic=False):
        raise ValueError(UNSUPPORTED)

    async def stop(self):
        self.wanted = False
        return await self.refresh()

    async def run(self):
        await self.stop_event.wait()
