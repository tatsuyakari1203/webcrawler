from gevent import monkey
monkey.patch_all()

import asyncio
import datetime
import json
import hashlib
import os
import time
import logging
import traceback
from queue import Queue, Empty
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.robotparser import RobotFileParser
import aiohttp
from aiohttp import ClientTimeout, ClientSession, TCPConnector
from bs4 import BeautifulSoup, Comment
from flask import Flask, request, jsonify, Response, render_template, send_file
import concurrent.futures
import psutil
from threading import Thread, Lock
from typing import Optional, List, Dict, Any, Tuple

# Import Crawl4AI
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

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

def send_log(message: str, level: int = logging.INFO, context: Optional[str] = None) -> None:
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"[{timestamp}] [{context}] {message}" if context else f"[{timestamp}] {message}"
    logger.log(level, log_msg)
    log_queue.put(log_msg)

# ================================
# Robots.txt and Sitemap functions (cập nhật theo review)
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

def get_crawl_delay_from_text(robots_text: str) -> Optional[int]:
    for line in robots_text.splitlines():
        if line.lower().startswith("crawl-delay:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None

async def fetch_with_retries(session: ClientSession, url: str, timeout: ClientTimeout, retries: int = 3, backoff_factor: float = 1.5) -> Optional[str]:
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    send_log(f"Non-200 response for {url}: {response.status}", logging.WARNING, context="HTTP")
        except Exception as e:
            send_log(f"Attempt {attempt+1} failed for {url}: {e}", logging.WARNING, context="HTTP")
            await asyncio.sleep(backoff_factor ** attempt)
    return None

async def get_robot_parser(session: ClientSession, domain: str, timeout: ClientTimeout) -> Tuple[RobotFileParser, List[str], Optional[int]]:
    rp = RobotFileParser()
    sitemap_urls: List[str] = []
    robots_text = load_cached_robots(domain)
    crawl_delay = None

    # Thêm kiểm tra thời hạn cache (24 giờ)
    cache_file = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    cache_expired = False
    if robots_text and os.path.exists(cache_file):
        file_age = time.time() - os.path.getmtime(cache_file)
        if file_age > 86400:  # 24 giờ
            cache_expired = True
            send_log(f"Robots.txt cache expired for {domain}, refreshing", context="ROBOTS")

    if robots_text is None or cache_expired:
        for scheme in ["https", "http"]:
            robots_url = f"{scheme}://{domain}/robots.txt"
            try:
                robots_text = await fetch_with_retries(session, robots_url, timeout)
                if robots_text:
                    cache_robots_file(domain, robots_text)
                    send_log(f"Robots.txt loaded and cached from {robots_url}", context="ROBOTS")
                    break
            except Exception as e:
                send_log(f"Error fetching robots.txt from {robots_url}: {str(e)}", logging.WARNING, context="ROBOTS")
        if not robots_text:
            send_log(f"No robots.txt found for {domain}, assuming all allowed", context="ROBOTS")
            # Ghi robots.txt rỗng để tránh các nỗ lực thất bại lặp lại
            cache_robots_file(domain, "")

    if robots_text:
        rp.parse(robots_text.splitlines())
        for line in robots_text.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip()
                if sitemap_url:  # Kiểm tra URL không rỗng
                    sitemap_urls.append(sitemap_url)
        crawl_delay = get_crawl_delay_from_text(robots_text)
        if crawl_delay:
            send_log(f"Crawl-delay detected: {crawl_delay} seconds for domain {domain}", context="ROBOTS")
    return rp, sitemap_urls, crawl_delay

async def fetch_sitemap_urls(sitemap_url: str, session: ClientSession, timeout: ClientTimeout, recursive: bool = True) -> List[str]:
    urls: List[str] = []
    try:
        text = await fetch_with_retries(session, sitemap_url, timeout)
        if not text:
            send_log(f"Failed to fetch sitemap: {sitemap_url}", logging.WARNING, context="SITEMAP")
            return urls

        # Phát hiện sitemap dạng text/plain (danh sách URL)
        if not text.strip().startswith('<?xml'):
            for line in text.strip().splitlines():
                if line.startswith('http'):
                    urls.append(line.strip())
            if urls:
                send_log(f"Found {len(urls)} URLs in plain text sitemap: {sitemap_url}", context="SITEMAP")
                return urls

        import lxml.etree as ET
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.XMLSyntaxError as xml_err:
            send_log(f"XML syntax error in sitemap {sitemap_url}: {xml_err}", logging.ERROR, context="SITEMAP")
            return urls

        ns = root.nsmap.get(None, '')
        if root.tag.endswith("sitemapindex"):
            send_log(f"Sitemap index detected: {sitemap_url}", context="SITEMAP")
            sub_sitemap_urls = []
            for sitemap in root.findall(f"{{{ns}}}sitemap") if ns else root.findall("sitemap"):
                loc_elem = sitemap.find(f"{{{ns}}}loc") if ns else sitemap.find("loc")
                if loc_elem is not None and loc_elem.text:
                    sub_url = loc_elem.text.strip()
                    if sub_url:
                        sub_sitemap_urls.append(sub_url)
            if recursive and sub_sitemap_urls:
                sub_tasks = [fetch_sitemap_urls(url, session, timeout, recursive) for url in sub_sitemap_urls]
                sub_results = await asyncio.gather(*sub_tasks, return_exceptions=True)
                for result in sub_results:
                    if isinstance(result, Exception):
                        send_log(f"Error processing sub-sitemap: {result}", logging.ERROR, context="SITEMAP")
                    else:
                        urls.extend(result)
        elif root.tag.endswith("urlset"):
            for url_elem in root.findall(f"{{{ns}}}url") if ns else root.findall("url"):
                loc_elem = url_elem.find(f"{{{ns}}}loc") if ns else url_elem.find("loc")
                if loc_elem is not None and loc_elem.text:
                    url_str = loc_elem.text.strip()
                    if url_str:
                        urls.append(url_str)
            send_log(f"Found {len(urls)} URLs in sitemap: {sitemap_url}", context="SITEMAP")
        else:
            send_log(f"Unknown sitemap format at {sitemap_url}", logging.WARNING, context="SITEMAP")
    except Exception as e:
        send_log(f"Error processing sitemap {sitemap_url}: {str(e)}", logging.ERROR, context="SITEMAP")
    return urls

# ================================
# Recursive Crawl Manager sử dụng Crawl4AI (cập nhật)
# ================================
class RecursiveCrawlManager:
    def __init__(self, start_url: str, max_depth: int, max_pages: int, content_selector: str = "", sitemap_url: str = "") -> None:
        self.start_url = urldefrag(start_url)[0]
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.content_selector = content_selector.strip()
        self.sitemap_url = sitemap_url.strip()
        self.visited_urls = set()
        self.results = []
        self.pages_crawled = 0
        self.domain = urlparse(self.start_url).netloc
        self.prefix = self.start_url if self.start_url.endswith("/") else self.start_url + "/"
        self.queue = asyncio.PriorityQueue()
        self.cancel_event = asyncio.Event()
        self.is_busy = True
        self.token_count = 0
        self.queue_size = 0
        self.result_lock = Lock()
        self.currently_crawling = set()
        send_log(f"RecursiveCrawlManager initialized for {self.start_url}", context="CRAWL")

    async def add_initial_tasks(self, session: ClientSession, timeout: ClientTimeout):
        if self.sitemap_url:
            send_log("Using provided sitemap URL.", context="SITEMAP")
            sitemap_list = [s.strip() for s in self.sitemap_url.split(",") if s.strip()]
            for sitemap in sitemap_list:
                sitemap_urls = await fetch_sitemap_urls(sitemap, session, timeout)
                for url in sitemap_urls:
                    if url.startswith(self.prefix):
                        await self.queue.put((self.max_depth, (url, self.max_depth)))
        else:
            rp, robots_sitemaps, _ = await get_robot_parser(session, self.domain, timeout)
            if robots_sitemaps:
                send_log("Sitemap detected from robots.txt. Adding URLs.", context="SITEMAP")
                for sitemap in robots_sitemaps:
                    sitemap_urls = await fetch_sitemap_urls(sitemap, session, timeout)
                    for url in sitemap_urls:
                        if url.startswith(self.prefix):
                            await self.queue.put((self.max_depth, (url, self.max_depth)))
            else:
                send_log("No sitemap available; starting with root URL.", context="CRAWL")
        await self.queue.put((0, (self.start_url, 0)))

    async def crawl(self) -> list:
        session_timeout = ClientTimeout(total=15)
        connector = TCPConnector(limit_per_host=2)
        
        # Đặt timeout tổng cho crawl (ví dụ: 1 giờ)
        overall_timeout = time.time() + 3600
        
        # Tạo dict để rate limit theo domain
        domain_limiters = {}
        
        async with aiohttp.ClientSession(timeout=session_timeout, connector=connector) as session:
            try:
                await self.add_initial_tasks(session, session_timeout)
                
                while (not self.queue.empty() and 
                       self.pages_crawled < self.max_pages and 
                       not self.cancel_event.is_set() and
                       time.time() < overall_timeout):
                    
                    try:
                        priority, (url, depth) = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                        
                    # Validate URL format
                    try:
                        parsed = urlparse(url)
                        if not parsed.scheme or not parsed.netloc:
                            send_log(f"Invalid URL format: {url}", logging.WARNING, context="CRAWL")
                            self.queue.task_done()
                            continue
                    except Exception:
                        send_log(f"URL parsing error: {url}", logging.WARNING, context="CRAWL")
                        self.queue.task_done()
                        continue
                    
                    # Skip nếu đã duyệt
                    if url in self.visited_urls:
                        self.queue.task_done()
                        continue
                    
                    # Kiểm tra phạm vi domain
                    if url != self.start_url and not url.startswith(self.prefix):
                        send_log(f"Skipping out-of-scope URL: {url}", context="CRAWL")
                        self.visited_urls.add(url)
                        self.queue.task_done()
                        continue
                    
                    # Bỏ qua các file nhị phân
                    if any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.pdf', '.zip', '.exe']):
                        send_log(f"Skipping binary content URL: {url}", context="CRAWL")
                        self.visited_urls.add(url)
                        self.queue.task_done()
                        continue
                    
                    self.visited_urls.add(url)
                    self.currently_crawling.add(url)
                    
                    # Lấy domain và áp dụng rate limiting
                    domain = urlparse(url).netloc
                    rp, _, crawl_delay = await get_robot_parser(session, domain, session_timeout)
                    
                    if domain not in domain_limiters:
                        domain_limiters[domain] = time.time()
                    else:
                        delay_time = crawl_delay if crawl_delay else 1  # Mặc định 1 giây nếu không có crawl-delay
                        time_since_last = time.time() - domain_limiters[domain]
                        if time_since_last < delay_time:
                            # Đưa lại URL vào hàng đợi để tôn trọng rate limit
                            await asyncio.sleep(0.1)
                            await self.queue.put((priority, (url, depth)))
                            self.currently_crawling.discard(url)
                            self.visited_urls.discard(url)  # Cho phép thử lại
                            self.queue.task_done()
                            continue
                    
                    domain_limiters[domain] = time.time()
                    
                    # Kiểm tra robots.txt cho phép hay không
                    if rp and not rp.can_fetch("*", url):
                        send_log(f"Blocked by robots.txt: {url}", context="CRAWL")
                        self.queue.task_done()
                        continue
                    
                    # Thực hiện crawl bằng Crawl4AI
                    browser_cfg = BrowserConfig(headless=True, verbose=True)
                    run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
                    if self.content_selector:
                        run_cfg.css_selector = self.content_selector
                    try:
                        async with AsyncWebCrawler(config=browser_cfg) as crawler:
                            result = await crawler.arun(url=url, config=run_cfg)
                    except Exception as e:
                        send_log(f"Error crawling {url}: {e}", logging.ERROR, context="CRAWL")
                        self.queue.task_done()
                        self.currently_crawling.discard(url)
                        continue
                    
                    if result and result.success:
                        title = result.markdown.split("\n")[0] if result.markdown else ""
                        self.results.append({
                            "url": url,
                            "title": title,
                            "markdown": result.markdown,
                            "html_hash": hashlib.md5(result.html.encode("utf-8")).hexdigest(),
                            "crawl_time": datetime.datetime.now().isoformat()
                        })
                        self.pages_crawled += 1
                        send_log(f"Completed URL: {url}", context="CRAWL")
                        
                        # Xử lý chuyển hướng
                        if hasattr(result, "final_url") and result.final_url and result.final_url != url:
                            send_log(f"URL {url} redirected to {result.final_url}", context="CRAWL")
                            self.visited_urls.add(result.final_url)
                        
                        # Nếu trong giới hạn độ sâu, trích xuất các liên kết mới
                        if depth < self.max_depth:
                            soup = BeautifulSoup(result.html, "html.parser")
                            for a in soup.find_all("a", href=True):
                                next_url = urldefrag(urljoin(url, a["href"]))[0]
                                if next_url not in self.visited_urls and next_url.startswith(self.prefix):
                                    await self.queue.put((depth + 1, (next_url, depth + 1)))
                    else:
                        send_log(f"Failed to crawl: {url}", logging.WARNING, context="CRAWL")
                    
                    self.currently_crawling.discard(url)
                    self.queue.task_done()
                    self.queue_size = self.queue.qsize()
            except asyncio.CancelledError:
                send_log("Crawl task cancelled", context="CRAWL")
            except Exception as e:
                send_log(f"Unexpected error during crawl: {str(e)}", logging.ERROR, context="CRAWL")
                send_log(traceback.format_exc(), logging.ERROR, context="CRAWL")
            finally:
                remaining = self.queue.qsize() if hasattr(self.queue, "qsize") else "unknown"
                send_log(f"Crawl completed. Processed {self.pages_crawled} pages. Queue had {remaining} remaining items.", context="CRAWL")
                return self.results

    def run(self) -> None:
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
                    "visited_urls": list(self.visited_urls)
                }
            save_crawl_result(self.start_url, self.result_data)
            send_log(f"Crawl completed: {len(results)} pages found. Total tokens: {self.token_count}", context="CRAWL")
            send_log("Data processing complete. JSON file is ready for download.", context="CRAWL")
        except Exception as e:
            send_log(f"Crawl terminated with error: {e}", logging.ERROR, context="CRAWL")
            with self.result_lock:
                self.result_data = {"pages": []}
        finally:
            self.is_busy = False
            send_log("Crawler has been completely shut down.", context="CRAWL")
            new_loop.close()
            global current_crawler
            current_crawler = None
            process_queue()

    def cancel(self) -> None:
        self.cancel_event.set()
        send_log("Cancellation requested. All crawl tasks will be terminated immediately.", context="CRAWL")

# ================================
# Persistence for Crawl Results and Queue (cập nhật)
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
        try:
            crawl_queue_data = json.load(f)
        except Exception:
            crawl_queue_data = []
else:
    crawl_queue_data = []

def save_crawl_result(url: str, result_data: dict) -> str:
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
    send_log(f"Crawl result saved to {filename}", context="PERSIST")
    return filename

def load_queue() -> List[Dict[str, Any]]:
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        import fcntl
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH)  # Shared lock for reading
                queue_data = json.load(f)
            except json.JSONDecodeError as e:
                send_log(f"Queue file corrupted: {str(e)}. Creating backup and starting with empty queue.", 
                         logging.ERROR, context="QUEUE")
                import shutil
                backup_file = f"{QUEUE_FILE}.corrupted.{int(time.time())}"
                shutil.copy2(QUEUE_FILE, backup_file)
                return []
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        valid_entries = []
        for entry in queue_data:
            if isinstance(entry, dict) and "url" in entry:
                if "submitted_at" not in entry:
                    entry["submitted_at"] = datetime.datetime.now().isoformat()
                valid_entries.append(entry)
        valid_entries.sort(key=lambda x: x.get("submitted_at", ""))
        deduped = {}
        for req in valid_entries:
            url = req.get("url")
            if url and url not in deduped:
                deduped[url] = req
        return list(deduped.values())
    except Exception as e:
        send_log(f"Error reading queue: {str(e)}", logging.ERROR, context="QUEUE")
        return []

def save_queue(queue: List[Dict[str, Any]]) -> None:
    try:
        import fcntl
        temp_file = f"{QUEUE_FILE}.temp"
        with open(temp_file, "w", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                json.dump(queue, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(temp_file, QUEUE_FILE)
        send_log(f"Queue saved with {len(queue)} tasks.", context="QUEUE")
    except Exception as e:
        send_log(f"Error saving queue: {str(e)}", logging.ERROR, context="QUEUE")

def add_to_queue(crawl_request: Dict[str, Any]) -> None:
    queue = load_queue()
    new_url = crawl_request.get("url")
    found = False
    for req in queue:
        if req.get("url") == new_url:
            found = True
            send_log(f"Queue already contains crawl request for URL: {new_url}. Merging duplicate request.", context="QUEUE")
            break
    if not found:
        crawl_request["submitted_at"] = datetime.datetime.now().isoformat()
        queue.append(crawl_request)
        save_queue(queue)
        send_log(f"Added new crawl request to queue for URL: {new_url}. Total queued tasks: {len(queue)}", context="QUEUE")

def process_queue():
    global current_crawler
    queue = load_queue()
    if queue and (current_crawler is None or not current_crawler.is_busy):
        crawl_request = queue.pop(0)
        save_queue(queue)
        send_log(f"Resuming crawl for queued URL: {crawl_request.get('url', 'unknown')}. Remaining tasks in queue: {len(queue)}", context="QUEUE")
        current_crawler = RecursiveCrawlManager(
            crawl_request.get("url"),
            int(crawl_request.get("max_depth", 2)),
            int(crawl_request.get("max_pages", 100)),
            content_selector=crawl_request.get("content_selector", ""),
            sitemap_url=crawl_request.get("sitemap_url", "")
        )
        executor.submit(current_crawler.run)
    else:
        send_log("No tasks in queue to process or crawler is busy.", context="QUEUE")

def queue_worker():
    while True:
        if current_crawler is None or not current_crawler.is_busy:
            process_queue()
        time.sleep(5)

# ================================
# Flask Application and Endpoints (cập nhật một số xử lý lỗi và xác thực đầu vào)
# ================================
app = Flask(__name__)
current_crawler: Optional[RecursiveCrawlManager] = None
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
        send_log("Server is busy; your crawl request has been queued.", context="API")
        return jsonify({"message": "Server is busy; your crawl request has been queued.", "queued": True}), 200

    current_crawler = RecursiveCrawlManager(
        start_url,
        int(data.get("max_depth", 2)),
        int(data.get("max_pages", 100)),
        content_selector=content_selector,
        sitemap_url=sitemap_url
    )
    send_log("Crawl process initiated.", context="API")
    executor.submit(current_crawler.run)
    return jsonify({"message": "Crawl started."})

@app.route('/cancel', methods=['POST'])
def cancel_route():
    global current_crawler
    if current_crawler is not None and current_crawler.is_busy:
        current_crawler.cancel()
        return jsonify({"message": "Crawl cancellation requested. Partial crawl data is available."})
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
    try:
        if current_crawler is None:
            send_log("Download requested but no crawler instance exists", context="API")
            return jsonify({"error": "No crawl has been started yet."}), 404

        if current_crawler.is_busy and not current_crawler.cancel_event.is_set():
            send_log("Download requested but crawl is still in progress.", context="API")
            return jsonify({
                "error": "Crawl is still in progress.",
                "progress": {
                    "pages_crawled": current_crawler.pages_crawled,
                    "max_pages": current_crawler.max_pages
                }
            }), 503

        with current_crawler.result_lock:
            if not hasattr(current_crawler, "result_data") or not current_crawler.result_data.get("pages"):
                send_log("Download requested but no crawl data is available.", context="API")
                return jsonify({"error": "No crawl data available."}), 404

            send_log("Download requested; sending result file.", context="API")
            domain = urlparse(current_crawler.start_url).netloc
            filename = f"{domain.replace('.', '_')}_crawl_result.json"
            json_data = json.dumps(current_crawler.result_data, ensure_ascii=False, indent=2)
            response = Response(json_data, mimetype='application/json')
            response.headers["Content-Disposition"] = f"attachment; filename={filename}"
            return response
    except Exception as e:
        send_log(f"Error during download: {str(e)}", logging.ERROR, context="API")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/crawled-files')
def crawled_files():
    if os.path.exists(CRAWLED_MAPPING_FILE):
        with open(CRAWLED_MAPPING_FILE, "r", encoding="utf-8") as f:
            mapping = json.load(f)
    else:
        mapping = {}
    files_list = []
    for url, info in mapping.items():
        files_list.append({
            "url": url,
            "filename": info.get("filename"),
            "last_updated": info.get("last_updated")
        })
    return jsonify(files_list)

@app.route('/download-file')
def download_file():
    filename = request.args.get("file")
    if not filename:
        return jsonify({"error": "Missing file parameter."}), 400
    filepath = os.path.join(CRAWL_RESULTS_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found."}), 404
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
        temperature = None
        if temps:
            for sensor_name, entries in temps.items():
                if entries:
                    temperature = entries[0].current
                    break
        if temperature is None:
            temperature = "N/A"
        busy = current_crawler.is_busy if current_crawler is not None else False
        queue_len = len(load_queue())
        return jsonify({"cpu": cpu, "ram": ram, "temperature": temperature, "busy": busy, "queue_length": queue_len})
    except Exception as e:
        send_log(f"Error fetching server status: {e}", logging.ERROR, context="STATUS")
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
                "queue_size": current_crawler.queue_size,
                "current_urls": list(current_crawler.currently_crawling) if current_crawler.currently_crawling else [current_crawler.start_url]
            }
        else:
            status_data["crawler"] = {"is_busy": False}
        status_data["queue_length"] = len(load_queue())
        return jsonify(status_data)
    except Exception as e:
        send_log(f"Error fetching detailed status: {e}", logging.ERROR, context="STATUS")
        return jsonify({"error": str(e)}), 500

@app.route('/resume-queue', methods=['POST'])
def resume_queue():
    global current_crawler
    if current_crawler is None or not current_crawler.is_busy:
        process_queue()
        send_log("User triggered resume of queue processing.", context="QUEUE")
        return jsonify({"message": "Queue resumed."}), 200
    else:
        send_log("Resume queue request received: crawler is busy; queue remains active.", logging.WARNING, context="QUEUE")
        return jsonify({"message": "Crawler is busy; queue is active."}), 200

if __name__ == '__main__':
    Thread(target=queue_worker, daemon=True).start()
    if load_queue():
        send_log(f"Server started with {len(load_queue())} queued crawl task(s).", context="SYSTEM")
    process_queue()
    app.run(host='0.0.0.0', port=5007, threaded=True)
