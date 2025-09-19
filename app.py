from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict
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

CATEGORY_KEYWORDS = [
    "奇幻小说", "情色小说", "情色文学", "文学评论", "武侠小说", "通俗小说",
    "时空穿越小说", "类型", "言情小说", "都市小说", "暗黑小说", "青少年言情小说"
]

class Chapter(BaseModel):
    title: str
    content: str
    source: str

class BookResult(BaseModel):
    id: str
    title: str
    description: str
    source_book: str
    chapters: List[Chapter]

class CrawlRequest(BaseModel):
    num_books: int = 5
    num_chapters: int = 10

def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all('img'):
        img.decompose()
    for a in soup.find_all('a'):
        a.replace_with(a.get_text())
    return str(soup)

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or '', 'html.parser')
    text = soup.get_text(separator='\n')
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return '\n\n'.join(lines)

def sanitize_filename(name: str, max_len: int = 200) -> str:
    name = unicodedata.normalize('NFKD', name)
    name = name.replace('/', ' ').replace('\\', ' ')
    name = re.sub(r'[<>:"|?*]', '', name)
    name = name.strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or 'untitled'

async def fetch_text(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, timeout=20.0, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.text
    except Exception as exc:
        logger.debug("fetch_text error for %s: %s", url, exc)
        return None
    return None

def extract_full_description(dsoup: BeautifulSoup) -> str:
    desc_nodes = dsoup.select('.intro, .detail_intro, .summary, .review, .book-info, .desc, .book-desc')
    parts = []
    for node in desc_nodes:
        html = clean_html(str(node))
        text = html_to_text(html)
        if text and text not in parts:
            parts.append(text)
    if not parts:
        return 'Chưa có mô tả'
    return "\n\n".join(parts)

async def crawl_books(req: CrawlRequest):
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

        # Data structure: category -> subcategory -> list[BookResult]
        results: Dict[str, Dict[str, List[BookResult]]] = {}

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

            description = extract_full_description(dsoup)

            # Crawl subcategory from breadcrumb (category trên web)
            breadcrumb = dsoup.select_one('.breadcrumb a:nth-of-type(2)')
            subcategory = breadcrumb.get_text().strip() if breadcrumb else 'Unknown'

            # Random gán category từ CATEGORY_KEYWORDS
            category = random.choice(CATEGORY_KEYWORDS)

            chapters = []
            for i in range(1, req.num_chapters + 1):
                ch_url = f'https://qnote.qq.com/read/{book_id}/{i}'
                ch_body = await fetch_text(client, ch_url)
                if not ch_body:
                    break
                csoup = BeautifulSoup(ch_body, 'html.parser')
                ch_title_tag = csoup.find('h1')
                ch_title = ch_title_tag.get_text().strip() if ch_title_tag else f'Chương {i}'

                selectors = ['.content', '.chapter', '.read-content', '#content', '.article']
                ch_html = None
                for sel in selectors:
                    nodes = csoup.select(sel)
                    if nodes:
                        ch_html = ''.join(str(n) for n in nodes)
                        logger.debug("[%s] chapter %s: extracted using selector '%s'", book_id, i, sel)
                        break

                if not ch_html:
                    p_nodes = csoup.select('body p') or csoup.find_all('p')
                    if p_nodes:
                        ch_html = ''.join(str(p) for p in p_nodes)
                        logger.debug("[%s] chapter %s: fallback to <p> paragraphs (count=%s)", book_id, i, len(p_nodes))

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

            book = BookResult(
                id=book_id,
                title=title,
                description=description,
                source_book=source_book,
                chapters=chapters
            )

            # Tổ chức kết quả theo category và subcategory
            if category not in results:
                results[category] = {}
            if subcategory not in results[category]:
                results[category][subcategory] = []
            results[category][subcategory].append(book)

        # Chuyển kết quả về dạng list cho API trả về
        output = []
        for cat in CATEGORY_KEYWORDS:
            if cat in results:
                for subcat, books in results[cat].items():
                    output.append({
                        "category": cat,
                        "subcategory": subcat,
                        "books": [b.model_dump() for b in books]
                    })
            else:
                # Nếu cần tạo category trống
                output.append({
                    "category": cat,
                    "subcategory": "",
                    "books": []
                })
        return output

@app.post('/api/crawl')
async def api_crawl(req: CrawlRequest):
    return await crawl_books(req)

@app.get('/', tags=['root'])
async def root():
    return {"status": "ok", "service": "QNote Auto Import Backend"}

@app.get('/api/crawl')
async def api_crawl_get(num_books: int = 2, num_chapters: int = 5):
    req = CrawlRequest(num_books=num_books, num_chapters=num_chapters)
    return await crawl_books(req)

# Các hàm lưu và export giữ nguyên (nếu tổ chức theo category thì cần chỉnh lại export cho đúng cấu trúc mới)

if __name__ == '__main__':
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
                json.dump(results, fh, ensure_ascii=False, indent=2)
            print(f"Hoàn tất. Lưu {len(results)} category vào: {fpath}")

        asyncio.run(run_and_save())
    except KeyboardInterrupt:
        print('\nHủy bởi người dùng')
