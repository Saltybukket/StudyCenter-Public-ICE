"""Real rendered setup flow with synthetic data; no component downloads or accounts."""
import asyncio
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings


@pytest.mark.parametrize('width,theme', [(1366, 'light'), (390, 'dark')])
async def test_visual_setup_real_forms_resume_error_and_finish(tmp_path, width, theme):
    app = create_app(Settings(data_dir=tmp_path/'data'))
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128); sock.setblocking(False)
    base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='warning'))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    errors = []; external = []; failure_posts = []
    try:
        while not server.started:
            if serving.done(): await serving
            await asyncio.sleep(.02)
        async with async_playwright() as driver:
            executable = os.environ.get('STUDYCENTER_ICE_TEST_CHROMIUM', driver.chromium.executable_path)
            if not Path(executable).exists(): pytest.skip('Project Chromium not installed')
            browser = await driver.chromium.launch(executable_path=executable, headless=True, chromium_sandbox=True)
            context = await browser.new_context(viewport={'width': width, 'height': 940}, locale='de-AT')
            await context.add_init_script("localStorage.setItem('studycenter_ice.theme', '" + theme + "')")
            page = await context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
            async def local_only(route):
                if urlsplit(route.request.url).netloc != urlsplit(base).netloc:
                    external.append(route.request.url); await route.abort()
                else: await route.continue_()
            await page.route('**/*', local_only)
            async def shot(name):
                directory = os.environ.get('STUDYCENTER_ICE_TEST_SCREENSHOTS')
                if directory:
                    folder = Path(directory); folder.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(folder/f'{name}-{width}-{theme}.png'), full_page=True)
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            await page.goto(base+'/overview')
            await expect(page).to_have_url(base+'/welcome')
            await expect(page.locator('#welcome-heading')).to_contain_text('Dein Studium.')
            await expect(page.locator('html')).to_have_attribute('data-theme', theme)
            await shot('welcome')
            # Keyboard activates the same real form as a pointer.
            await page.get_by_role('button', name='Los geht’s →', exact=True).focus()
            await page.keyboard.press('Enter')
            await expect(page.locator('#welcome-heading')).to_contain_text('Ein erster Kurs.')
            await page.get_by_role('button', name='Kurs anlegen', exact=False).click()
            await expect(page.locator('#course-create')).to_have_attribute('open', '')
            await page.locator('#course-create input[name="title"]').fill('Synthetischer Testkurs')
            await page.locator('#course-create input[name="semester"]').fill('2030W')
            await page.locator('#course-create').get_by_role('button', name='Kurs anlegen', exact=True).click()
            await page.get_by_role('link', name='Einrichtung fortsetzen →').click()
            await expect(page.locator('.welcome-status')).to_contain_text('1 Kurs · 0 Quellen')
            await shot('courses')
            await page.get_by_role('button', name='Weiter →', exact=True).click()
            await page.get_by_role('button', name='KI-Verbindung öffnen', exact=False).click()
            await expect(page).to_have_url(base+'/model')
            await page.get_by_role('link', name='Einrichtung fortsetzen →').click()
            await expect(page.locator('#welcome-heading')).to_contain_text('KI, wenn sie')
            await page.get_by_role('button', name='Weiter →', exact=True).click()
            await page.get_by_role('button', name='Darstellung wählen', exact=False).click()
            await expect(page.locator('[data-settings-pane="display"]')).to_be_visible()
            await page.get_by_role('link', name='Einrichtung fortsetzen →').click()
            await page.get_by_role('button', name='Weiter →', exact=True).click()
            await expect(page.locator('#welcome-heading')).to_contain_text('Klein anfangen.')
            await shot('components')
            if width < 850:
                current = page.locator('.welcome-steps [aria-current="step"]')
                bounds = await current.bounding_box()
                assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
            await page.get_by_role('button', name='Weiter →', exact=True).click()
            await expect(page.locator('#welcome-heading')).to_contain_text('Gut verbunden.')
            await page.get_by_role('button', name='Weiter →', exact=True).click()
            await expect(page.locator('.welcome-summary')).to_contain_text('Später möglich')
            await shot('finish')
            async def fail_once(route):
                if route.request.method == 'POST':
                    failure_posts.append(route.request.url)
                    await route.fulfill(status=503, json={'detail':'Speichern vorübergehend nicht möglich'})
                else: await route.fallback()
            await page.route('**/welcome', fail_once)
            await page.get_by_role('button', name='Tour abschließen & ICE öffnen →').click()
            await expect(page.locator('#welcome-error')).to_contain_text('Fortschritt nicht bestätigt')
            await expect(page.get_by_role('button', name='Tour abschließen & ICE öffnen →')).to_be_enabled()
            assert len(failure_posts) == 1
            await page.unroute('**/welcome', fail_once)
            await page.get_by_role('button', name='Tour abschließen & ICE öffnen →').click()
            await expect(page).to_have_url(base+'/overview')
            await page.reload(); await expect(page).to_have_url(base+'/overview')
            await page.get_by_role('link', name='Einrichtung', exact=True).click()
            await page.get_by_role('button', name='Tour von vorn ansehen').click()
            await expect(page.locator('#welcome-heading')).to_contain_text('Dein Studium.')
            await page.get_by_role('button', name='Später fortsetzen').click()
            await expect(page).to_have_url(base+'/overview')
            await page.reload(); await expect(page).to_have_url(base+'/overview')
            assert not errors and not external
            assert app.state.db.one('SELECT COUNT(*) n FROM courses')['n'] == 1
            assert app.state.db.rows('SELECT * FROM sources') == []
            assert app.state.db.rows('SELECT * FROM items') == []
            assert not (tmp_path/'data/components').exists()
            await context.close(); await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 15)
