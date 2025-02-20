import asyncio
import datetime
import json
import re
import hashlib
from queue import Queue, Empty
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.robotparser import RobotFileParser
import aiohttp
from aiohttp import ClientTimeout, ClientSession
from bs4 import BeautifulSoup, Comment
from flask import Flask, request, jsonify, Response, render_template
import concurrent.futures
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import psutil
import logging
import lxml.etree as ET
import os
from threading import Lock
from typing import Optional, Tuple, List, Dict, Any

# ================================
# Logging and SSE queue configuration
# ================================
logger = logging.getLogger("Crawler")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('[%(asctime)s] %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)
logger.addHandler(handler)

log_queue: Queue = Queue()

def send_log(message: str, level: int = logging.INFO) -> None:
    """Logs a message and pushes it to the SSE log queue."""
    log_msg = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    logger.log(level, message)
    log_queue.put(log_msg)

# ================================
# HTML Processing Utilities
# ================================
def clean_text(raw_html: str) -> str:
    """
    Extract structured text from HTML, preserving headings, paragraphs, and lists.
    """
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
    """
    Extract title from <title> or meta tag og:title.
    """
    soup = BeautifulSoup(raw_html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    meta_title = soup.find("meta", property="og:title")
    if meta_title and meta_title.get("content"):
        return meta_title["content"].strip()
    return ""

# ================================
# Robots.txt Caching and Parsing
# ================================
ROBOTS_CACHE_DIR = "./robots_cache"
if not os.path.exists(ROBOTS_CACHE_DIR):
    os.makedirs(ROBOTS_CACHE_DIR)

def cache_robots_file(domain: str, content: str) -> None:
    """Cache robots.txt content to disk."""
    filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)

def load_cached_robots(domain: str) -> Optional[str]:
    """Load cached robots.txt content if available."""
    filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    return None

async def fetch_with_retries(session: ClientSession, url: str, timeout: ClientTimeout, retries: int = 3) -> Optional[str]:
    """
    Perform HTTP GET with retries.
    """
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    send_log(f"Non-200 response for {url}: {response.status}", logging.WARNING)
        except Exception as e:
            send_log(f"Attempt {attempt+1} failed for {url}: {e}", logging.WARNING)
            await asyncio.sleep(1)
    return None

async def get_robot_parser(session: ClientSession, domain: str, timeout: ClientTimeout) -> Tuple[RobotFileParser, List[str]]:
    """
    Retrieve and parse robots.txt, and extract sitemap URLs.
    """
    rp = RobotFileParser()
    sitemap_urls: List[str] = []
    robots_text = load_cached_robots(domain)
    for scheme in ["https", "http"]:
        robots_url = f"{scheme}://{domain}/robots.txt"
        if robots_text is None:
            robots_text = await fetch_with_retries(session, robots_url, timeout)
            if robots_text:
                cache_robots_file(domain, robots_text)
                send_log(f"Loaded and cached robots.txt from {robots_url}")
            else:
                send_log(f"Robots.txt not found at {robots_url}")
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
    """
    Parse XML sitemap (supports sitemap index) concurrently and return list of URLs.
    """
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
# PagePool: Manage reusable Playwright pages
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
# Fetching Page Content via Playwright
# ================================
async def fetch_page_content(page_pool: PagePool, url: str, timeout_seconds: int) -> Optional[Tuple[str, str]]:
    """
    Use Playwright to load a page, scroll with limits, and return raw HTML and cleaned text.
    """
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
        cleaned = clean_text(raw_content)
        return raw_content, cleaned
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
# CrawlManager: Manages crawling process and data optimization
# ================================
class CrawlManager:
    def __init__(self, start_url: str, max_tokens: int, max_depth: int, concurrency: int, timeout_seconds: int, max_pages: int) -> None:
        self.start_url: str = urldefrag(start_url)[0]
        self.max_tokens: int = max_tokens
        self.max_depth: int = max_depth
        available_cores = psutil.cpu_count(logical=False)
        self.concurrency: int = min(concurrency, max(1, available_cores - 1))  # Dynamic concurrency
        self.timeout_seconds: int = timeout_seconds
        self.max_pages: int = max_pages
        self.result_data: Optional[Dict[str, Any]] = None
        self.cancel_event: asyncio.Event = asyncio.Event()
        self.is_busy: bool = True
        self.token_count: int = 0
        self.domain_throttle = DomainThrottle(limit=2)
        self.pages_crawled: int = 0
        self.visited_urls: set = set()
        self.queue_size: int = 0
        self.result_lock = Lock()

    async def crawl(self) -> List[Dict[str, Any]]:
        """
        Main crawl process with parallel sitemap fetching.
        """
        visited = set()
        visited_lock = asyncio.Lock()
        results: List[Dict[str, Any]] = []
        q: asyncio.Queue = asyncio.Queue()
        await q.put((self.start_url, 0))
        semaphore = asyncio.Semaphore(self.concurrency)
        prefix = self.start_url if self.start_url.endswith('/') else self.start_url + '/'
        session_timeout = ClientTimeout(total=self.timeout_seconds)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            page_pool = PagePool(browser, size=self.concurrency)
            await page_pool.initialize()
            async with aiohttp.ClientSession(timeout=session_timeout) as session:
                domain = urlparse(self.start_url).netloc
                rp, sitemap_urls = await get_robot_parser(session, domain, session_timeout)
                robots_cache: Dict[str, RobotFileParser] = {domain: rp}
                if sitemap_urls:
                    sitemap_tasks = [fetch_sitemap_urls(sitemap, session, session_timeout) for sitemap in sitemap_urls]
                    sitemap_results = await asyncio.gather(*sitemap_tasks)
                    for sitemap_link_urls in sitemap_results:
                        for url in sitemap_link_urls:
                            if url.startswith(prefix):
                                await q.put((url, self.max_depth))
                else:
                    send_log("No sitemap available. Proceeding with recursive crawl.", logging.INFO)

                async def worker() -> None:
                    nonlocal results
                    while True:
                        if self.cancel_event.is_set():
                            break
                        try:
                            url, depth = await asyncio.wait_for(q.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            if self.cancel_event.is_set():
                                break
                            continue
                        if self.cancel_event.is_set():
                            q.task_done()
                            break
                        async with visited_lock:
                            if url in visited:
                                q.task_done()
                                continue
                            if url != self.start_url and not url.startswith(prefix):
                                send_log(f"Skipping out-of-scope URL: {url}", logging.INFO)
                                visited.add(url)
                                q.task_done()
                                continue
                            visited.add(url)
                            self.visited_urls.add(url)
                        current_domain = urlparse(url).netloc
                        if current_domain not in robots_cache:
                            rp_current, _ = await get_robot_parser(session, current_domain, session_timeout)
                            robots_cache[current_domain] = rp_current
                        else:
                            rp_current = robots_cache[current_domain]
                        if rp_current is not None and not rp_current.can_fetch("*", url):
                            send_log(f"Blocked by robots.txt: {url}", logging.INFO)
                            q.task_done()
                            continue
                        send_log(f"Crawling: {url} (Depth: {depth})", logging.INFO)
                        domain_semaphore = await self.domain_throttle.get_semaphore(current_domain)
                        async with semaphore, domain_semaphore:
                            page_result = await fetch_page_content(page_pool, url, self.timeout_seconds)
                        if not page_result:
                            send_log(f"Failed to retrieve content for {url}", logging.WARNING)
                            q.task_done()
                            continue
                        raw_content, cleaned_content = page_result
                        title = extract_title(raw_content) if raw_content else ""
                        tokens = cleaned_content.split()
                        page_token_count = len(tokens)
                        self.token_count += page_token_count
                        content_to_store = cleaned_content if self.token_count < self.max_tokens else "Token limit reached."
                        results.append({
                            "url": url,
                            "title": title,
                            "content": content_to_store,
                            "content_hash": hashlib.md5(cleaned_content.encode('utf-8')).hexdigest(),
                            "crawl_time": datetime.datetime.now().isoformat()
                        })
                        self.pages_crawled += 1
                        send_log(f"Completed: {url}", logging.INFO)
                        if len(results) >= self.max_pages:
                            send_log("Reached max pages limit. Initiating crawl cancellation.", logging.INFO)
                            self.cancel_event.set()
                        if depth < self.max_depth and not self.cancel_event.is_set():
                            soup = BeautifulSoup(raw_content, "html.parser")
                            for a in soup.find_all("a", href=True):
                                next_url = urldefrag(urljoin(url, a["href"]))[0]
                                async with visited_lock:
                                    if next_url not in visited:
                                        await q.put((next_url, depth + 1))
                        self.queue_size = q.qsize()
                        q.task_done()

                worker_tasks = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
                while not q.empty():
                    if self.cancel_event.is_set():
                        try:
                            q.get_nowait()
                            q.task_done()
                        except Exception:
                            break
                    else:
                        await asyncio.sleep(0.1)
                await q.join()
                for task in worker_tasks:
                    task.cancel()
                await asyncio.gather(*worker_tasks, return_exceptions=True)
                await page_pool.close_all()
                await browser.close()
                return results

    def deduplicate_results(self, results: List[Dict[str, Any]]) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
        """
        Deduplicate pages based on content hash, retaining content for storage efficiency.
        """
        common_contents: Dict[str, str] = {}
        deduped_results: List[Dict[str, Any]] = []
        for page in results:
            content = page.get("content", "")
            content_hash = page.get("content_hash", "")
            if content_hash and content_hash not in common_contents:
                common_contents[content_hash] = content
                deduped_results.append(page)
            else:
                page["content"] = ""
                deduped_results.append(page)
        return common_contents, deduped_results

    def preprocess_for_llm(self, result_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Preprocess JSON to inline content for LLM readability, removing unnecessary fields.
        """
        pages = result_data["pages"]
        common_contents = result_data["common_contents"]
        processed_pages = []
        for page in pages:
            if not page["content"] and page["content_hash"] in common_contents:
                page["content"] = common_contents[page["content_hash"]]
            processed_page = {
                "url": page["url"],
                "title": page["title"],
                "content": page["content"],
                "crawl_time": page["crawl_time"]
            }
            processed_pages.append(processed_page)
        return processed_pages

    def run(self) -> None:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            results = new_loop.run_until_complete(self.crawl())
            common_contents, deduped_results = self.deduplicate_results(results)
            with self.result_lock:
                self.result_data = {
                    "crawl_date": datetime.datetime.now().isoformat(),
                    "source": urlparse(self.start_url).netloc,
                    "pages": deduped_results,
                    "common_contents": common_contents,
                    "total_tokens": self.token_count,
                    "pages_crawled": self.pages_crawled,
                    "visited_urls": list(self.visited_urls)
                }
            send_log(f"Crawl completed. {len(deduped_results)} pages found. Total tokens: {self.token_count}", logging.INFO)
            send_log("Data processing complete. JSON file is ready for download.", logging.INFO)
        except Exception as e:
            send_log(f"Crawl terminated with error: {e}", logging.ERROR)
            with self.result_lock:
                self.result_data = {"pages": [], "common_contents": {}}
        finally:
            self.is_busy = False
            send_log("Crawler has been completely shut down.", logging.INFO)
            new_loop.close()

    def cancel(self) -> None:
        self.cancel_event.set()
        send_log("Cancellation requested via CrawlManager. All jobs will be cancelled immediately.", logging.INFO)

# ================================
# Flask Application and Endpoints
# ================================
app = Flask(__name__)
current_crawler: Optional[CrawlManager] = None
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start-crawl', methods=['POST'])
def start_crawl_route():
    global current_crawler, log_queue
    data = request.get_json()
    start_url = data.get("url")
    if not start_url:
        return jsonify({"error": "Missing URL parameter."}), 400
    try:
        max_tokens = int(data.get("max_tokens", 2000000))
    except ValueError:
        max_tokens = 2000000
    try:
        max_depth = int(data.get("max_depth", 2))
    except ValueError:
        max_depth = 2
    try:
        concurrency = int(data.get("concurrency", 3))
    except ValueError:
        concurrency = 3
    try:
        timeout_seconds = int(data.get("timeout", 10))
    except ValueError:
        timeout_seconds = 10
    try:
        max_pages = int(data.get("max_pages", 100))
    except ValueError:
        max_pages = 100
    if current_crawler is not None and current_crawler.is_busy:
        return jsonify({"error": "Server is busy. Please try again later."}), 503
    log_queue = Queue()
    current_crawler = CrawlManager(start_url, max_tokens, max_depth, concurrency, timeout_seconds, max_pages)
    send_log("Crawl process initiated.", logging.INFO)
    executor.submit(current_crawler.run)
    return jsonify({"message": "Crawl started."})

@app.route('/cancel', methods=['POST'])
def cancel():
    global current_crawler
    if current_crawler is not None and current_crawler.is_busy:
        current_crawler.cancel()
        return jsonify({"message": "Crawl cancellation requested. Data crawled so far is available."})
    else:
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
        send_log("Download requested but crawl is still in progress.", logging.INFO)
        return "Crawl is still in progress. Please wait until it finishes.", 503
    with current_crawler.result_lock if current_crawler else Lock():
        if current_crawler is None or current_crawler.result_data is None or not current_crawler.result_data.get("pages"):
            send_log("Download requested but no crawl data is available.", logging.INFO)
            return "No crawl data available.", 404
        send_log("Download requested; sending file.", logging.INFO)
        domain = urlparse(current_crawler.start_url).netloc
        filename = f"{domain.replace('.', '_')}_crawl_result.json"
        json_data = json.dumps(current_crawler.result_data, ensure_ascii=False, indent=2)
        response = Response(json_data, mimetype='application/json')
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response

@app.route('/status')
def status_endpoint():
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        temps = psutil.sensors_temperatures()
        temperature = None
        if temps:
            for sensor_name, entries in temps.items():
                if entries:
                    temperature = entries[0].current
                    break
        if temperature is None:
            temperature = "N/A"
        busy = current_crawler.is_busy if current_crawler is not None else False
        return jsonify({"cpu": cpu, "ram": ram, "temperature": temperature, "busy": busy})
    except Exception as e:
        send_log(f"Error fetching server status: {e}", logging.ERROR)
        return jsonify({"cpu": "--", "ram": "--", "temperature": "--", "busy": False, "error": str(e)})

@app.route('/detailed-status')
def detailed_status():
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        temps = psutil.sensors_temperatures()
        temperature = None
        if temps:
            for sensor_name, entries in temps.items():
                if entries:
                    temperature = entries[0].current
                    break
        if temperature is None:
            temperature = "N/A"
        status_data = {
            "server": {
                "cpu": cpu,
                "ram": ram,
                "temperature": temperature
            },
            "crawler": {}
        }
        if current_crawler:
            status_data["crawler"] = {
                "is_busy": current_crawler.is_busy,
                "total_tokens": current_crawler.token_count,
                "pages_crawled": current_crawler.pages_crawled,
                "visited_urls_count": len(current_crawler.visited_urls),
                "queue_size": current_crawler.queue_size
            }
        else:
            status_data["crawler"] = {"is_busy": False}
        return jsonify(status_data)
    except Exception as e:
        send_log(f"Error fetching detailed status: {e}", logging.ERROR)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5006, threaded=True)
