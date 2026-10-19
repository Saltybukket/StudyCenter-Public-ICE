import asyncio
import argparse
import os
import sys
import logging
from logging.handlers import RotatingFileHandler

import uvicorn

from .app import create_app
from .runtime import wait_for_process_exit,windows_shutdown_listener


class OwnedServer(uvicorn.Server):
    def handle_exit(self,sig,frame):
        logging.getLogger('studycenter_ice.lifecycle').warning('Server-Beendigungssignal %s empfangen',sig)
        super().handle_exit(sig,frame)

    async def shutdown(self,sockets=None):
        # Uvicorn closes HTTP before lifespan shutdown. Stop workers before drain.
        logging.getLogger('studycenter_ice.lifecycle').warning('Shutdown beginnt: %s HTTP-Anfragen',len(self.server_state.tasks))
        self.config.app.state.begin_stop(system=True)
        await super().shutdown(sockets)
        logging.getLogger('studycenter_ice.lifecycle').warning('Shutdown abgeschlossen')


def main(argv=None):
    # pythonw at Windows login has no streams; Uvicorn's logging expects them.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    parser=argparse.ArgumentParser(prog='StudyCenter-ICE')
    parser.add_argument('--after-process',type=int,metavar='PID')
    parser.add_argument('--headless',action='store_true',help='Backend only, without tray or window (default).')
    arguments=parser.parse_args(argv)
    if arguments.after_process is not None:
        if arguments.after_process<=0:parser.error('--after-process benötigt eine positive Prozess-ID.')
        try:wait_for_process_exit(arguments.after_process)
        except (OSError,ValueError) as error:parser.exit(1,f'StudyCenter-ICE-Neustart abgebrochen: {error}\n')
    # Playwright's driver subprocess needs the Proactor loop on native Windows.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    app=create_app()
    directory=app.state.settings.data_dir/'logs';directory.mkdir(exist_ok=True)
    handler=RotatingFileHandler(directory/'backend.log',maxBytes=256*1024,backupCount=2,encoding='utf8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    for name in ('studycenter_ice.lifecycle','uvicorn.error'):
        logging.getLogger(name).addHandler(handler)
    logging.getLogger('studycenter_ice.lifecycle').warning('ICE-Backend gestartet; PID=%s',os.getpid())
    server=OwnedServer(uvicorn.Config(app,host="127.0.0.1",port=18791,workers=1,reload=False,loop="asyncio",access_log=False,timeout_graceful_shutdown=5))
    app.state.stop_callback=lambda:setattr(server,'should_exit',True)
    def system_stop():
        callback=getattr(app.state,'system_shutdown',app.state.stop_callback)
        callback()
    with windows_shutdown_listener(system_stop) as ready:
        app.state.windows_shutdown_ready=ready
        try:server.run()
        finally:
            for name in ('studycenter_ice.lifecycle','uvicorn.error'):logging.getLogger(name).removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    main()
