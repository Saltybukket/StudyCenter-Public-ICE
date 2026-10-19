import asyncio

import pytest

from studycenter_ice.tunnel import TunnelService


@pytest.mark.asyncio
async def test_hosted_tunnel_is_truthfully_unavailable_and_never_stores_credentials(tmp_path):
    service = TunnelService(tmp_path / 'uncreated')
    initial = service.public()
    assert initial['supported'] is False
    assert initial['connected'] is False
    assert initial['tunnel_id'] == ''
    assert initial['key_saved'] is False
    assert initial['auto_start'] is False
    for operation in (service.start(), service.start(takeover=True),
                      service.configure('nonexistent', True, 'synthetic-secret')):
        with pytest.raises(ValueError, match='nicht eingerichtet'):
            await operation
    await service.refresh()
    await service.stop()
    assert not service.directory.exists()
    assert 'synthetic-secret' not in str(service.public())


@pytest.mark.asyncio
async def test_disabled_tunnel_lifecycle_stops_without_spawning_processes(tmp_path):
    service = TunnelService(tmp_path / 'uncreated')
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0)
    assert not task.done()
    service.stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    assert not service.directory.exists()
