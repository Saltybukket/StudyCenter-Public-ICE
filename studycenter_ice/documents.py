import hashlib
import json
import re
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from statistics import median

import pymupdf
from bs4 import BeautifulSoup

from .db import encode, now


def html_content(raw: bytes, url: str):
    soup = BeautifulSoup(raw, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    published = None
    for selector in ('meta[property="article:published_time"]', 'meta[name="date"]', 'time[datetime]'):
        node = soup.select_one(selector)
        if node:
            published = node.get("content") or node.get("datetime")
            break
    main = content_root(soup)
    for node in main.select("script,style,nav,footer,.site-header,form,input,button,.usermenu,.logininfo"):
        node.decompose()
    for node in main.select("s,del,strike,[style*='line-through']"):
        node.replace_with(" [DURCHGESTRICHEN: " + node.get_text(" ", strip=True) + "] ")
    # Preserve table rows and headers instead of flattening cell boundaries.
    for row in main.select("tr"):
        row.replace_with("\n" + " | ".join(cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)) + "\n")
    for heading in main.find_all(re.compile("^h[1-6]$")):
        heading.insert_before("\n" + "#" * int(heading.name[1]) + " ")
        heading.insert_after("\n")
    text = main.get_text("\n", strip=True)
    return title, published, [{"page": 1, "anchor": "html", "text": text, "gaps": []}]


def content_root(soup):
    # Some CMS themes place multiple content sections beside a short <main> title block.
    return soup.select_one('#region-main') or soup.select_one('#main') or soup.find('main') or soup.find('article') or soup.body or soup


def calendar_content(raw):
    # RFC 5545 line folding is transport syntax, not part of source field values.
    lines = []
    for line in raw.decode('utf-8-sig').replace('\r', '').split('\n'):
        if not line:
            continue
        if line.startswith((' ', '\t')) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    pages, event = [], None
    for line in lines:
        if line == 'BEGIN:VEVENT':
            event = [line]
        elif event is not None:
            event.append(line)
            if line == 'END:VEVENT':
                uid = next((x[4:] for x in event if x.startswith('UID:')), None)
                if not uid:
                    raise ValueError('Kalendertermin ohne UID')
                pages.append({'page': len(pages)+1, 'anchor': uid, 'text': '\n'.join(event), 'gaps': []})
                event = None
    if event is not None or not pages:
        raise ValueError('Kalenderexport unvollständig oder ohne Termine')
    return pages


def ocr_pixmap(pixmap, settings):
    if not settings.tesseract or not Path(settings.tesseract).exists():
        return None, "OCR nötig; Tesseract fehlt (TESSERACT_CMD)."
    with tempfile.TemporaryDirectory() as directory:
        png = Path(directory) / "page.png"
        pixmap.save(png)
        try:
            result = subprocess.run([settings.tesseract, str(png), "stdout", "-l", settings.ocr_lang], capture_output=True, timeout=120, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                return None, f"OCR fehlgeschlagen; Sprachdaten {settings.ocr_lang} prüfen."
            if not result.stdout.strip():
                return None, "OCR hat keinen lesbaren Text gefunden; Seitenansicht prüfen."
            return result.stdout.strip(), None
        except (OSError, subprocess.TimeoutExpired):
            return None, "OCR konnte nicht ausgeführt werden."


def paper_grid(page, drawings, *, explicit=False):
    """Identify background paths, never erase page content or table/ink paths.

    A grid needs two dense regular families spanning most of the page, with
    mostly empty tiny cells. Regular, populated tables alone do not qualify.
    Automatic use additionally requires a pale stroke (as in OneNote exports).
    """
    families = defaultdict(lambda: [[], []])
    for index, drawing in enumerate(drawings):
        if drawing['type'] != 's' or len(drawing['items']) != 1:
            continue
        entry = drawing['items'][0]
        if entry[0] != 'l':
            continue
        a, b = entry[1:3]
        color = drawing.get('color') or (0, 0, 0)
        if not explicit and min(color) < .7:
            continue
        style = (tuple(round(c, 3) for c in color), round(drawing.get('width') or 0, 2), drawing.get('dashes'))
        if abs(a.y-b.y) < .2 and abs(a.x-b.x) >= page.rect.width*.75:
            families[style][0].append((a.y, index))
        elif abs(a.x-b.x) < .2 and abs(a.y-b.y) >= page.rect.height*.75:
            families[style][1].append((a.x, index))
    words = page.get_text('words')
    for horizontal, vertical in families.values():
        if min(len(horizontal), len(vertical)) < 16:
            continue
        axes, steps = [], []
        for lines, dimension in ((horizontal, page.rect.height), (vertical, page.rect.width)):
            positions = sorted(set(round(position, 2) for position, _ in lines))
            distances = [b-a for a, b in zip(positions, positions[1:])]
            step = median(distances)
            if step <= 0 or positions[-1]-positions[0] < dimension*.65:
                break
            if sum(abs(d-step) <= step*.08 for d in distances) < len(distances)*.95:
                break
            axes.append(positions)
            steps.append(step)
        if len(axes) != 2 or max(steps)/min(steps) > 1.25:
            continue
        # Text crosses many paper cells; a real table normally populates cells.
        cells = (len(axes[0])-1)*(len(axes[1])-1)
        if words and (len(words)/cells > .12 or median(w[3]-w[1] for w in words) < max(steps)*.7):
            continue
        indices = {i for _, i in horizontal+vertical}
        return indices, {'lines': len(indices), 'spacing': [round(s, 2) for s in steps],
                         'reason': 'Dichtes, gleichmäßiges Raster über fast die ganze Seite; überwiegend leere Zellen. Nur diese Hintergrundlinien werden bei Tabellen und Durchstreichungen ausgelassen.'}
    return set(), None


def cached_ocr(page):
    if 'ocr_text' in page:
        return page['ocr_text'], page.get('ocr_gaps', [])
    # Older stored pages have OCR before table/drawing notices. Keep this output
    # during a local geometry correction rather than running OCR again.
    text = page['text']
    visual = page.get('visual_transcription', {}).get('text')
    if visual and text.endswith(visual):
        text = text[:-len(visual)]
    match = re.search(r'\n\[OCR (?:ganze Seite|Bildbereich)', text)
    ocr = re.split(r'\n\[(?:Tabelle|DURCHGESTRICHEN|Mögliche Durchstreichung)', text[match.start():], maxsplit=1)[0] if match else ''
    return ocr, [g for g in page.get('gaps', []) if 'OCR' in g or 'Bildbereich' in g]


def read_pdf_page(page, settings, *, ignore_grid=None, previous=None):
    text = page.get_text('text', sort=True)
    ocr_text, gaps = '', []
    if previous is not None:
        ocr_text, gaps = cached_ocr(previous)
    elif len(text.strip()) < 30:
        ocr, gap = ocr_pixmap(page.get_pixmap(dpi=180), settings)
        if ocr:
            ocr_text = '\n[OCR ganze Seite]\n'+ocr
        if gap:
            gaps.append(gap)
    else:
        seen = set()
        for info in page.get_image_info():
            rect = pymupdf.Rect(info['bbox']) & page.rect
            if rect.width < 60 or rect.height < 25 or tuple(rect) in seen:
                continue
            seen.add(tuple(rect))
            ocr, gap = ocr_pixmap(page.get_pixmap(dpi=180, clip=rect), settings)
            location = ','.join(str(round(x)) for x in rect)
            if ocr:
                ocr_text += f'\n[OCR Bildbereich {location}]\n{ocr}'
            if gap:
                gaps.append(f'Bildbereich {location}: {gap}')
    text += ocr_text
    drawings = page.get_drawings()
    excluded, grid = paper_grid(page, drawings, explicit=ignore_grid is True)
    if ignore_grid is False:
        excluded = set()
    paths = [drawing for i, drawing in enumerate(drawings) if i not in excluded]
    for table in page.find_tables(paths=paths).tables:
        text += '\n[Tabelle]\n'+'\n'.join(' | '.join(str(cell or '') for cell in row) for row in table.extract())
    for annotation in page.annots() or []:
        if annotation.type[0] == pymupdf.PDF_ANNOT_STRIKE_OUT:
            text += '\n[DURCHGESTRICHEN: '+page.get_textbox(annotation.rect)+']'
    words = page.get_text('words')
    for drawing in paths:
        for entry in drawing['items']:
            if entry[0] == 'l':
                start, end = entry[1:3]
                if abs(start.y-end.y) < 2 and abs(start.x-end.x) > 10:
                    strike = [word for x0,y0,x1,y1,word,*_ in words
                              if min(start.x,end.x) <= (x0+x1)/2 <= max(start.x,end.x)
                              and y0+(y1-y0)*.25 < start.y < y0+(y1-y0)*.75]
                    if strike:
                        text += '\n[Mögliche Durchstreichung: '+' '.join(strike)+'; Seitenansicht beachten]'
    if not text.strip():
        gaps.append('Kein lesbarer Seiteninhalt; Seitenansicht prüfen.')
    result = {'page': page.number+1, 'anchor': f'page-{page.number+1}', 'text': text, 'gaps': gaps,
              'ocr_text': ocr_text, 'ocr_gaps': list(gaps),
              'reading_options': {'ignore_grid': ignore_grid},
              'grid_filter': {**(grid or {}), 'applied': bool(excluded)}}
    if previous and previous.get('visual_transcription'):
        result['visual_transcription'] = previous['visual_transcription']
        result['text'] += '\n'+previous['visual_transcription']['text']
    return result


def pdf_content(path: Path, settings, *, page_options=None):
    pages = []
    with pymupdf.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError("PDF ist passwortgeschützt")
        for number, page in enumerate(pdf, 1):
            pages.append(read_pdf_page(page, settings, ignore_grid=(page_options or {}).get(number)))
    return pages


class DocumentStore:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def reread_page(self, doc_id, number, ignore_grid, *, expected_reading):
        document = self.db.one('SELECT * FROM documents WHERE id=?', (doc_id,))
        if not document or document['mime'] != 'application/pdf':
            raise ValueError('PDF-Dokument nicht vorhanden.')
        pages = json.loads(document['pages'])
        previous = next((p for p in pages if p['page'] == number), None)
        if not previous:
            raise ValueError('Gespeicherte Seite nicht vorhanden.')
        if reading_id(previous) != expected_reading:
            raise ValueError('Diese Lesefassung wurde inzwischen geändert. Seite erneut öffnen.')
        with pymupdf.open(document['original_path']) as pdf:
            if not 1 <= number <= len(pdf):
                raise ValueError('PDF-Seite nicht vorhanden.')
            updated = read_pdf_page(pdf[number-1], self.settings, ignore_grid=ignore_grid, previous=previous)
        history = list(previous.get('reading_history', []))
        identifier = reading_id(previous)
        if not any(p['reading_id'] == identifier for p in history):
            history.append({k:v for k,v in previous.items() if k != 'reading_history'} |
                           {'reading_id': identifier, 'saved_at': now()})
        updated['reading_history'] = history
        updated['reread_at'] = now()
        pages[pages.index(previous)] = updated
        # Optimistic check inside the write transaction prevents stale local
        # rereads from overwriting new transcriptions or another page correction.
        with self.db.connection() as con:
            changed = con.execute('UPDATE documents SET pages=? WHERE id=? AND pages=?',
                                  (encode(pages), doc_id, document['pages']))
            if changed.rowcount != 1:
                raise ValueError('Dokument inzwischen geändert. Seite erneut öffnen.')
        return updated

    def save(self, source_id, url, title, mime, raw, published=None, *, revision='', repository_path='', source_url='', mail_metadata=None):
        digest = hashlib.sha256(raw).hexdigest()
        old = self.db.one("SELECT * FROM documents WHERE source_id=? AND url=? AND hash=?", (source_id, url, digest))
        if old:
            latest = self.db.one('SELECT id FROM documents WHERE source_id=? AND url=? ORDER BY fetched_at DESC,id DESC LIMIT 1', (source_id,url))
            self.db.execute("UPDATE documents SET fetched_at=? WHERE id=?", (now(), old["id"]))
            if latest and latest['id'] != old['id'] and not mail_metadata:
                self.db.execute("UPDATE documents SET analysis_state='pending' WHERE id=?", (old['id'],))
            return old["id"]
        extension = {"application/pdf": ".pdf", "text/html": ".html", "message/rfc822": ".eml",
                     "image/png": ".png", "image/jpeg": ".jpg"}.get(mime, '.txt')
        path = self.settings.data_dir / "documents" / (digest + extension)
        path.write_bytes(raw)
        return self.db.execute("INSERT INTO documents(source_id,url,title,mime,hash,published_at,fetched_at,original_path,revision,repository_path,source_url,mail_metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (source_id, url, title, mime, digest, published, now(), str(path), revision, repository_path, source_url, encode(mail_metadata or {})))

    def process(self, doc_id):
        document = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        if document["processing_state"] == "success":
            return document
        try:
            path = Path(document["original_path"])
            if document["mime"] == "application/pdf":
                old_pages = json.loads(document['pages'])
                options = {p['page']: p.get('reading_options', {}).get('ignore_grid') for p in old_pages}
                pages = pdf_content(path, self.settings, page_options=options)
                for page in pages:
                    previous = next((p for p in old_pages if p['page'] == page['page']), {})
                    for key in ('reading_history', 'reread_at'):
                        if key in previous:
                            page[key] = previous[key]
                title, published = document["title"], document["published_at"]
                if title == 'Kursseite':
                    title = self.db.source(document['source_id'])['title']
            elif document["mime"] == "text/html":
                title, published, pages = html_content(path.read_bytes(), document["url"])
                if document.get("mail_metadata", "{}") != "{}":
                    title, published = document["title"], document["published_at"]
            elif document['mime'] == 'text/calendar':
                title, published = document['title'], document['published_at']
                pages = calendar_content(path.read_bytes())
            elif document['mime'] == 'message/rfc822':
                from .mail import message_pages
                title, published = document['title'], document['published_at']
                pages = message_pages(path.read_bytes())
            elif document['mime'] in ('image/png', 'image/jpeg'):
                title, published = document['title'], document['published_at']
                text, gap = ocr_pixmap(pymupdf.Pixmap(str(path)), self.settings)
                pages = [{'page': 1, 'anchor': 'image', 'text': text or '', 'gaps': [gap] if gap else []}]
            else:
                title, published = document["title"], document["published_at"]
                charset = json.loads(document.get('mail_metadata', '{}')).get('charset', 'utf-8')
                pages = [{"page": 1, "anchor": "text", "text": path.read_text(encoding=charset), "gaps": []}]
            # A verified visual transcription belongs to these unchanged original bytes.
            # Repeated OCR must retain it, while fresh document versions start without it.
            verified = {p['page']: p['visual_transcription'] for p in json.loads(document['pages'])
                        if isinstance(p.get('visual_transcription'), dict) and p['visual_transcription'].get('text')}
            for page in pages:
                if page['page'] in verified:
                    page['visual_transcription'] = verified[page['page']]
                    page['text'] += '\n' + verified[page['page']]['text']
            gaps = [f"Seite {p['page']}: {g}" for p in pages for g in p["gaps"]]
            # A changed OCR result invalidates previously completed chunks.
            if encode(pages) != document["pages"]:
                self.db.execute("UPDATE documents SET progress='{}',analysis_state='pending' WHERE id=?", (doc_id,))
                if document['mime'] == 'text/html' and not gaps:
                    # Moodle changes runtime element IDs on every render. Keep each original
                    # version, but replay validated results when all extracted content is identical.
                    previous = self.db.one("""SELECT progress FROM documents WHERE source_id=? AND url=?
                        AND id!=? AND pages=? AND title=? AND published_at IS ?
                        AND processing_state='success' AND progress!='{}'
                        ORDER BY fetched_at DESC,id DESC LIMIT 1""",
                        (document['source_id'],document['url'],doc_id,encode(pages),title,published))
                    if previous:
                        self.db.execute('UPDATE documents SET progress=? WHERE id=?', (previous['progress'],doc_id))
            self.db.execute("UPDATE documents SET pages=?,title=?,published_at=?,processing_state=?,error=? WHERE id=?", (encode(pages), title, published, "partial" if gaps else "success", "\n".join(gaps), doc_id))
        except Exception as error:
            self.db.execute("UPDATE documents SET processing_state='failed',error=? WHERE id=?", (f"Dokument nicht lesbar: {type(error).__name__}: {error}", doc_id))
        return self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))


def reading_id(page):
    return hashlib.sha256(encode({k:page.get(k) for k in ('text','gaps','visual_transcription','reading_options')}).encode()).hexdigest()


def chunks(pages, size=10000, overlap=1000):
    # Every page, including the end of a long page, is covered. A shared page gives context at boundaries.
    segments = []
    for page in pages:
        text = page["text"]
        for start in range(0, max(len(text), 1), size - overlap):
            segments.append({**page, "anchor": f"{page['anchor']}:{start}", "text": text[start:start+size]})
            if start + size >= len(text):
                break
    result, current, length = [], [], 0
    for segment in segments:
        if current and length + len(segment["text"]) > size:
            result.append(current)
            previous = current[-1]
            current = [{**previous, "text": previous["text"][-overlap:]}]
            length = len(current[0]["text"])
        current.append(segment)
        length += len(segment["text"])
    if current:
        result.append(current)
    return result
