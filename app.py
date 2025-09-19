奇幻小说
情色小说
情色文学
文学评论
武侠小说
热门小说
穿越小说
网站分类
言情小说
通俗小说
都市小说
黑暗小说
青春纯爱
网站分类
站内全文搜索



from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import httpx
from bs4 import BeautifulSoup
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
import os
import json
import re
import random
import logging
import unicodedata


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI(title="QNote Auto Import Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Chapter(BaseModel):
    title: str
    content: str
    source: str


class BookResult(BaseModel):
    id: str
    title: str
    description: str
    category: str
    source_book: str
    chapters: List[Chapter]


class CrawlRequest(BaseModel):
    num_books: int = 5
    num_chapters: int = 10


async def fetch_text(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, timeout=20.0, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.text
    except Exception as exc:
        logger.debug("fetch_text error for %s: %s", url, exc)
        return None
    return None


def clean_html(html: str) -> str:
    """Remove images and convert anchors to text, return cleaned HTML string."""
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all('img'):
        img.decompose()
    for a in soup.find_all('a'):
        a.replace_with(a.get_text())
    return str(soup)


def html_to_text(html: str) -> str:
    """Convert HTML to a readable plain-text string."""
    soup = BeautifulSoup(html or '', 'html.parser')
    text = soup.get_text(separator='\n')
    # Normalize whitespace
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return '\n\n'.join(lines)


def sanitize_filename(name: str, max_len: int = 200) -> str:
    # Normalize unicode, remove path separators and disallowed characters
    name = unicodedata.normalize('NFKD', name)
    name = name.replace('/', ' ').replace('\\', ' ')
    # remove characters not allowed in Windows filenames
    name = re.sub(r'[<>:"|?*]', '', name)
    name = name.strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or 'untitled'


async def crawl_books(req: CrawlRequest) -> List[BookResult]:
    homepage = 'https://qnote.qq.com/'
    async with httpx.AsyncClient() as client:
        body = await fetch_text(client, homepage)
        if not body:
            raise HTTPException(status_code=502, detail='Failed to fetch QNote homepage')

        soup = BeautifulSoup(body, 'html.parser')
        links = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/detail/' in href:
                if href.startswith('http'):
                    links.add(href)
                else:
                    try:
                        links.add(str(httpx.URL(homepage).join(href)))
                    except Exception:
                        links.add(href)

        # extract book ids
        book_ids = []
        for href in links:
            m = re.search(r'/detail/(\d+)', str(href))
            if m:
                book_ids.append(m.group(1))

        book_ids = list(dict.fromkeys(book_ids))
        random.shuffle(book_ids)
        book_ids = book_ids[: req.num_books]

        results: List[BookResult] = []

        for book_id in book_ids:
            detail_url = f'https://qnote.qq.com/detail/{book_id}'
            detail_body = await fetch_text(client, detail_url)
            if not detail_body:
                continue

            # check first chapter existence
            first_ch_url = f'https://qnote.qq.com/read/{book_id}/1'
            first_ch_body = await fetch_text(client, first_ch_url)
            source_book = first_ch_url if first_ch_body else detail_url

            dsoup = BeautifulSoup(detail_body, 'html.parser')
            title_tag = dsoup.find(['h1', 'h2'])
            title = title_tag.get_text().strip() if title_tag else f'Book {book_id}'

            desc_nodes = dsoup.select('.intro, .detail_intro')
            if desc_nodes:
                desc_html = ''.join(str(n) for n in desc_nodes)
                description = clean_html(desc_html)
            else:
                description = '<p>Chưa có mô tả</p>'

            breadcrumb = dsoup.select_one('.breadcrumb a:nth-of-type(2)')
            category = breadcrumb.get_text().strip() if breadcrumb else 'Unknown'

            chapters = []
            for i in range(1, req.num_chapters + 1):
                ch_url = f'https://qnote.qq.com/read/{book_id}/{i}'
                ch_body = await fetch_text(client, ch_url)
                if not ch_body:
                    break
                csoup = BeautifulSoup(ch_body, 'html.parser')
                ch_title_tag = csoup.find('h1')
                ch_title = ch_title_tag.get_text().strip() if ch_title_tag else f'Chương {i}'

                # Try multiple selectors commonly used for chapter content
                selectors = ['.content', '.chapter', '.read-content', '#content', '.article']
                ch_html = None
                for sel in selectors:
                    nodes = csoup.select(sel)
                    if nodes:
                        ch_html = ''.join(str(n) for n in nodes)
                        logger.debug("[%s] chapter %s: extracted using selector '%s'", book_id, i, sel)
                        break

                # fallback: collect paragraph tags inside body
                if not ch_html:
                    p_nodes = csoup.select('body p') or csoup.find_all('p')
                    if p_nodes:
                        ch_html = ''.join(str(p) for p in p_nodes)
                        logger.debug("[%s] chapter %s: fallback to <p> paragraphs (count=%s)", book_id, i, len(p_nodes))

                # last-resort: take largest text block
                if not ch_html:
                    texts = [t.strip() for t in csoup.get_text(separator='\n').split('\n') if t.strip()]
                    if texts:
                        ch_html = '<p>' + '</p><p>'.join(texts[:50]) + '</p>'
                        logger.debug("[%s] chapter %s: fallback to plain text (len=%s)", book_id, i, len(texts))

                if ch_html:
                    ch_content = clean_html(ch_html)
                else:
                    ch_content = '<p>Chưa có nội dung</p>'

                chapters.append(Chapter(title=ch_title, content=ch_content, source=ch_url))

            results.append(BookResult(id=book_id, title=title, description=description, category=category, source_book=source_book, chapters=chapters))

        return results


@app.post('/api/crawl', response_model=List[BookResult])
async def api_crawl(req: CrawlRequest):
    return await crawl_books(req)


@app.get('/', tags=['root'])
async def root():
    return {"status": "ok", "service": "QNote Auto Import Backend"}


@app.get('/api/crawl', response_model=List[BookResult])
async def api_crawl_get(num_books: int = 2, num_chapters: int = 5):
    """Convenience GET endpoint for quick testing in a browser."""
    req = CrawlRequest(num_books=num_books, num_chapters=num_chapters)
    return await crawl_books(req)


@app.post('/api/crawl_and_save')
async def api_crawl_and_save(req: CrawlRequest):
    results = await crawl_books(req)
    # prepare output dir
    out_dir = os.path.join(os.getcwd(), 'output')
    os.makedirs(out_dir, exist_ok=True)
    fname = f"qnote_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    fpath = os.path.join(out_dir, fname)
    # serialize with ensure_ascii=False so Unicode preserved
    with open(fpath, 'w', encoding='utf-8') as fh:
        json.dump([r.model_dump() for r in results], fh, ensure_ascii=False, indent=2)
    return {"saved": fpath, "count": len(results)}


@app.post('/api/crawl_and_export')
async def api_crawl_and_export(req: CrawlRequest):
    """Crawl and export each book into its own folder under `output/`.

    Folder layout:
      output/<id>_<sanitized-title>/
        description.txt     # contains title, category, source, and description
        chapter_001 - title.txt
        chapter_002 - title.txt
        meta.json           # full BookResult as JSON
    """
    results = await crawl_books(req)
    out_root = os.path.join(os.getcwd(), 'output')
    os.makedirs(out_root, exist_ok=True)
    saved = []

    for book in results:
        sanitized = sanitize_filename(book.title)
        dir_name = f"{book.id}_{sanitized}"
        book_dir = os.path.join(out_root, dir_name)
        os.makedirs(book_dir, exist_ok=True)

        # write description.txt (include category and source)
        desc_path = os.path.join(book_dir, 'description.txt')
        with open(desc_path, 'w', encoding='utf-8') as fh:
            fh.write(f"Title: {book.title}\n")
            fh.write(f"Category: {book.category}\n")
            fh.write(f"Source: {book.source_book}\n\n")
            fh.write(html_to_text(book.description))

        # write per-chapter text files
        for idx, ch in enumerate(book.chapters, start=1):
            num = str(idx).zfill(3)
            ch_title_safe = sanitize_filename(ch.title)
            fname = f"{num} - {ch_title_safe}.txt"
            fpath = os.path.join(book_dir, fname)
            with open(fpath, 'w', encoding='utf-8') as fh:
                fh.write(f"{ch.title}\n\n")
                fh.write(html_to_text(ch.content))

        # save meta.json for machine consumption
        meta_path = os.path.join(book_dir, 'meta.json')
        with open(meta_path, 'w', encoding='utf-8') as fh:
            json.dump(book.model_dump(), fh, ensure_ascii=False, indent=2)

        saved.append(book_dir)

    return {"exported": saved, "count": len(saved)}


if __name__ == '__main__':
    # Interactive runner: prompt user for numbers and run the crawler
    try:
        print("Chạy QNote crawler (chạy trực tiếp, không phải server)")
        n_books = input('Số lượng truyện (mặc định 2): ').strip() or '2'
        n_chapters = input('Số lượng chương trên mỗi truyện (mặc định 5): ').strip() or '5'
        try:
            n_books_i = int(n_books)
            n_ch_i = int(n_chapters)
        except ValueError:
            print('Vui lòng nhập số nguyên hợp lệ. Dùng giá trị mặc định 2,5')
            n_books_i = 2
            n_ch_i = 5

        req = CrawlRequest(num_books=n_books_i, num_chapters=n_ch_i)

        async def run_and_save():
            results = await crawl_books(req)
            out_dir = os.path.join(os.getcwd(), 'output')
            os.makedirs(out_dir, exist_ok=True)
            fname = f"qnote_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
            fpath = os.path.join(out_dir, fname)
            with open(fpath, 'w', encoding='utf-8') as fh:
                json.dump([r.model_dump() for r in results], fh, ensure_ascii=False, indent=2)
            print(f"Hoàn tất. Lưu {len(results)} truyện vào: {fpath}")

        asyncio.run(run_and_save())
    except KeyboardInterrupt:
        print('\nHủy bởi người dùng')


