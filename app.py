import asyncio
import datetime
import json
from queue import Queue, Empty
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.robotparser import RobotFileParser
import aiohttp
from aiohttp import ClientTimeout, ClientSession
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, Response, render_template
import threading
import concurrent.futures
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import psutil  # pip install psutil
import logging
import lxml.etree as ET  # Sử dụng lxml để parse XML nhanh hơn
import os
import time

# ------------------------------
# Cấu hình logging chuẩn và log_queue cho SSE
# ------------------------------
logger = logging.getLogger("Crawler")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('[%(asctime)s] %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)
logger.addHandler(handler)

log_queue = Queue()

def send_log(message: str, level=logging.INFO):
    """
    Ghi log qua logging và đẩy vào log_queue để SSE.
    """
    logger.log(level, message)
    log_queue.put(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

# ------------------------------
# Caching robots.txt lên ổ đĩa
# ------------------------------
ROBOTS_CACHE_DIR = "./robots_cache"
if not os.path.exists(ROBOTS_CACHE_DIR):
    os.makedirs(ROBOTS_CACHE_DIR)

def cache_robots_file(domain, content):
    filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)

def load_cached_robots(domain):
    filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    return None

# ------------------------------
# Hàm hỗ trợ request với cơ chế retry
# ------------------------------
async def fetch_with_retries(session: ClientSession, url: str, timeout: ClientTimeout, retries=3):
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

# ------------------------------
# Lấy và parse robots.txt (caching kèm retry)
# ------------------------------
async def get_robot_parser(session: ClientSession, domain: str, timeout: ClientTimeout):
    rp = RobotFileParser()
    sitemap_urls = []
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
                    sitemap = line.split(":", 1)[1].strip()
                    sitemap_urls.append(sitemap)
            break
    if not sitemap_urls:
        send_log(f"No sitemap found in robots.txt for {domain}", logging.INFO)
    return rp, sitemap_urls

# ------------------------------
# Parse sitemap sử dụng lxml (hỗ trợ sitemap index)
# ------------------------------
async def fetch_sitemap_urls(sitemap_url: str, session: ClientSession, timeout: ClientTimeout, recursive=True):
    urls = []
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
        for sitemap in root.findall(f"{{{ns}}}sitemap") if ns else root.findall("sitemap"):
            loc_elem = sitemap.find(f"{{{ns}}}loc") if ns else sitemap.find("loc")
            if loc_elem is not None and loc_elem.text:
                loc = loc_elem.text.strip()
                if recursive:
                    urls.extend(await fetch_sitemap_urls(loc, session, timeout, recursive))
    elif root.tag.endswith("urlset"):
        for url_elem in root.findall(f"{{{ns}}}url") if ns else root.findall("url"):
            loc_elem = url_elem.find(f"{{{ns}}}loc") if ns else url_elem.find("loc")
            if loc_elem is not None and loc_elem.text:
                urls.append(loc_elem.text.strip())
        send_log(f"Found {len(urls)} URLs in sitemap: {sitemap_url}", logging.INFO)
    else:
        send_log(f"Unknown sitemap format at {sitemap_url}", logging.WARNING)
    return urls

# ------------------------------
# Tạo một pool để tái sử dụng Playwright pages
# ------------------------------
class PagePool:
    def __init__(self, browser, size=5):
        self.browser = browser
        self.size = size
        self.pool = asyncio.Queue()
    
    async def initialize(self):
        for _ in range(self.size):
            page = await self.browser.new_page()
            await self.pool.put(page)
    
    async def get_page(self):
        return await self.pool.get()
    
    async def release_page(self, page):
        await self.pool.put(page)
    
    async def close_all(self):
        while not self.pool.empty():
            page = await self.pool.get()
            await page.close()

# ------------------------------
# Lấy nội dung trang sử dụng pool của Playwright
# ------------------------------
async def fetch_page_content(page_pool: PagePool, url: str, timeout_seconds: int):
    try:
        page = await page_pool.get_page()
        try:
            await page.goto(url, timeout=timeout_seconds * 1000)
        except PlaywrightTimeoutError as e:
            send_log(f"Timeout navigating to {url}: {e}", logging.WARNING)
            await page_pool.release_page(page)
            return None
        # Cuộn trang để kích hoạt lazy loading
        prev_height = await page.evaluate("document.body.scrollHeight")
        while True:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == prev_height:
                break
            prev_height = new_height
        content = await page.content()
        send_log(f"Page content loaded for: {url}", logging.INFO)
        await page_pool.release_page(page)
        return content
    except Exception as e:
        send_log(f"Error fetching {url} with Playwright: {e}", logging.ERROR)
        return None

# ------------------------------
# Domain throttling: giới hạn số request đồng thời cho mỗi domain
# ------------------------------
class DomainThrottle:
    def __init__(self, limit=2):
        self.limit = limit
        self.domain_semaphores = {}
        self.lock = asyncio.Lock()
    
    async def get_semaphore(self, domain):
        async with self.lock:
            if domain not in self.domain_semaphores:
                self.domain_semaphores[domain] = asyncio.Semaphore(self.limit)
            return self.domain_semaphores[domain]

# ------------------------------
# CrawlManager: quản lý quá trình crawl với các cải tiến
# ------------------------------
class CrawlManager:
    def __init__(self, start_url, max_tokens, max_depth, concurrency, timeout_seconds, max_pages):
        self.start_url = urldefrag(start_url)[0]
        self.max_tokens = max_tokens
        self.max_depth = max_depth
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.result_data = None
        self.cancel_event = asyncio.Event()
        self.is_busy = True
        self.token_count = 0
        self.domain_throttle = DomainThrottle(limit=2)

    async def crawl(self):
        visited = set()
        visited_lock = asyncio.Lock()
        results = []
        q = asyncio.Queue()
        await q.put((self.start_url, 0))
        semaphore = asyncio.Semaphore(self.concurrency)
        prefix = self.start_url if self.start_url.endswith('/') else self.start_url + '/'
        session_timeout = ClientTimeout(total=self.timeout_seconds)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            # Khởi tạo pool page để tái sử dụng
            page_pool = PagePool(browser, size=self.concurrency)
            await page_pool.initialize()
            async with aiohttp.ClientSession(timeout=session_timeout) as session:
                domain = urlparse(self.start_url).netloc
                rp, sitemap_urls = await get_robot_parser(session, domain, session_timeout)
                robots_cache = {domain: rp}
                if sitemap_urls:
                    for sitemap in sitemap_urls:
                        sitemap_link_urls = await fetch_sitemap_urls(sitemap, session, session_timeout)
                        for url in sitemap_link_urls:
                            if url.startswith(prefix):
                                await q.put((url, self.max_depth))
                else:
                    send_log("No sitemap available. Proceeding with recursive crawl as per original logic.", logging.INFO)
                
                async def worker():
                    nonlocal results
                    while True:
                        try:
                            url, depth = await asyncio.wait_for(q.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            # Nếu hàng đợi rỗng, kiểm tra cancel_event
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
                            content = await fetch_page_content(page_pool, url, self.timeout_seconds)
                        if not content:
                            send_log(f"Failed to retrieve content for {url}", logging.WARNING)
                            q.task_done()
                            continue
                        soup = BeautifulSoup(content, "html.parser")
                        title = soup.title.string.strip() if soup.title and soup.title.string else ""
                        text_content = soup.get_text(separator=" ", strip=True)
                        tokens = text_content.split()
                        page_token_count = len(tokens)
                        self.token_count += page_token_count
                        if self.token_count >= self.max_tokens:
                            send_log("Token limit reached. Initiating immediate crawl cancellation.", logging.INFO)
                            self.cancel_event.set()
                        results.append({
                            "url": url,
                            "title": title,
                            "content": text_content if self.token_count < self.max_tokens else "Token limit reached."
                        })
                        send_log(f"Completed: {url}", logging.INFO)
                        if len(results) >= self.max_pages:
                            send_log("Reached max pages limit. Initiating immediate crawl cancellation.", logging.INFO)
                            self.cancel_event.set()
                        if depth < self.max_depth and not self.cancel_event.is_set():
                            for a in soup.find_all("a", href=True):
                                next_url = urldefrag(urljoin(url, a["href"]))[0]
                                async with visited_lock:
                                    if next_url not in visited:
                                        await q.put((next_url, depth+1))
                        q.task_done()

                # Tạo các worker tasks
                worker_tasks = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
                
                # Nếu hủy, giải phóng các mục trong queue để tránh chờ mãi
                while not q.empty():
                    if self.cancel_event.is_set():
                        try:
                            q.get_nowait()
                            q.task_done()
                        except Exception:
                            break
                    else:
                        await asyncio.sleep(0.1)
                # Đợi hàng đợi được xử lý hoàn toàn
                await q.join()
                # Sau khi hoàn thành queue, hủy tất cả worker
                for task in worker_tasks:
                    task.cancel()
                await asyncio.gather(*worker_tasks, return_exceptions=True)
                await page_pool.close_all()
                await browser.close()
                return results

    def run(self):
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            results = new_loop.run_until_complete(self.crawl())
            self.result_data = {"pages": results if results is not None else []}
            send_log(f"Crawl completed. {len(results)} pages found. Total tokens: {self.token_count}", logging.INFO)
        except Exception as e:
            send_log(f"Crawl terminated with error: {e}", logging.ERROR)
            self.result_data = {"pages": []}
        finally:
            self.is_busy = False
            send_log("Crawler has been completely shut down.", logging.INFO)
            new_loop.close()

    def cancel(self):
        self.cancel_event.set()
        send_log("Cancellation requested via CrawlManager.", logging.INFO)

# ------------------------------
# Flask Endpoints
# ------------------------------
app = Flask(__name__)
current_crawler = None
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
    if current_crawler is not None and current_crawler.is_busy:
        send_log("Download requested but crawl is still in progress.", logging.INFO)
        return "Crawl is still in progress. Please wait until it finishes.", 503
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

if __name__ == '__main__':
    # Chạy ứng dụng Flask trên cổng 5006 với chế độ threaded
    app.run(host='0.0.0.0', port=5006, threaded=True)
