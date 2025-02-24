from gevent import monkey
monkey.patch_all()

import asyncio
import datetime
import json
import hashlib
from queue import Queue, Empty
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.robotparser import RobotFileParser
import aiohttp
from aiohttp import ClientTimeout, ClientSession
from bs4 import BeautifulSoup, Comment
from flask import Flask, request, jsonify, Response, render_template, send_file
import concurrent.futures
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import psutil
import logging
import lxml.etree as ET
import os
from threading import Lock, Thread
from typing import Optional, Tuple, List, Dict, Any
import time
import enum

# ================================
# Logging Configuration
# ================================
logger = logging.getLogger("Crawler")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('[%(asctime)s] %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)
logger.addHandler(handler)

log_queue: Queue = Queue()

def send_log(message: str, level: int = logging.INFO) -> None:
    log_msg = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    logger.log(level, message)
    log_queue.put(log_msg)

# ================================
# HTML Processing Utilities
# ================================
def clean_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for comment in soup.findAll(text=lambda text: isinstance(text, Comment)):
        comment.extract()
    text_parts = []
    for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li']):
        text = element.get_text(strip=True)
        if text:
            text_parts.append(text)
    return "\n".join(text_parts)

def extract_title(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    meta_title = soup.find("meta", property="og:title")
    if meta_title and meta_title.get("content"):
        return meta_title["content"].strip()
    return ""

def extract_optimized_text(raw_html: str, selector: Optional[str] = None) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    if selector:
        element = soup.select_one(selector)
        if element:
            for tag in element(["script", "style"]):
                tag.decompose()
            for comment in element.findAll(text=lambda text: isinstance(text, Comment)):
                comment.extract()
            text = element.get_text(separator="\n", strip=True)
            if text:
                return text
    return clean_text(raw_html)

# ================================
# Robots.txt and Sitemap Utilities
# ================================
ROBOTS_CACHE_DIR = "./robots_cache"
if not os.path.exists(ROBOTS_CACHE_DIR):
    os.makedirs(ROBOTS_CACHE_DIR)

def cache_robots_file(domain: str, content: str) -> None:
    filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)

def load_cached_robots(domain: str) -> Optional[str]:
    filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    return None

async def fetch_with_retries(session: ClientSession, url: str, timeout: ClientTimeout, retries: int = 3) -> Optional[str]:
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    return await response.text()
                send_log(f"Non-200 response for {url}: {response.status}", logging.WARNING)
        except Exception as e:
            send_log(f"Attempt {attempt+1} failed for {url}: {e}", logging.WARNING)
            await asyncio.sleep(1)
    return None

async def get_robot_parser(session: ClientSession, domain: str, timeout: ClientTimeout) -> Tuple[RobotFileParser, List[str]]:
    rp = RobotFileParser()
    sitemap_urls: List[str] = []
    robots_text = load_cached_robots(domain)
    for scheme in ["https", "http"]:
        robots_url = f"{scheme}://{domain}/robots.txt"
        if robots_text is None:
            robots_text = await fetch_with_retries(session, robots_url, timeout)
            if robots_text:
                cache_robots_file(domain, robots_text)
                send_log(f"Robots.txt loaded and cached from {robots_url}", logging.INFO)
            else:
                send_log(f"Robots.txt not found at {robots_url}", logging.WARNING)
        if robots_text:
            rp.parse(robots_text.splitlines())
            for line in robots_text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_urls.append(line.split(":", 1)[1].strip())
            break
    if not sitemap_urls:
        send_log(f"No sitemap found in robots.txt for {domain}", logging.INFO)
    return rp, sitemap_urls

async def fetch_sitemap_urls(sitemap_url: str, session: ClientSession, timeout: ClientTimeout, recursive: bool = True) -> List[str]:
    urls: List[str] = []
    text = await fetch_with_retries(session, sitemap_url, timeout)
    if not text:
        send_log(f"Failed to fetch sitemap: {sitemap_url}", logging.WARNING)
        return urls
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except Exception as e:
        send_log(f"XML parse error for sitemap {sitemap_url}: {e}", logging.ERROR)
        return urls
    ns = root.nsmap.get(None, '')
    if root.tag.endswith("sitemapindex"):
        send_log(f"Sitemap index detected: {sitemap_url}", logging.INFO)
        sub_sitemap_urls = []
        for sitemap in root.findall(f"{{{ns}}}sitemap") if ns else root.findall("sitemap"):
            loc_elem = sitemap.find(f"{{{ns}}}loc") if ns else sitemap.find("loc")
            if loc_elem is not None and loc_elem.text:
                sub_sitemap_urls.append(loc_elem.text.strip())
        if recursive and sub_sitemap_urls:
            sub_tasks = [fetch_sitemap_urls(url, session, timeout, recursive) for url in sub_sitemap_urls]
            sub_results = await asyncio.gather(*sub_tasks)
            for sub_urls in sub_results:
                urls.extend(sub_urls)
    elif root.tag.endswith("urlset"):
        for url_elem in root.findall(f"{{{ns}}}url") if ns else root.findall("url"):
            loc_elem = url_elem.find(f"{{{ns}}}loc") if ns else url_elem.find("loc")
            if loc_elem is not None and loc_elem.text:
                urls.append(loc_elem.text.strip())
        send_log(f"Found {len(urls)} URLs in sitemap: {sitemap_url}", logging.INFO)
    else:
        send_log(f"Unknown sitemap format at {sitemap_url}", logging.WARNING)
    return urls

# ================================
# PagePool
# ================================
class PagePool:
    def __init__(self, browser, size: int = 5) -> None:
        self.browser = browser
        self.size = size
        self.pool: asyncio.Queue = asyncio.Queue()

    async def initialize(self) -> None:
        for _ in range(self.size):
            page = await self.browser.new_page()
            await self.pool.put(page)

    async def get_page(self):
        return await self.pool.get()

    async def release_page(self, page) -> None:
        await self.pool.put(page)

    async def close_all(self) -> None:
        while not self.pool.empty():
            page = await self.pool.get()
            await page.close()

# ================================
# Fetch Page Content
# ================================
async def fetch_page_content(page_pool: PagePool, url: str, timeout_seconds: int) -> Optional[Tuple[str, str]]:
    page = await page_pool.get_page()
    try:
        await page.goto(url, timeout=timeout_seconds * 1000)
        max_scrolls = 5
        scroll_count = 0
        prev_height = await page.evaluate("document.body.scrollHeight")
        while scroll_count < max_scrolls:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == prev_height:
                break
            prev_height = new_height
            scroll_count += 1
        raw_content = await page.content()
        send_log(f"Page content loaded for: {url}", logging.INFO)
        await page_pool.release_page(page)
        return raw_content, raw_content
    except PlaywrightTimeoutError as e:
        send_log(f"Timeout navigating to {url}: {e}", logging.WARNING)
        await page_pool.release_page(page)
        return None
    except Exception as e:
        send_log(f"Error fetching {url} with Playwright: {e}", logging.ERROR)
        await page_pool.release_page(page)
        return None

# ================================
# Domain Throttling
# ================================
class DomainThrottle:
    def __init__(self, limit: int = 2) -> None:
        self.limit = limit
        self.domain_semaphores: Dict[str, asyncio.Semaphore] = {}
        self.lock = asyncio.Lock()

    async def get_semaphore(self, domain: str) -> asyncio.Semaphore:
        async with self.lock:
            if domain not in self.domain_semaphores:
                self.domain_semaphores[domain] = asyncio.Semaphore(self.limit)
            return self.domain_semaphores[domain]

# ================================
# FSM States
# ================================
class CrawlerState(enum.Enum):
    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    CRAWLING = "CRAWLING"
    CANCELLING = "CANCELLING"
    FINISHED = "FINISHED"

# ================================
# CrawlManager with FSM
# ================================
class CrawlManager:
    def __init__(self, start_url: str, max_tokens: int, max_depth: int, concurrency: int,
                 timeout_seconds: int, max_pages: int, content_selector: Optional[str] = "",
                 sitemap_url: Optional[str] = "") -> None:
        self.start_url: str = urldefrag(start_url)[0]
        self.max_tokens: int = max_tokens
        self.max_depth: int = max_depth
        self.concurrency: int = min(concurrency, max(1, psutil.cpu_count(logical=False) - 1))
        self.timeout_seconds: int = timeout_seconds
        self.max_pages: int = max_pages
        self.content_selector: str = content_selector.strip()
        self.sitemap_url: str = sitemap_url.strip()
        self.state: CrawlerState = CrawlerState.IDLE
        self.result_data: Optional[Dict[str, Any]] = None
        self.cancel_event: asyncio.Event = asyncio.Event()
        self.token_count: int = 0
        self.domain_throttle = DomainThrottle(limit=2)
        self.pages_crawled: int = 0
        self.visited_urls: set = set()
        self.queue_size: int = 0
        self.result_lock = Lock()
        self.user_root_url: str = self.start_url
        self.currently_crawling: set = set()
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self.worker_tasks: List[asyncio.Task] = []
        self.results: List[Dict[str, Any]] = []  # Store results incrementally

    def transition_to(self, new_state: CrawlerState) -> None:
        old_state = self.state
        self.state = new_state
        send_log(f"State transition: {old_state.value} -> {new_state.value}", logging.INFO)
        if new_state == CrawlerState.CANCELLING:
            self.save_partial_results()

    @property
    def is_busy(self) -> bool:
        return self.state in (CrawlerState.INITIALIZING, CrawlerState.CRAWLING, CrawlerState.CANCELLING)

    def save_partial_results(self) -> None:
        """Save the current progress to file when cancelling or finishing."""
        with self.result_lock:
            self.result_data = {
                "crawl_date": datetime.datetime.now().isoformat(),
                "source": urlparse(self.start_url).netloc,
                "pages": self.results,
                "total_tokens": self.token_count,
                "pages_crawled": self.pages_crawled,
                "visited_urls": list(self.visited_urls),
                "status": "cancelled" if self.state == CrawlerState.CANCELLING else "completed"
            }
            save_crawl_result(self.start_url, self.result_data)
            send_log(f"Saved partial results: {self.pages_crawled} pages", logging.INFO)

    async def initialize_crawl(self, session: ClientSession, page_pool: PagePool) -> None:
        self.transition_to(CrawlerState.INITIALIZING)
        prefix = self.start_url if self.start_url.endswith('/') else self.start_url + '/'
        session_timeout = ClientTimeout(total=self.timeout_seconds)
        domain = urlparse(self.start_url).netloc
        rp, robots_sitemaps = await get_robot_parser(session, domain, session_timeout)

        await self.task_queue.put((self.start_url, 0))
        if self.sitemap_url:
            send_log("Using manually provided sitemap URL(s)", logging.INFO)
            sitemap_list = [s.strip() for s in self.sitemap_url.split(",") if s.strip()]
            sitemap_tasks = [fetch_sitemap_urls(s, session, session_timeout) for s in sitemap_list]
            sitemap_results = await asyncio.gather(*sitemap_tasks)
            for sitemap_link_urls in sitemap_results:
                for url in sitemap_link_urls:
                    if url.startswith(prefix):
                        await self.task_queue.put((url, self.max_depth))
            send_log(f"Added {self.task_queue.qsize()} URLs from manual sitemap", logging.INFO)
        elif robots_sitemaps:
            send_log("Using sitemap from robots.txt", logging.INFO)
            sitemap_tasks = [fetch_sitemap_urls(s, session, session_timeout) for s in robots_sitemaps]
            sitemap_results = await asyncio.gather(*sitemap_tasks)
            for sitemap_link_urls in sitemap_results:
                for url in sitemap_link_urls:
                    if url.startswith(prefix):
                        await self.task_queue.put((url, self.max_depth))
            send_log(f"Added {self.task_queue.qsize()} URLs from robots.txt sitemap", logging.INFO)
        else:
            send_log("No sitemap available; starting recursive crawl", logging.INFO)

    async def crawl_worker(self, session: ClientSession, page_pool: PagePool, semaphore: asyncio.Semaphore,
                          visited: set, visited_lock: asyncio.Lock) -> None:
        robots_cache: Dict[str, RobotFileParser] = {}
        prefix = self.start_url if self.start_url.endswith('/') else self.start_url + '/'
        while self.state == CrawlerState.CRAWLING:
            try:
                url, depth = await asyncio.wait_for(self.task_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if self.cancel_event.is_set():
                    break
                continue

            async with visited_lock:
                if url in visited:
                    self.task_queue.task_done()
                    continue
                if url != self.start_url and not url.startswith(prefix):
                    send_log(f"Skipping out-of-scope URL: {url}", logging.INFO)
                    visited.add(url)
                    self.task_queue.task_done()
                    continue
                visited.add(url)
                self.visited_urls.add(url)
                self.currently_crawling.add(url)

            try:
                current_domain = urlparse(url).netloc
                if current_domain not in robots_cache:
                    rp, _ = await get_robot_parser(session, current_domain, ClientTimeout(total=self.timeout_seconds))
                    robots_cache[current_domain] = rp
                else:
                    rp = robots_cache[current_domain]

                if rp and not rp.can_fetch("*", url):
                    send_log(f"URL blocked by robots.txt: {url}", logging.INFO)
                    self.task_queue.task_done()
                    continue

                send_log(f"Processing URL: {url} (Depth: {depth})", logging.INFO)
                domain_semaphore = await self.domain_throttle.get_semaphore(current_domain)
                async with semaphore, domain_semaphore:
                    page_result = await fetch_page_content(page_pool, url, self.timeout_seconds)
                if not page_result:
                    send_log(f"Content retrieval failed for URL: {url}", logging.WARNING)
                    self.task_queue.task_done()
                    continue

                raw_content, _ = page_result
                cleaned_content = (extract_optimized_text(raw_content, self.content_selector)
                                 if self.content_selector else clean_text(raw_content))
                title = extract_title(raw_content) if raw_content else ""
                tokens = cleaned_content.split()
                page_token_count = len(tokens)
                self.token_count += page_token_count

                content_to_store = (cleaned_content if self.token_count < self.max_tokens
                                  else "Token limit reached.")
                page_data = {
                    "url": url,
                    "title": title,
                    "content": content_to_store,
                    "content_hash": hashlib.md5(cleaned_content.encode('utf-8')).hexdigest(),
                    "crawl_time": datetime.datetime.now().isoformat()
                }
                self.results.append(page_data)
                self.pages_crawled += 1
                send_log(f"Completed processing URL: {url} (Tokens: {page_token_count})", logging.INFO)

                if len(self.results) >= self.max_pages or self.token_count >= self.max_tokens:
                    send_log(f"Limit reached: {len(self.results)} pages or {self.token_count} tokens", logging.INFO)
                    self.transition_to(CrawlerState.CANCELLING)
                    break
                elif depth < self.max_depth and self.state == CrawlerState.CRAWLING:
                    soup = BeautifulSoup(raw_content, "html.parser")
                    for a in soup.find_all("a", href=True):
                        next_url = urldefrag(urljoin(url, a["href"]))[0]
                        async with visited_lock:
                            if next_url not in visited:
                                await self.task_queue.put((next_url, depth + 1))
                self.queue_size = self.task_queue.qsize()
            except Exception as e:
                send_log(f"Error processing URL {url}: {e}", logging.ERROR)
            finally:
                self.currently_crawling.discard(url)
                self.task_queue.task_done()

    async def crawl(self) -> List[Dict[str, Any]]:
        self.results = []  # Reset results for each crawl
        visited: set = set()
        visited_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(self.concurrency)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            page_pool = PagePool(browser, size=self.concurrency)
            await page_pool.initialize()
            async with aiohttp.ClientSession(timeout=ClientTimeout(total=self.timeout_seconds)) as session:
                await self.initialize_crawl(session, page_pool)
                self.transition_to(CrawlerState.CRAWLING)
                send_log(f"Crawl started with {self.task_queue.qsize()} initial tasks", logging.INFO)

                self.worker_tasks = [
                    asyncio.create_task(self.crawl_worker(session, page_pool, semaphore, visited, visited_lock))
                    for _ in range(self.concurrency)
                ]

                # Wait for either queue completion or cancellation
                await asyncio.wait(
                    [self.task_queue.join(), self.cancel_event.wait()],
                    return_when=asyncio.FIRST_COMPLETED
                )

                if self.cancel_event.is_set():
                    self.transition_to(CrawlerState.CANCELLING)
                elif self.state == CrawlerState.CRAWLING:
                    self.transition_to(CrawlerState.FINISHED)

                # Cancel all worker tasks cleanly
                for task in self.worker_tasks:
                    task.cancel()
                await asyncio.gather(*self.worker_tasks, return_exceptions=True)

                # Cleanup resources
                await page_pool.close_all()
                await browser.close()

                # Save results if not already saved (e.g., on FINISHED state)
                if self.state != CrawlerState.CANCELLING:
                    self.save_partial_results()

                send_log(f"Crawl ended with state: {self.state.value}", logging.INFO)
                return self.results

    def run(self) -> None:
        self.transition_to(CrawlerState.IDLE)
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            results = new_loop.run_until_complete(self.crawl())
            with self.result_lock:
                self.result_data = {
                    "crawl_date": datetime.datetime.now().isoformat(),
                    "source": urlparse(self.start_url).netloc,
                    "pages": results,
                    "total_tokens": self.token_count,
                    "pages_crawled": self.pages_crawled,
                    "visited_urls": list(self.visited_urls),
                    "status": "completed"
                }
        except Exception as e:
            send_log(f"Crawl failed: {e}", logging.ERROR)
            with self.result_lock:
                self.result_data = {"pages": self.results, "status": "failed"}
            self.save_partial_results()
        finally:
            self.transition_to(CrawlerState.IDLE)
            global current_crawler
            current_crawler = None
            process_queue()  # Immediately trigger queue processing
            new_loop.close()

    def cancel(self) -> None:
        if self.state in (CrawlerState.INITIALIZING, CrawlerState.CRAWLING):
            self.transition_to(CrawlerState.CANCELLING)
            self.cancel_event.set()
            send_log("Crawl cancellation initiated", logging.INFO)

# ================================
# Persistence and Queue
# ================================
CRAWL_RESULTS_DIR = "./crawl_results"
if not os.path.exists(CRAWL_RESULTS_DIR):
    os.makedirs(CRAWL_RESULTS_DIR)

CRAWLED_MAPPING_FILE = "crawled_mapping.json"
if os.path.exists(CRAWLED_MAPPING_FILE):
    with open(CRAWLED_MAPPING_FILE, "r", encoding="utf-8") as f:
        crawled_mapping = json.load(f)
else:
    crawled_mapping = {}

QUEUE_FILE = "crawl_queue.json"
if os.path.exists(QUEUE_FILE):
    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        crawl_queue_data = json.load(f)
else:
    crawl_queue_data = []

def save_crawl_result(url: str, result_data: Dict[str, Any]) -> str:
    domain = urlparse(url).netloc
    file_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    filename = f"{domain.replace('.', '_')}_{file_hash}.json"
    filepath = os.path.join(CRAWL_RESULTS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2)
    crawled_mapping[url] = {
        "filename": filename,
        "last_updated": datetime.datetime.now().isoformat()
    }
    with open(CRAWLED_MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(crawled_mapping, f, ensure_ascii=False, indent=2)
    return filename

def load_queue() -> List[Dict[str, Any]]:
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            try:
                queue_data = json.load(f)
                deduped = {}
                for req in queue_data:
                    url = req.get("url")
                    if url:
                        if url in deduped:
                            existing_time = deduped[url]["submitted_at"]
                            current_time = req.get("submitted_at")
                            if current_time < existing_time:
                                deduped[url]["submitted_at"] = current_time
                        else:
                            deduped[url] = req
                return list(deduped.values())
            except Exception as e:
                send_log(f"Error reading queue: {e}", logging.ERROR)
                return []
    return []

def save_queue(queue: List[Dict[str, Any]]) -> None:
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

def add_to_queue(crawl_request: Dict[str, Any]) -> None:
    queue = load_queue()
    new_url = crawl_request.get("url")
    found = False
    for req in queue:
        if req.get("url") == new_url:
            found = True
            send_log(f"Queue already contains request for {new_url}; merging", logging.INFO)
            break
    if not found:
        crawl_request["submitted_at"] = datetime.datetime.now().isoformat()
        queue.append(crawl_request)
        save_queue(queue)
        send_log(f"Added request for {new_url} to queue (Total: {len(queue)})", logging.INFO)

def process_queue():
    global current_crawler
    queue = load_queue()
    if queue and (current_crawler is None or not current_crawler.is_busy):
        crawl_request = queue.pop(0)
        save_queue(queue)
        send_log(f"Processing queued request for {crawl_request.get('url', 'unknown')} (Remaining: {len(queue)})", logging.INFO)
        current_crawler = CrawlManager(
            crawl_request.get("url"),
            int(crawl_request.get("max_tokens", 2000000)),
            int(crawl_request.get("max_depth", 2)),
            int(crawl_request.get("concurrency", 3)),
            int(crawl_request.get("timeout", 10)),
            int(crawl_request.get("max_pages", 100)),
            content_selector=crawl_request.get("content_selector", ""),
            sitemap_url=crawl_request.get("sitemap_url", "")
        )
        executor.submit(current_crawler.run)
    else:
        send_log("No tasks in queue or crawler busy", logging.INFO)


def queue_worker():
    while True:
        if current_crawler is None or not current_crawler.is_busy:
            process_queue()
        else:
            send_log("Queue worker waiting for crawler to finish", logging.DEBUG)
        time.sleep(1)  # Reduced sleep time for faster queue processing

# ================================
# Flask Application
# ================================
app = Flask(__name__)
current_crawler: Optional[CrawlManager] = None
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start-crawl', methods=['POST'])
def start_crawl_route():
    global current_crawler, crawled_mapping
    data = request.get_json()
    start_url = data.get("url")
    force = data.get("force", False)
    content_selector = data.get("content_selector", "").strip()
    sitemap_url = data.get("sitemap_url", "").strip()
    if not start_url:
        return jsonify({"error": "Missing URL parameter."}), 400

    if start_url in crawled_mapping and not force:
        return jsonify({
            "message": "This URL has been crawled before. Do you want to re-crawl to update?",
            "cached_data": crawled_mapping[start_url]
        }), 200

    if current_crawler is not None and current_crawler.is_busy:
        data["content_selector"] = content_selector
        data["sitemap_url"] = sitemap_url
        add_to_queue(data)
        send_log("Crawler busy; request queued", logging.INFO)
        return jsonify({"message": "Server is busy; your crawl request has been queued.", "queued": True}), 200

    current_crawler = CrawlManager(
        start_url,
        int(data.get("max_tokens", 2000000)),
        int(data.get("max_depth", 2)),
        int(data.get("concurrency", 3)),
        int(data.get("timeout", 10)),
        int(data.get("max_pages", 100)),
        content_selector=content_selector,
        sitemap_url=sitemap_url
    )
    send_log("Crawl initiated", logging.INFO)
    executor.submit(current_crawler.run)
    return jsonify({"message": "Crawl started."})

@app.route('/cancel', methods=['POST'])
def cancel():
    global current_crawler
    if current_crawler is not None and current_crawler.is_busy:
        current_crawler.cancel()
        return jsonify({"message": "Crawl cancellation requested. Partial crawl data is available."})
    return jsonify({"message": "No crawl in progress."}), 400

@app.route('/stream')
def stream():
    def event_stream():
        while True:
            try:
                message = log_queue.get(timeout=1)
                yield f"data: {message}\n\n"
            except Empty:
                yield ": keep-alive\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/download')
def download():
    global current_crawler
    if current_crawler is not None and current_crawler.is_busy and not current_crawler.cancel_event.is_set():
        send_log("Download requested but crawl in progress", logging.INFO)
        return "Crawl is still in progress. Please wait until it finishes.", 503
    with current_crawler.result_lock if current_crawler else Lock():
        if current_crawler is None or current_crawler.result_data is None or not current_crawler.result_data.get("pages"):
            send_log("No crawl data available for download", logging.INFO)
            return "No crawl data available.", 404
        send_log("Sending crawl result file", logging.INFO)
        domain = urlparse(current_crawler.start_url).netloc
        filename = f"{domain.replace('.', '_')}_crawl_result.json"
        json_data = json.dumps(current_crawler.result_data, ensure_ascii=False, indent=2)
        response = Response(json_data, mimetype='application/json')
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response

@app.route('/crawled-files')
def crawled_files():
    if os.path.exists(CRAWLED_MAPPING_FILE):
        with open(CRAWLED_MAPPING_FILE, "r", encoding="utf-8") as f:
            mapping = json.load(f)
    else:
        mapping = {}
    files_list = [
        {"url": url, "filename": info.get("filename"), "last_updated": info.get("last_updated")}
        for url, info in mapping.items()
    ]
    return jsonify(files_list)

@app.route('/download-file')
def download_file():
    filename = request.args.get("file")
    if not filename:
        return "Missing file parameter.", 400
    filepath = os.path.join(CRAWL_RESULTS_DIR, filename)
    if not os.path.exists(filepath):
        return "File not found.", 404
    return send_file(filepath, as_attachment=True)

@app.route('/queue')
def queue_status():
    queue = load_queue()
    return jsonify(queue)

@app.route('/status')
def status_endpoint():
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        temps = psutil.sensors_temperatures()
        temperature = "N/A"
        if temps:
            for sensor_name, readings in temps.items():
                if readings:
                    temperature = readings[0].current
                    break
        crawler_state = current_crawler.state.value if current_crawler else "NONE"
        busy = current_crawler.is_busy if current_crawler else False
        queue_len = len(load_queue())
        return jsonify({
            "cpu": cpu,
            "ram": ram,
            "temperature": temperature,
            "busy": busy,
            "queue_length": queue_len,
            "crawler_state": crawler_state
        })
    except Exception as e:
        send_log(f"Error fetching status: {e}", logging.ERROR)
        return jsonify({"cpu": "--", "ram": "--", "temperature": "--", "busy": False, "queue_length": 0, "crawler_state": "NONE", "error": str(e)})

@app.route('/detailed-status')
def detailed_status():
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        temps = psutil.sensors_temperatures()
        temperature = "N/A"
        if temps:
            for sensor_name, readings in temps.items():
                if readings:
                    temperature = readings[0].current
                    break
        crawler_state = current_crawler.state.value if current_crawler else "NONE"
        status_data = {
            "server": {
                "cpu": cpu,
                "ram": ram,
                "temperature": temperature,
                "crawler_state": crawler_state
            },
            "crawler": {"is_busy": False} if not current_crawler else {
                "is_busy": current_crawler.is_busy,
                "total_tokens": current_crawler.token_count,
                "pages_crawled": current_crawler.pages_crawled,
                "visited_urls_count": len(current_crawler.visited_urls),
                "queue_size": current_crawler.queue_size,
                "current_urls": [current_crawler.start_url]
            },
            "queue_length": len(load_queue())
        }
        return jsonify(status_data)
    except Exception as e:
        send_log(f"Error fetching detailed status: {e}", logging.ERROR)
        return jsonify({"error": str(e)}), 500

@app.route('/crawler-state')
def crawler_state():
    try:
        if current_crawler:
            return jsonify({"state": current_crawler.state.value})
        else:
            return jsonify({"state": "NONE"})
    except Exception as e:
        send_log(f"Error fetching crawler state: {e}", logging.ERROR)
        return jsonify({"state": "NONE", "error": str(e)}), 500

@app.route('/resume-queue', methods=['POST'])
def resume_queue():
    global current_crawler
    if current_crawler is None or not current_crawler.is_busy:
        process_queue()
        send_log("Queue resumed by user", logging.INFO)
        return jsonify({"message": "Queue resumed."}), 200
    send_log("Queue resume requested but crawler busy", logging.WARNING)
    return jsonify({"message": "Crawler is busy; queue is active."}), 200

if __name__ == '__main__':
    Thread(target=queue_worker, daemon=True).start()
    if load_queue():
        send_log(f"Server started with {len(load_queue())} queued tasks", logging.INFO)
    process_queue()
    app.run(host='0.0.0.0', port=5006, threaded=True)
