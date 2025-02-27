from gevent import monkey
monkey.patch_all()

import asyncio
import datetime
import json
import hashlib
import os
import time
import logging
from queue import Queue, Empty
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.robotparser import RobotFileParser
import aiohttp
from aiohttp import ClientTimeout, ClientSession, TCPConnector
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, Response, render_template, send_file
import concurrent.futures
import psutil
from threading import Thread, Lock
from typing import Optional, List, Dict, Any, Tuple
import threading
import requests

# Import Crawl4AI
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

# ==========================
# Logging configuration
# ==========================
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
    try:
        log_queue.put_nowait(log_msg)
    except Exception:
        logger.warning("Log queue full, dropping message.")

# ================================
# Directory and persistence config
# ================================
ROBOTS_CACHE_DIR = "./robots_cache"
CRAWL_RESULTS_DIR = "./crawl_results"
CRAWLED_MAPPING_FILE = "crawled_mapping.json"
QUEUE_FILE = "crawl_queue.json"

for directory in [ROBOTS_CACHE_DIR, CRAWL_RESULTS_DIR]:
    os.makedirs(directory, exist_ok=True)

# ====================================
# Robots.txt and Sitemap Functions
# ====================================
def cache_robots_file(domain: str, content: str) -> None:
    filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)

def load_cached_robots(domain: str) -> Optional[str]:
    filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
    if os.path.exists(filename):
        if time.time() - os.path.getmtime(filename) > 86400:
            return None
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
                response.raise_for_status()
                return await response.text()
        except Exception as e:
            send_log(f"Attempt {attempt+1}/{retries} failed for {url}: {e}", logging.WARNING, context="HTTP")
            if attempt < retries - 1:
                await asyncio.sleep(backoff_factor ** attempt)
    return None

async def get_robot_parser(session: ClientSession, domain: str, timeout: ClientTimeout) -> Tuple[Optional[RobotFileParser], List[str], Optional[int]]:
    sitemap_urls: List[str] = []
    robots_text = load_cached_robots(domain)

    if robots_text is None:
        for scheme in ["https", "http"]:
            robots_url = f"{scheme}://{domain}/robots.txt"
            robots_text = await fetch_with_retries(session, robots_url, timeout)
            if robots_text:
                cache_robots_file(domain, robots_text)
                send_log(f"Robots.txt cached from {robots_url}", context="ROBOTS")
                break
        if not robots_text:
            send_log(f"No robots.txt for {domain}, assuming all allowed", context="ROBOTS")
            return None, [], None

    rp = RobotFileParser()
    rp.parse(robots_text.splitlines())
    for line in robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_url = line.split(":", 1)[1].strip()
            if sitemap_url:
                sitemap_urls.append(sitemap_url)
    crawl_delay = get_crawl_delay_from_text(robots_text)
    return rp, sitemap_urls, crawl_delay

async def fetch_sitemap_urls(sitemap_url: str, session: ClientSession, timeout: ClientTimeout, recursive: bool = True) -> List[str]:
    urls: List[str] = []
    try:
        text = await fetch_with_retries(session, sitemap_url, timeout)
        if not text:
            return urls

        if not text.strip().startswith('<?xml'):
            urls = [line.strip() for line in text.strip().splitlines() if line.startswith('http')]
            send_log(f"Found {len(urls)} URLs in plain text sitemap: {sitemap_url}", context="SITEMAP")
            return urls

        import lxml.etree as ET
        root = ET.fromstring(text.encode("utf-8"))
        ns = root.nsmap.get(None, '')
        if root.tag.endswith("sitemapindex"):
            send_log(f"Sitemap index detected: {sitemap_url}", context="SITEMAP")
            sub_sitemap_urls = [elem.text.strip() for elem in root.findall(f"{{{ns}}}sitemap/{{{ns}}}loc") if elem.text]
            if recursive and sub_sitemap_urls:
                sub_tasks = [fetch_sitemap_urls(url, session, timeout, recursive) for url in sub_sitemap_urls]
                sub_results = await asyncio.gather(*sub_tasks, return_exceptions=True)
                for result in sub_results:
                    if not isinstance(result, Exception):
                        urls.extend(result)
        elif root.tag.endswith("urlset"):
            urls = [elem.text.strip() for elem in root.findall(f"{{{ns}}}url/{{{ns}}}loc") if elem.text]
            send_log(f"Found {len(urls)} URLs in sitemap: {sitemap_url}", context="SITEMAP")
    except Exception as e:
        send_log(f"Error processing sitemap {sitemap_url}: {str(e)}", logging.ERROR, context="SITEMAP")
    return urls

# ===============================
# Multithreaded Sitemap Crawler
# ===============================
visited_lock = threading.Lock()

def crawl_url_for_sitemap(url: str, domain: str, visited: set, max_depth: int = 3, current_depth: int = 0) -> set:
    if current_depth > max_depth or url in visited:
        return set()
    
    local_urls = set()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.find_all('a', href=True):
            absolute_url = urljoin(url, link['href'])
            parsed_url = urlparse(absolute_url)
            if (parsed_url.scheme in ['http', 'https'] and 
                parsed_url.netloc == domain and 
                '#' not in absolute_url):
                local_urls.add(absolute_url)
        time.sleep(0.1)
    except requests.RequestException as e:
        send_log(f"Error crawling {url} for sitemap: {e}", logging.WARNING, context="SITEMAP")
    
    with visited_lock:
        visited.add(url)
    return local_urls

def generate_sitemap_multithreaded(start_url: str, max_depth: int = 3, max_workers: int = 10) -> set:
    visited = set()
    domain = urlparse(start_url).netloc
    to_crawl = {start_url}
    current_depth = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        while to_crawl and current_depth <= max_depth:
            send_log(f"Crawling depth {current_depth} with {len(to_crawl)} URLs", context="SITEMAP")
            future_to_url = {executor.submit(crawl_url_for_sitemap, url, domain, visited, max_depth, current_depth): url 
                            for url in to_crawl if url not in visited}
            next_to_crawl = set()
            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    urls = future.result()
                    next_to_crawl.update(urls)
                except Exception as e:
                    send_log(f"Error processing {url}: {e}", logging.ERROR, context="SITEMAP")
            to_crawl = next_to_crawl - visited
            current_depth += 1
    
    send_log(f"Generated sitemap with {len(visited)} URLs using multithreaded crawler", context="SITEMAP")
    return visited

# =======================================
# FSM-Based Recursive Crawl Manager Class
# =======================================
class RecursiveCrawlManager:
    def __init__(self, start_url: str, max_depth: int, max_pages: int, content_selector: str = "", sitemap_url: str = "") -> None:
        self.start_url = urldefrag(start_url)[0]
        self.max_depth = max(1, max_depth)
        self.max_pages = max(1, max_pages)
        self.content_selector = content_selector.strip()
        self.sitemap_url = sitemap_url.strip()
        self.visited_urls = set()
        self.results = []
        self.pages_crawled = 0
        self.page_valid = 0
        self.domain = urlparse(self.start_url).netloc
        self.prefix = self.start_url if self.start_url.endswith("/") else self.start_url + "/"
        self.queue = asyncio.PriorityQueue()
        self.cancel_event = asyncio.Event()
        self.is_busy = False
        self.token_count = 0
        self.queue_size = 0
        self.result_lock = Lock()
        self.currently_crawling = set()
        self.used_generated_sitemap = False
        self._loop = None
        self.initial_urls = set()  # Lưu các URL từ sitemap ban đầu
        send_log(f"Initialized for {self.start_url} (depth={self.max_depth}, pages={self.max_pages})", context="CRAWL")

    async def add_initial_tasks(self, session: ClientSession, timeout: ClientTimeout):
        sitemap_urls = set()

        if self.sitemap_url:
            send_log("Using provided sitemap URL.", context="SITEMAP")
            for sitemap in [s.strip() for s in self.sitemap_url.split(",") if s.strip()]:
                urls = await fetch_sitemap_urls(sitemap, session, timeout)
                sitemap_urls.update(urls)
            if sitemap_urls:
                send_log(f"Found {len(sitemap_urls)} URLs from provided sitemap", context="SITEMAP")
        else:
            rp, robots_sitemaps, _ = await get_robot_parser(session, self.domain, timeout)
            if robots_sitemaps:
                send_log("Sitemap detected from robots.txt.", context="SITEMAP")
                for sitemap in robots_sitemaps:
                    urls = await fetch_sitemap_urls(sitemap, session, timeout)
                    sitemap_urls.update(urls)
                if sitemap_urls:
                    send_log(f"Found {len(sitemap_urls)} URLs from robots.txt", context="SITEMAP")
            if not sitemap_urls:
                send_log("No sitemap found; generating sitemap.", context="SITEMAP")
                sitemap_urls = generate_sitemap_multithreaded(self.start_url, self.max_depth)
                self.used_generated_sitemap = True

        self.initial_urls = sitemap_urls.copy()
        for url in sitemap_urls:
            if url.startswith(self.prefix):
                await self.queue.put((self.max_depth, (url, self.max_depth)))
                self.page_valid += 1

        if self.queue.empty():
            send_log("No valid URLs from sitemap; checking start URL.", context="CRAWL")
            rp, _, _ = await get_robot_parser(session, self.domain, timeout)
            if rp is None or rp.can_fetch("*", self.start_url):
                await self.queue.put((0, (self.start_url, 0)))
                self.page_valid = 1
                self.initial_urls.add(self.start_url)
            else:
                send_log(f"Start URL {self.start_url} blocked by robots.txt.", logging.WARNING, context="CRAWL")
        send_log(f"Queue initialized with {self.page_valid} valid pages from sitemap.", context="CRAWL")

    async def crawl(self) -> list:
        self.is_busy = True
        session_timeout = ClientTimeout(total=15)
        connector = TCPConnector(limit_per_host=2)
        overall_timeout = time.time() + 3600
        domain_limiters = {}

        async with aiohttp.ClientSession(timeout=session_timeout, connector=connector) as session:
            await self.add_initial_tasks(session, session_timeout)
            
            if self.queue.empty():
                send_log("No URLs to crawl.", logging.WARNING, context="CRAWL")
                return self.results

            while (not self.queue.empty() and 
                   self.pages_crawled < self.max_pages and 
                   not self.cancel_event.is_set() and
                   time.time() < overall_timeout):
                try:
                    priority, (url, depth) = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if url in self.visited_urls or not urlparse(url).netloc:
                    self.queue.task_done()
                    continue

                if url != self.start_url and not url.startswith(self.prefix):
                    send_log(f"Skipping out-of-scope URL: {url}", logging.DEBUG, context="CRAWL")
                    self.visited_urls.add(url)
                    self.queue.task_done()
                    continue

                if any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.pdf', '.zip', '.exe']):
                    send_log(f"Skipping binary content URL: {url}", logging.DEBUG, context="CRAWL")
                    self.visited_urls.add(url)
                    self.queue.task_done()
                    continue

                self.visited_urls.add(url)
                self.currently_crawling.add(url)

                domain = urlparse(url).netloc
                rp, _, crawl_delay = await get_robot_parser(session, domain, session_timeout)
                
                if not self.used_generated_sitemap and rp and not rp.can_fetch("*", url):
                    send_log(f"Blocked by robots.txt: {url}", context="CRAWL")
                    self.currently_crawling.discard(url)
                    self.queue.task_done()
                    continue

                delay_time = crawl_delay if crawl_delay else 1
                if domain in domain_limiters:
                    time_since_last = time.time() - domain_limiters[domain]
                    if time_since_last < delay_time:
                        await asyncio.sleep(delay_time - time_since_last)
                        await self.queue.put((priority, (url, depth)))
                        self.currently_crawling.discard(url)
                        self.visited_urls.discard(url)
                        self.queue.task_done()
                        continue
                domain_limiters[domain] = time.time()

                browser_cfg = BrowserConfig(headless=True, verbose=False)
                run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
                if self.content_selector:
                    run_cfg.css_selector = self.content_selector

                try:
                    async with AsyncWebCrawler(config=browser_cfg) as crawler:
                        result = await crawler.arun(url=url, config=run_cfg)
                except Exception as e:
                    send_log(f"Error crawling {url}: {e}", logging.ERROR, context="CRAWL")
                    self.currently_crawling.discard(url)
                    self.queue.task_done()
                    continue

                if result and result.success:
                    title = result.markdown.split("\n")[0] if result.markdown else ""
                    tokens = len(result.markdown.split()) if result.markdown else 0
                    self.token_count += tokens
                    self.results.append({
                        "url": url,
                        "title": title,
                        "markdown": result.markdown,
                        "html_hash": hashlib.md5(result.html.encode("utf-8")).hexdigest(),
                        "crawl_time": datetime.datetime.now().isoformat()
                    })
                    self.pages_crawled += 1
                    send_log(f"Completed URL: {url}", context="CRAWL")
                    
                    if hasattr(result, "final_url") and result.final_url and result.final_url != url:
                        self.visited_urls.add(result.final_url)

                    # Extract and enqueue new URLs
                    soup = BeautifulSoup(result.html, "html.parser")
                    for a in soup.find_all('a', href=True):
                        next_url = urldefrag(urljoin(url, a["href"]))[0]
                        if (next_url not in self.visited_urls and 
                            next_url not in self.currently_crawling and 
                            next_url.startswith(self.prefix) and 
                            next_url not in [item[1][0] for item in list(self.queue._queue)]):
                            new_priority = self.max_depth if next_url in self.initial_urls else self.max_depth - 1
                            await self.queue.put((new_priority, (next_url, min(depth + 1, self.max_depth))))
                            if next_url not in self.initial_urls:
                                send_log(f"Added new discovered URL: {next_url} (depth={min(depth + 1, self.max_depth)})", context="CRAWL")
                            self.page_valid += 1

                else:
                    send_log(f"Failed to crawl: {url}", logging.WARNING, context="CRAWL")

                self.currently_crawling.discard(url)
                self.queue.task_done()
                self.queue_size = self.queue.qsize()

            remaining = self.queue.qsize()
            send_log(f"Crawl completed. Processed {self.pages_crawled}/{self.max_pages} pages. Queue remaining: {remaining}", context="CRAWL")
            return self.results

    def run(self) -> None:
        if self.is_busy:
            send_log("Crawler already running.", logging.WARNING, context="CRAWL")
            return
        
        self.is_busy = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            results = self._loop.run_until_complete(self.crawl())
            with self.result_lock:
                self.result_data = {
                    "crawl_date": datetime.datetime.now().isoformat(),
                    "source": self.domain,
                    "pages": results,
                    "total_tokens": self.token_count,
                    "pages_crawled": self.pages_crawled,
                    "page_valid": self.page_valid,
                    "visited_urls": list(self.visited_urls)
                }
            save_crawl_result(self.start_url, self.result_data)
            send_log(f"Crawl completed: {len(results)} pages, {self.token_count} tokens, {self.page_valid} valid pages", context="CRAWL")
        except Exception as e:
            send_log(f"Crawl failed: {e}", logging.ERROR, context="CRAWL")
            with self.result_lock:
                self.result_data = {"pages": [], "page_valid": self.page_valid}
        finally:
            self.is_busy = False
            send_log("Crawler shut down.", context="CRAWL")
            if self._loop:
                self._loop.close()
                self._loop = None
            global current_crawler
            current_crawler = None
            process_queue()

    def cancel(self) -> None:
        if self.is_busy:
            self.cancel_event.set()
            send_log("Cancellation requested.", context="CRAWL")

# =======================================
# Persistence for Crawl Results and Queue
# =======================================
def save_crawl_result(url: str, result_data: dict) -> str:
    domain = urlparse(url).netloc
    file_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    filename = f"{domain.replace('.', '_')}_{file_hash}.json"
    filepath = os.path.join(CRAWL_RESULTS_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result_data, f, ensure_ascii=False, indent=2)
        with Lock():
            crawled_mapping[url] = {"filename": filename, "last_updated": datetime.datetime.now().isoformat()}
            with open(CRAWLED_MAPPING_FILE, "w", encoding="utf-8") as f:
                json.dump(crawled_mapping, f, ensure_ascii=False, indent=2)
        send_log(f"Crawl result saved to {filename}", context="PERSIST")
        return filename
    except Exception as e:
        send_log(f"Failed to save crawl result: {e}", logging.ERROR, context="PERSIST")
        return ""

def load_queue() -> List[Dict[str, Any]]:
    try:
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return []
    except Exception as e:
        send_log(f"Error loading queue: {e}", logging.ERROR, context="QUEUE")
        return []

def load_crawled_mapping() -> Dict[str, Any]:
    try:
        if os.path.exists(CRAWLED_MAPPING_FILE):
            with open(CRAWLED_MAPPING_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        send_log(f"Error loading crawled mapping: {e}", logging.ERROR, context="PERSIST")
        return {}

def save_queue(queue: List[Dict[str, Any]]) -> None:
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        send_log(f"Queue saved with {len(queue)} tasks.", context="QUEUE")
    except Exception as e:
        send_log(f"Error saving queue: {e}", logging.ERROR, context="QUEUE")

def add_to_queue(crawl_request: Dict[str, Any]) -> None:
    with Lock():
        queue_data = load_queue()
        new_url = crawl_request.get("url")
        if not any(req.get("url") == new_url for req in queue_data):
            crawl_request["submitted_at"] = datetime.datetime.now().isoformat()
            queue_data.append(crawl_request)
            save_queue(queue_data)
            send_log(f"Added crawl request for {new_url}", context="QUEUE")

def process_queue():
    global current_crawler
    with Lock():
        queue_data = load_queue()
        if queue_data and (current_crawler is None or not current_crawler.is_busy):
            crawl_request = queue_data.pop(0)
            save_queue(queue_data)
            send_log(f"Processing queued crawl: {crawl_request.get('url')}", context="QUEUE")
            current_crawler = RecursiveCrawlManager(
                crawl_request.get("url"),
                int(crawl_request.get("max_depth", 2)),
                int(crawl_request.get("max_pages", 100)),
                crawl_request.get("content_selector", ""),
                crawl_request.get("sitemap_url", "")
            )
            executor.submit(current_crawler.run)

def queue_worker():
    while True:
        process_queue()
        time.sleep(5)

# =====================
# Flask Application
# =====================
app = Flask(__name__)
current_crawler: Optional[RecursiveCrawlManager] = None
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
crawled_mapping = load_crawled_mapping()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start-crawl', methods=['POST'])
def start_crawl_route():
    global current_crawler
    data = request.get_json()
    if not isinstance(data, dict) or "url" not in data:
        return jsonify({"error": "Invalid JSON or missing 'url' parameter"}), 400

    start_url = data.get("url")
    force = data.get("force", False)
    content_selector = data.get("content_selector", "").strip()
    sitemap_url = data.get("sitemap_url", "").strip()

    if start_url in crawled_mapping and not force:
        return jsonify({
            "message": "This URL has been crawled before.",
            "cached_data": crawled_mapping[start_url]
        }), 200

    if current_crawler and current_crawler.is_busy:
        add_to_queue(data)
        return jsonify({
            "message": "Server is busy; request queued.",
            "queue_length": len(load_queue())
        }), 202

    current_crawler = RecursiveCrawlManager(
        start_url,
        int(data.get("max_depth", 2)),
        int(data.get("max_pages", 100)),
        content_selector,
        sitemap_url
    )
    send_log("Crawl process initiated.", context="API")
    executor.submit(current_crawler.run)
    return jsonify({
        "message": "Crawl started.",
        "start_url": start_url,
        "max_depth": current_crawler.max_depth,
        "max_pages": current_crawler.max_pages
    }), 200

@app.route('/cancel', methods=['POST'])
def cancel_route():
    global current_crawler
    if current_crawler and current_crawler.is_busy:
        current_crawler.cancel()
        return jsonify({
            "message": "Crawl cancellation requested.",
            "pages_crawled": current_crawler.pages_crawled,
            "page_valid": current_crawler.page_valid
        }), 200
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
    if not current_crawler:
        return jsonify({"error": "No crawl initiated."}), 404
    if current_crawler.is_busy and not current_crawler.cancel_event.is_set():
        return jsonify({
            "error": "Crawl in progress.",
            "progress": {
                "pages_crawled": current_crawler.pages_crawled,
                "page_valid": current_crawler.page_valid,
                "max_pages": current_crawler.max_pages
            }
        }), 503
    
    with current_crawler.result_lock:
        if not hasattr(current_crawler, "result_data") or not current_crawler.result_data.get("pages"):
            return jsonify({"error": "No crawl data available.", "page_valid": current_crawler.page_valid}), 404
        domain = urlparse(current_crawler.start_url).netloc
        filename = f"{domain.replace('.', '_')}_crawl_result.json"
        json_data = json.dumps(current_crawler.result_data, ensure_ascii=False, indent=2)
        response = Response(json_data, mimetype='application/json')
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response

@app.route('/crawled-files')
def crawled_files():
    with Lock():
        files = [{"url": url, "filename": info["filename"], "last_updated": info["last_updated"]} 
                 for url, info in crawled_mapping.items()]
    return jsonify({
        "message": "List of crawled files.",
        "files": files,
        "total": len(files)
    }), 200

@app.route('/download-file')
def download_file():
    filename = request.args.get("file")
    if not filename:
        return jsonify({"error": "Missing 'file' parameter."}), 400
    filepath = os.path.join(CRAWL_RESULTS_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": f"File '{filename}' not found."}), 404
    return send_file(filepath, as_attachment=True, download_name=filename)

@app.route('/queue')
def queue_status():
    queue_data = load_queue()
    return jsonify({
        "message": "Current crawl queue status.",
        "queue": queue_data,
        "queue_length": len(queue_data)
    }), 200

@app.route('/status')
def status_endpoint():
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory().percent
    temps = psutil.sensors_temperatures()
    temperature = "N/A"
    if temps:
        for entries in temps.values():
            if entries:
                temperature = entries[0].current
                break
    busy = current_crawler.is_busy if current_crawler else False
    crawler_status = {
        "is_busy": busy,
        "pages_crawled": current_crawler.pages_crawled if current_crawler else 0,
        "page_valid": current_crawler.page_valid if current_crawler else 0,
        "total_tokens": current_crawler.token_count if current_crawler else 0
    } if current_crawler else {"is_busy": False}
    return jsonify({
        "message": "Server status.",
        "system": {"cpu": cpu, "ram": ram, "temperature": temperature},
        "crawler": crawler_status,
        "queue_length": len(load_queue())
    }), 200

@app.route('/detailed-status')
def detailed_status():
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        temps = psutil.sensors_temperatures()
        temperature = "N/A"
        if temps:
            for entries in temps.values():
                if entries:
                    temperature = entries[0].current
                    break
        status_data = {
            "server": {"cpu": cpu, "ram": ram, "temperature": temperature},
            "crawler": {
                "is_busy": current_crawler.is_busy,
                "total_tokens": current_crawler.token_count,
                "pages_crawled": current_crawler.pages_crawled,
                "visited_urls_count": len(current_crawler.visited_urls),
                "queue_size": current_crawler.queue_size,
                "current_urls": list(current_crawler.currently_crawling) if current_crawler.currently_crawling else [current_crawler.start_url]
            } if current_crawler else {"is_busy": False},
            "queue_length": len(load_queue())
        }
        return jsonify(status_data), 200
    except Exception as e:
        send_log(f"Error in detailed status: {e}", logging.ERROR, context="STATUS")
        return jsonify({"error": str(e)}), 500

@app.route('/resume-queue', methods=['POST'])
def resume_queue():
    global current_crawler
    if current_crawler and current_crawler.is_busy:
        send_log("Crawler busy; queue remains active.", logging.WARNING, context="QUEUE")
        return jsonify({"message": "Crawler is busy; queue is active."}), 200
    process_queue()
    send_log("Queue resumed by user.", context="QUEUE")
    return jsonify({"message": "Queue resumed."}), 200

if __name__ == '__main__':
    Thread(target=queue_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=5006, threaded=True)
