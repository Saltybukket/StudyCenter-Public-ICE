import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pymupdf
import pytest
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings
from studycenter_ice.db import encode
from studycenter_ice.documents import DocumentStore, paper_grid, read_pdf_page, reading_id


def background(page, pitch=10, color=(.8, .9, 1)):
    for y in range(20, int(page.rect.height)-20, pitch):
        page.draw_line((0, y), (page.rect.width, y), color=color, width=.3)
    for x in range(0, int(page.rect.width), pitch):
        page.draw_line((x, 20), (x, page.rect.height-20), color=color, width=.3)


def table(page):
    for y in (220, 260, 300):
        page.draw_line((50, y), (250, y), color=(0, 0, 0))
    for x in (50, 150, 250):
        page.draw_line((x, 220), (x, 300), color=(0, 0, 0))
    for x, y, text in ((60,245,'Name'),(160,245,'Value'),(60,285,'Alpha'),(160,285,'42')):
        page.insert_text((x,y),text)


@pytest.mark.parametrize('grid', [False, True])
def test_true_table_and_mixed_grid_formula_strike_are_preserved(tmp_path, monkeypatch, grid):
    ocr = Mock(side_effect=AssertionError('Native-only page needs no OCR'))
    monkeypatch.setattr('studycenter_ice.documents.ocr_pixmap', ocr)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=500)
        if grid:
            background(page)
        table(page)
        page.insert_text((50,70),'Formula x = -2 and fraction a / b remain readable.')
        page.draw_line((50,100),(100,100))  # fraction / axis geometry
        page.insert_text((50,150),'Cancelled')
        word = next(w for w in page.get_text('words') if w[4]=='Cancelled')
        page.draw_line((word[0],(word[1]+word[3])/2),(word[2],(word[1]+word[3])/2))
        result = read_pdf_page(page,Settings(data_dir=tmp_path))
        assert result['grid_filter']['applied'] is grid
        assert '[Tabelle]\nName | Value\nAlpha | 42' in result['text']
        assert 'Formula x = -2' in result['text']
        assert '[Mögliche Durchstreichung: Cancelled;' in result['text']
        assert not ocr.called


def test_dense_regular_table_is_not_paper(tmp_path):
    with pymupdf.open() as pdf:
        page=pdf.new_page(width=200,height=200)
        background(page,pitch=10)
        # Dense native cell contents distinguish a table from empty squared paper.
        for y in range(30,170,10):
            for x in range(2,192,10):
                page.insert_text((x,y),'1',fontsize=7)
        assert not paper_grid(page,page.get_drawings(),explicit=True)[0]


def test_plain_native_page_identical_and_no_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr('studycenter_ice.documents.ocr_pixmap',Mock(side_effect=AssertionError('Unexpected OCR')))
    with pymupdf.open() as pdf:
        page=pdf.new_page()
        page.insert_text((50,50),'Ordinary native text: all original words and formulas remain intact.')
        result=read_pdf_page(page,Settings(data_dir=tmp_path))
        assert result['text']==page.get_text('text',sort=True)
        assert not result['grid_filter']['applied']


def test_reread_persists_page_scope_history_search_original_and_manual_transcription(tmp_path, monkeypatch):
    settings=Settings(data_dir=tmp_path)
    app=create_app(settings);db=app.state.db;store=DocumentStore(db,settings)
    sid=db.execute("INSERT INTO sources(kind,title,url) VALUES('manual','Fixture','https://example.org')")
    with pymupdf.open() as pdf:
        page=pdf.new_page(width=400,height=500);background(page)
        page.insert_text((50,70),'Preserved native extraction original words for this page.')
        pdf.new_page().insert_text((50,50),'Other page: this must remain completely unchanged.')
        raw=pdf.tobytes()
    doc_id=store.save(sid,'https://example.org/grid.pdf','Grid','application/pdf',raw)
    doc=store.process(doc_id);pages=json.loads(doc['pages'])
    pages[0]['visual_transcription']={'text':'[Visuell geprüft]\nManual handwritten truth','original_page':1}
    pages[0]['text']+='\n'+pages[0]['visual_transcription']['text']
    # Existing quoted reading is retained, including a prior spurious table marker.
    pages[0]['text']+='\n[Tabelle]\nOLDGRID'
    db.execute("UPDATE documents SET pages=?,analysis_state='success',progress=? WHERE id=?",(encode(pages),'{"completed":true}',doc_id))
    before=db.one('SELECT * FROM documents WHERE id=?',(doc_id,));other=pages[1]
    monkeypatch.setattr('studycenter_ice.documents.ocr_pixmap',Mock(side_effect=AssertionError('Reread reuses saved OCR')))
    client=TestClient(app)
    response=client.post(f'/documents/{doc_id}/pages/1/reread',data={'csrf':app.state.csrf,'reading_id':reading_id(pages[0]),'ignore_grid':'on'},follow_redirects=False)
    assert response.status_code==303
    after=db.one('SELECT * FROM documents WHERE id=?',(doc_id,));new=json.loads(after['pages'])[0]
    assert json.loads(after['pages'])[1]==other
    assert new['visual_transcription']==pages[0]['visual_transcription']
    assert 'Manual handwritten truth' in new['text']
    assert new['reading_history'][0]['text']==pages[0]['text']
    assert Path(after['original_path']).read_bytes()==raw
    for key in ('analysis_state','progress','published_at','fetched_at','hash'):
        assert after[key]==before[key]
    assert app.state.search.dirty.is_set() is False
    assert 'Diese Seite neu lesen' in client.get(f'/documents/{doc_id}').text
    history=client.get(f'/documents/{doc_id}?reading={new["reading_history"][0]["reading_id"]}')
    assert history.status_code==200 and 'OLDGRID' in history.text
    assert client.post(f'/documents/{doc_id}/pages/1/reread',data={'csrf':app.state.csrf,'reading_id':reading_id(pages[0]),'ignore_grid':'off'}).status_code==409
    reverted=store.reread_page(doc_id,1,False,expected_reading=reading_id(new))
    assert not reverted['grid_filter']['applied'] and '[Tabelle]' in reverted['text']


def test_actual_stored_onenote_page_filter_and_ocr_reuse(tmp_path, monkeypatch):
    original=Path(__file__).resolve().parents[1]/'.local/documents/f8881a0193af26de69c3e26a07abac6b0617a1b27bd8147c6aa9a95211cdcd8b.pdf'
    if not original.exists():
        pytest.skip('Private original case is verified locally; no personal PDF in repository fixtures.')
    digest=hashlib.sha256(original.read_bytes()).hexdigest()
    monkeypatch.setattr('studycenter_ice.documents.ocr_pixmap',Mock(side_effect=AssertionError('Saved OCR must be reused')))
    with pymupdf.open(original) as pdf:
        page=pdf[0]
        baseline=read_pdf_page(page,Settings(data_dir=tmp_path),ignore_grid=False,previous={'text':page.get_text()+'\n[OCR Bildbereich 31,402,326,616]\nTiming image verified OCR','gaps':[]})
        corrected=read_pdf_page(page,Settings(data_dir=tmp_path),ignore_grid=True,previous=baseline)
        assert baseline['text'].count('|')>10000
        assert baseline['text'].count('Mögliche Durchstreichung')==6
        assert corrected['text'].count('|')==0 and 'Mögliche Durchstreichung' not in corrected['text']
        assert 'Timing image verified OCR' in corrected['text']
        assert corrected['grid_filter']['lines']==224
        assert '[OCR ganze Seite]' not in corrected['text']
    assert hashlib.sha256(original.read_bytes()).hexdigest()==digest
