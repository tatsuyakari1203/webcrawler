from gevent import monkey
monkey.patch_all()

import asyncio
import datetime
import json
import hashlib
import os
import time
import logging
import random
from queue import Queue, Empty
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.robotparser import RobotFileParser
import aiohttp
from aiohttp import ClientTimeout, ClientSession, TCPConnector
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, Response, render_template, send_file
import concurrent.futures
import psutil
from threading import Thread, Lock, Event
from typing import Optional, List, Dict, Any, Tuple, Set
import threading
import requests
from dataclasses import dataclass, field
from enum import Enum, auto

# Import Crawl4AI
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

# ==========================
# Improved Logging Configuration
# ==========================
# Create a custom formatter for better readability
class ColoredFormatter(logging.Formatter):
    """Formatter adding colors to levelname"""
    grey = "\x1b[38;20m"
    yellow = "\x1b[33;20m"
    green = "\x1b[32;20m"
    blue = "\x1b[34;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    
    FORMATS = {
        logging.DEBUG: grey + '%(asctime)s [%(levelname)s] %(message)s' + reset,
        logging.INFO: blue + '%(asctime)s [%(levelname)s] %(message)s' + reset,
        logging.WARNING: yellow + '%(asctime)s [%(levelname)s] %(message)s' + reset,
        logging.ERROR: red + '%(asctime)s [%(levelname)s] %(message)s' + reset,
        logging.CRITICAL: bold_red + '%(asctime)s [%(levelname)s] %(message)s' + reset
    }
    
    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt='%Y-%m-%d %H:%M:%S')
        return formatter.format(record)

# Set up logger
logger = logging.getLogger("Crawler")
logger.setLevel(logging.INFO)
logger.handlers = []  # Clear any existing handlers

# Console handler with colored output
console_handler = logging.StreamHandler()
console_handler.setFormatter(ColoredFormatter())
logger.addHandler(console_handler)

# Create a file handler for persistence
file_handler = logging.FileHandler("crawler.log", mode="a")
file_formatter = logging.Formatter('[%(asctime)s] %(levelname)s [%(context)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Queue for streaming logs to the frontend
log_queue = Queue(maxsize=2000)  # Increased size to avoid dropping important messages

# Log context categories for better organization
LOG_CONTEXT = {
    "INIT": "Initialization",
    "CRAWL": "Crawler",
    "HTTP": "HTTP Request",
    "SITEMAP": "Sitemap",
    "ROBOTS": "Robots.txt",
    "QUEUE": "Queue Manager",
    "DISCOVERY": "URL Discovery",
    "PERSIST": "Persistence",
    "STATUS": "Status Update",
    "API": "API Endpoint"
}

class LogFilter:
    """Filter to add context to log record"""
    def __init__(self, context):
        self.context = context
        
    def filter(self, record):
        record.context = self.context
        return True

def send_log(message: str, level: int = logging.INFO, context: Optional[str] = None) -> None:
    """Improved logging function that handles context better and avoids duplicates"""
    if not context:
        context = "GENERAL"
    
    # Add record to logger with filter for context
    log_filter = LogFilter(context)
    record = logging.LogRecord(
        name=logger.name,
        level=level,
        pathname="",
        lineno=0,
        msg=message,
        args=(),
        exc_info=None
    )
    record.context = context
    
    if level == logging.DEBUG and logger.level > logging.DEBUG:
        # Skip DEBUG messages if not in debug mode
        return
        
    # Format timestamp separately for queue (since we can't pass the LogRecord directly)
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Get level name
    level_name = logging.getLevelName(level)
    
    # Format full message for queue
    if context:
        log_msg = f"[{timestamp}] {level_name} [{context}] {message}"
    else:
        log_msg = f"[{timestamp}] {level_name} {message}"
    
    # Pass to logger
    logger.handle(record)
    
    # Add to queue for frontend
    try:
        # Don't send DEBUG messages to frontend
        if level > logging.DEBUG:
            log_queue.put_nowait(log_msg)
    except Exception:
        logger.warning("Log queue full, dropping message.", extra={"context": "LOGGING"})

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
# Crawler State Management
# ====================================
class CrawlerState(Enum):
    """Enumeration of crawler states for better state management"""
    IDLE = auto()
    INITIALIZING = auto()
    CRAWLING = auto()
    CANCELLING = auto()
    COMPLETED = auto()
    ERROR = auto()

@dataclass
class CrawlRequest:
    """Data structure for crawl requests"""
    url: str
    max_depth: int = 2
    max_pages: int = 100
    content_selector: str = ""
    sitemap_url: str = ""
    force: bool = False
    submitted_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())

@dataclass
class CrawlStatus:
    """Data structure for tracking crawl status"""
    state: CrawlerState = CrawlerState.IDLE
    pages_crawled: int = 0
    pages_valid: int = 0
    queue_size: int = 0
    token_count: int = 0
    error_count: int = 0
    start_time: Optional[datetime.datetime] = None
    end_time: Optional[datetime.datetime] = None
    
    def to_dict(self):
        return {
            "state": self.state.name,
            "pages_crawled": self.pages_crawled,
            "pages_valid": self.pages_valid,
            "queue_size": self.queue_size,
            "token_count": self.token_count,
            "error_count": self.error_count,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration": (self.end_time - self.start_time).total_seconds() if self.start_time and self.end_time else None
        }

# ====================================
# Domain Rate Limiter
# ====================================
class AdaptiveRateLimiter:
    """Class for adaptive rate limiting per domain"""
    def __init__(self):
        self.domain_info = {}
        self.lock = Lock()
    
    def get_delay(self, domain: str, robots_delay: Optional[int] = None) -> float:
        with self.lock:
            if domain not in self.domain_info:
                # Start with the robots.txt delay if available, otherwise use a default
                base_delay = robots_delay if robots_delay is not None else 1.0
                self.domain_info[domain] = {
                    "last_access": 0,
                    "base_delay": base_delay,
                    "current_delay": base_delay,
                    "success_count": 0,
                    "failure_count": 0
                }
            
            info = self.domain_info[domain]
            time_since_last = time.time() - info["last_access"]
            
            # Return the current delay or 0 if enough time has passed
            return max(0, info["current_delay"] - time_since_last)
    
    def update_access(self, domain: str, success: bool) -> None:
        with self.lock:
            if domain in self.domain_info:
                info = self.domain_info[domain]
                info["last_access"] = time.time()
                
                # Adaptive logic - decrease delay on success, increase on failure
                if success:
                    info["success_count"] += 1
                    if info["success_count"] >= 5:
                        info["current_delay"] = max(0.5, info["current_delay"] * 0.9)
                        info["success_count"] = 0
                else:
                    info["failure_count"] += 1
                    if info["failure_count"] >= 2:
                        info["current_delay"] = min(5.0, info["current_delay"] * 1.5)
                        info["failure_count"] = 0

# ====================================
# Robots.txt and Sitemap Functions - Enhanced
# ====================================
class RobotsManager:
    """Class for managing robots.txt parsing and caching"""
    def __init__(self):
        self.cache = {}
        self.lock = Lock()
    
    def cache_robots_file(self, domain: str, content: str) -> None:
        filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        with self.lock:
            self.cache[domain] = {
                "parser": self._create_parser(content),
                "text": content,
                "timestamp": time.time()
            }
    
    def _create_parser(self, content: str) -> RobotFileParser:
        parser = RobotFileParser()
        parser.parse(content.splitlines())
        return parser
    
    def load_cached_robots(self, domain: str) -> Optional[Dict[str, Any]]:
        # First check memory cache
        with self.lock:
            if domain in self.cache:
                cache_entry = self.cache[domain]
                if time.time() - cache_entry["timestamp"] <= 86400:  # 24 hours
                    return cache_entry
                else:
                    # Expired, remove from memory cache
                    del self.cache[domain]
        
        # Then check disk cache
        filename = os.path.join(ROBOTS_CACHE_DIR, f"{domain}.txt")
        if os.path.exists(filename):
            if time.time() - os.path.getmtime(filename) <= 86400:  # 24 hours
                with open(filename, "r", encoding="utf-8") as f:
                    content = f.read()
                cache_entry = {
                    "parser": self._create_parser(content),
                    "text": content,
                    "timestamp": os.path.getmtime(filename)
                }
                with self.lock:
                    self.cache[domain] = cache_entry
                return cache_entry
        return None
    
    def get_crawl_delay(self, robots_text: str) -> Optional[int]:
        for line in robots_text.splitlines():
            if line.lower().startswith("crawl-delay:"):
                try:
                    return int(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
        return None
    
    def get_sitemaps(self, robots_text: str) -> List[str]:
        sitemaps = []
        for line in robots_text.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip()
                if sitemap_url:
                    sitemaps.append(sitemap_url)
        return sitemaps
    
    async def get_robot_parser(self, session: ClientSession, domain: str, timeout: ClientTimeout) -> Tuple[Optional[RobotFileParser], List[str], Optional[int]]:
        cached = self.load_cached_robots(domain)
        if cached:
            send_log(f"Using cached robots.txt for {domain}", logging.DEBUG, "ROBOTS")
            return cached["parser"], self.get_sitemaps(cached["text"]), self.get_crawl_delay(cached["text"])
        
        # If not cached, fetch from server
        robots_text = ""
        for scheme in ["https", "http"]:
            robots_url = f"{scheme}://{domain}/robots.txt"
            try:
                send_log(f"Fetching robots.txt from {robots_url}", logging.DEBUG, "ROBOTS")
                async with session.get(robots_url, timeout=timeout) as response:
                    if response.status == 200:
                        robots_text = await response.text()
                        self.cache_robots_file(domain, robots_text)
                        send_log(f"Robots.txt cached from {robots_url}", context="ROBOTS")
                        break
            except Exception as e:
                send_log(f"Error fetching {robots_url}: {e}", logging.WARNING, context="ROBOTS")
        
        if not robots_text:
            send_log(f"No robots.txt for {domain}, assuming all allowed", context="ROBOTS")
            # Cache an empty file
            self.cache_robots_file(domain, "")
            robots_text = ""
        
        parser = RobotFileParser()
        parser.parse(robots_text.splitlines())
        return parser, self.get_sitemaps(robots_text), self.get_crawl_delay(robots_text)

# HTTP Fetching with improved error handling and retries
async def fetch_with_retries(session: ClientSession, url: str, timeout: ClientTimeout, 
                             retries: int = 3, backoff_factor: float = 1.5) -> Tuple[Optional[str], bool]:
    for attempt in range(retries):
        try:
            send_log(f"Fetching URL: {url} (attempt {attempt+1}/{retries})", logging.DEBUG, context="HTTP")
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    return await response.text(), True
                elif response.status in [403, 404, 410]:  # Permanent errors
                    send_log(f"Permanent error {response.status} for {url}", logging.WARNING, context="HTTP")
                    return None, False
                else:
                    send_log(f"HTTP error {response.status} for {url}", logging.WARNING, context="HTTP")
                    # Temporary error, will retry
        except asyncio.TimeoutError:
            send_log(f"Timeout on attempt {attempt+1}/{retries} for {url}", logging.WARNING, context="HTTP")
        except aiohttp.ClientError as e:
            send_log(f"Client error on attempt {attempt+1}/{retries} for {url}: {e}", logging.WARNING, context="HTTP")
        except Exception as e:
            send_log(f"Unexpected error on attempt {attempt+1}/{retries} for {url}: {e}", logging.ERROR, context="HTTP")
        
        if attempt < retries - 1:
            # Exponential backoff with jitter
            delay = backoff_factor ** attempt + (0.1 * random.random())
            await asyncio.sleep(delay)
    
    return None, False

# Sitemap processing with improved XML handling
async def fetch_sitemap_urls(sitemap_url: str, session: ClientSession, timeout: ClientTimeout, recursive: bool = True) -> List[str]:
    urls: List[str] = []
    send_log(f"Processing sitemap: {sitemap_url}", context="SITEMAP")
    try:
        text, success = await fetch_with_retries(session, sitemap_url, timeout)
        if not text or not success:
            send_log(f"Failed to fetch sitemap from {sitemap_url}", logging.WARNING, context="SITEMAP")
            return urls

        # Check if it's a text sitemap
        if not text.strip().startswith('<?xml'):
            urls = [line.strip() for line in text.strip().splitlines() if line.strip().startswith('http')]
            send_log(f"Found {len(urls)} URLs in plain text sitemap: {sitemap_url}", context="SITEMAP")
            return urls

        try:
            import lxml.etree as ET
            root = ET.fromstring(text.encode("utf-8"))
        except Exception as e:
            send_log(f"XML parsing error for {sitemap_url}: {e}", logging.ERROR, context="SITEMAP")
            return urls

        # Get the namespace if present
        nsmap = root.nsmap or {}
        ns = nsmap.get(None, '')
        ns_prefix = f"{{{ns}}}" if ns else ""
        
        # Process sitemap index or urlset
        if root.tag.endswith("sitemapindex"):
            send_log(f"Sitemap index detected: {sitemap_url}", context="SITEMAP")
            sub_sitemap_urls = []
            for sitemap_elem in root.findall(f"{ns_prefix}sitemap") or []:
                loc_elem = sitemap_elem.find(f"{ns_prefix}loc")
                if loc_elem is not None and loc_elem.text:
                    sub_sitemap_urls.append(loc_elem.text.strip())
            
            send_log(f"Found {len(sub_sitemap_urls)} sub-sitemaps in index", logging.DEBUG, context="SITEMAP")
            
            if recursive and sub_sitemap_urls:
                # Process sub-sitemaps concurrently with task limiting
                tasks = []
                for url in sub_sitemap_urls:
                    if len(tasks) >= 5:  # Limit concurrent tasks
                        # Wait for some tasks to complete before adding more
                        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        tasks = list(pending)
                        for completed_task in done:
                            try:
                                result = await completed_task
                                urls.extend(result)
                            except Exception as e:
                                send_log(f"Error in sub-sitemap task: {e}", logging.ERROR, context="SITEMAP")
                    
                    tasks.append(asyncio.create_task(fetch_sitemap_urls(url, session, timeout, recursive)))
                
                # Wait for remaining tasks
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if not isinstance(result, Exception):
                            urls.extend(result)
        
        elif root.tag.endswith("urlset"):
            for url_elem in root.findall(f"{ns_prefix}url") or []:
                loc_elem = url_elem.find(f"{ns_prefix}loc")
                if loc_elem is not None and loc_elem.text:
                    urls.append(loc_elem.text.strip())
            send_log(f"Found {len(urls)} URLs in sitemap: {sitemap_url}", context="SITEMAP")
    
    except Exception as e:
        send_log(f"Error processing sitemap {sitemap_url}: {str(e)}", logging.ERROR, context="SITEMAP")
    
    return urls

# Multithreaded sitemap discovery with improved thread safety
def crawl_url_for_sitemap(url: str, domain: str, visited: Set[str], max_depth: int = 3, current_depth: int = 0) -> Set[str]:
    if current_depth > max_depth:
        return set()
    
    with visited_lock:
        if url in visited:
            return set()
        visited.add(url)
    
    local_urls = set()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; WebCrawler/1.0)'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.find_all('a', href=True):
            absolute_url = urljoin(url, link['href'])
            parsed_url = urlparse(absolute_url)
            if (parsed_url.scheme in ['http', 'https'] and 
                parsed_url.netloc == domain and 
                '#' not in absolute_url):
                local_urls.add(urldefrag(absolute_url)[0])  # Remove fragments
        time.sleep(0.1)  # Politeness delay
    except requests.RequestException as e:
        send_log(f"Error crawling {url} for sitemap: {e}", logging.WARNING, context="SITEMAP")
    
    return local_urls

def generate_sitemap_multithreaded(start_url: str, max_depth: int = 3, max_workers: int = 10) -> Set[str]:
    visited = set()
    visited_lock = Lock()  # Dedicated lock for this function
    domain = urlparse(start_url).netloc
    to_crawl = {start_url}
    current_depth = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        while to_crawl and current_depth <= max_depth:
            send_log(f"Sitemap generation: Crawling depth {current_depth} with {len(to_crawl)} URLs", context="SITEMAP")
            
            # Check for URLs already in visited before submitting tasks
            new_to_crawl = set()
            with visited_lock:
                for url in to_crawl:
                    if url not in visited:
                        new_to_crawl.add(url)
            
            # Submit tasks for new URLs
            future_to_url = {
                executor.submit(crawl_url_for_sitemap, url, domain, visited, max_depth, current_depth): url 
                for url in new_to_crawl
            }
            
            next_to_crawl = set()
            for future in concurrent.futures.as_completed(future_to_url):
                try:
                    urls = future.result()
                    next_to_crawl.update(urls)
                except Exception as e:
                    url = future_to_url[future]
                    send_log(f"Error processing {url}: {e}", logging.ERROR, context="SITEMAP")
            
            # Filter out already visited URLs
            with visited_lock:
                to_crawl = next_to_crawl - visited
            
            current_depth += 1
    
    send_log(f"Generated sitemap with {len(visited)} URLs using multithreaded crawler", context="SITEMAP")
    return visited

# =======================================
# Improved Recursive Crawl Manager Class
# =======================================
class RecursiveCrawlManager:
    def __init__(self, 
                 request: CrawlRequest,
                 robots_manager: RobotsManager,
                 rate_limiter: AdaptiveRateLimiter) -> None:
        
        self.request = request
        self.robots_manager = robots_manager
        self.rate_limiter = rate_limiter
        
        # Parse the URL
        self.start_url = urldefrag(request.url)[0]
        self.domain = urlparse(self.start_url).netloc
        self.prefix = self.start_url if self.start_url.endswith("/") else self.start_url + "/"
        
        # Crawl state
        self.status = CrawlStatus(state=CrawlerState.IDLE)
        self.visited_urls = set()
        self.url_in_queue = set()  # Track URLs in queue for quick checking
        self.currently_crawling = set()
        self.initial_urls = set()
        self.results = []
        self.used_generated_sitemap = False
        
        # Discovery metrics
        self.discovered_urls_count = 0
        self.sitemap_urls_count = 0
        self.duplicates_avoided = 0
        
        # Progress tracking for better logging
        self.last_progress_log = 0
        self.progress_log_interval = 10  # Log progress every 10 pages
        
        # Threading and asyncio
        self.result_lock = Lock()
        self.queue = None  # Will be initialized in crawl()
        self.cancel_event = Event()
        
        # Browser configuration
        self.browser_cfg = BrowserConfig(headless=True, verbose=False)
        
        send_log(f"Crawler initialized for {self.start_url}", context="INIT")
        send_log(f"Configuration: max_depth={request.max_depth}, max_pages={request.max_pages}", context="INIT")
        if request.content_selector:
            send_log(f"Content selector: {request.content_selector}", context="INIT")
        if request.sitemap_url:
            send_log(f"Custom sitemap URL: {request.sitemap_url}", context="INIT")
    
    async def add_initial_tasks(self):
        """Initialize the crawl queue with URLs from sitemaps or start URL"""
        self.queue = asyncio.PriorityQueue()
        sitemap_urls = set()
        filtered_urls = set()
        
        # Create aiohttp session
        timeout = ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Try to use provided sitemap URL first
            if self.request.sitemap_url:
                send_log(f"Using provided sitemap URL: {self.request.sitemap_url}", context="SITEMAP")
                for sitemap in [s.strip() for s in self.request.sitemap_url.split(",") if s.strip()]:
                    urls = await fetch_sitemap_urls(sitemap, session, timeout)
                    sitemap_urls.update(urls)
                if sitemap_urls:
                    send_log(f"Found {len(sitemap_urls)} URLs from provided sitemap", context="SITEMAP")
            
            # If no sitemap provided or found, check robots.txt
            if not sitemap_urls:
                rp, robots_sitemaps, _ = await self.robots_manager.get_robot_parser(session, self.domain, timeout)
                if robots_sitemaps:
                    send_log(f"Found {len(robots_sitemaps)} sitemaps in robots.txt", context="SITEMAP")
                    for sitemap in robots_sitemaps:
                        urls = await fetch_sitemap_urls(sitemap, session, timeout)
                        sitemap_urls.update(urls)
                    if sitemap_urls:
                        send_log(f"Found {len(sitemap_urls)} URLs from robots.txt sitemaps", context="SITEMAP")
                
                # If no sitemaps found, generate our own
                if not sitemap_urls:
                    send_log("No sitemap found in robots.txt; generating sitemap", context="SITEMAP")
                    sitemap_urls = generate_sitemap_multithreaded(self.start_url, self.request.max_depth)
                    self.used_generated_sitemap = True

        # Add sitemap URLs to initial set and queue
        self.initial_urls = sitemap_urls.copy()
        self.sitemap_urls_count = len(sitemap_urls)
        priority_depth = self.request.max_depth
        
        # Filter URLs by prefix and validity
        for url in sitemap_urls:
            if url.startswith(self.prefix) and not self._should_skip_url(url):
                filtered_urls.add(url)
                await self.queue.put((priority_depth, (url, priority_depth)))
                self.url_in_queue.add(url)  # Track URL in queue
                self.status.pages_valid += 1
        
        filtered_count = len(filtered_urls)
        if filtered_count > 0:
            send_log(f"Added {filtered_count} valid URLs from sitemap to crawl queue", context="SITEMAP")
            # Log sample URLs for debugging (up to 5)
            sample_urls = list(filtered_urls)[:5]
            for i, url in enumerate(sample_urls):
                send_log(f"Sample URL {i+1}: {url}", logging.DEBUG, context="SITEMAP")

        # Always add start URL if not already added
        if self.start_url not in filtered_urls and not self.queue.empty():
            if not self._should_skip_url(self.start_url):
                await self.queue.put((priority_depth, (self.start_url, 0)))
                self.url_in_queue.add(self.start_url)
                self.status.pages_valid += 1
                self.initial_urls.add(self.start_url)
                send_log(f"Added start URL to queue: {self.start_url}", context="CRAWL")

        # If queue is empty, check robots.txt and add start URL
        if self.queue.empty():
            send_log("No valid URLs from sitemap; checking start URL", context="CRAWL")
            
            # Check if start URL is allowed by robots.txt
            async with aiohttp.ClientSession(timeout=timeout) as session:
                rp, _, _ = await self.robots_manager.get_robot_parser(session, self.domain, timeout)
                if rp is None or rp.can_fetch("*", self.start_url):
                    await self.queue.put((0, (self.start_url, 0)))
                    self.url_in_queue.add(self.start_url)
                    self.status.pages_valid = 1
                    self.initial_urls.add(self.start_url)
                    send_log(f"Added start URL to queue: {self.start_url}", context="CRAWL")
                else:
                    send_log(f"Start URL {self.start_url} blocked by robots.txt", logging.WARNING, context="CRAWL")
        
        send_log(f"Queue initialized with {self.status.pages_valid} valid pages", context="CRAWL")
    
    def _should_skip_url(self, url: str) -> bool:
        """Check if a URL should be skipped based on various criteria"""
        # Skip already visited URLs
        if url in self.visited_urls:
            return True
        
        # Skip URLs without proper scheme
        parsed = urlparse(url)
        if not parsed.netloc:
            return True
        
        # Skip out-of-scope URLs
        if url != self.start_url and not url.startswith(self.prefix):
            return True
        
        # Skip binary/media file extensions
        if any(url.lower().endswith(ext) for ext in [
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp',  # Images
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',  # Documents
            '.zip', '.rar', '.tar', '.gz', '.7z',  # Archives
            '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv',  # Media
            '.exe', '.dll', '.bin', '.dat'  # Binaries
        ]):
            return True
        
        return False
    
    async def _process_page(self, url: str, depth: int, session: ClientSession) -> Optional[Dict[str, Any]]:
        """Process a single page and return results if successful"""
        domain = urlparse(url).netloc
        
        # Check robots.txt
        rp, _, crawl_delay = await self.robots_manager.get_robot_parser(session, domain, ClientTimeout(total=10))
        if not self.used_generated_sitemap and rp and not rp.can_fetch("*", url):
            send_log(f"URL blocked by robots.txt: {url}", context="CRAWL")
            return None
        
        # Apply rate limiting
        delay = self.rate_limiter.get_delay(domain, crawl_delay)
        if delay > 0:
            send_log(f"Rate limiting: waiting {delay:.2f}s for {domain}", logging.DEBUG, context="CRAWL")
            await asyncio.sleep(delay)
        
        # Configure the crawler
        run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
        if self.request.content_selector:
            run_cfg.css_selector = self.request.content_selector
        
        # Crawl the page
        try:
            send_log(f"Crawling URL: {url} (depth {depth})", context="CRAWL")
            async with AsyncWebCrawler(config=self.browser_cfg) as crawler:
                result = await crawler.arun(url=url, config=run_cfg)
                
                # Update rate limiter
                self.rate_limiter.update_access(domain, success=True)
                
                if result and result.success:
                    # Process successful result
                    title = result.markdown.split("\n")[0] if result.markdown else ""
                    tokens = len(result.markdown.split()) if result.markdown else 0
                    
                    # Log title for better context
                    if title:
                        send_log(f"Title: {title[:50]}{'...' if len(title) > 50 else ''}", logging.DEBUG, context="CRAWL")
                    
                    # Return page data
                    return {
                        "url": url,
                        "title": title,
                        "markdown": result.markdown,
                        "html_hash": hashlib.md5(result.html.encode("utf-8")).hexdigest(),
                        "crawl_time": datetime.datetime.now().isoformat(),
                        "html": result.html,  # Keep HTML for link extraction
                        "final_url": getattr(result, "final_url", url)
                    }
        except Exception as e:
            send_log(f"Error crawling {url}: {e}", logging.ERROR, context="CRAWL")
            self.status.error_count += 1
            self.rate_limiter.update_access(domain, success=False)
        
        return None
    
    def extract_links_from_html(self, html: str, base_url: str) -> List[str]:
        """Extract all links from HTML regardless of content selector"""
        links = []
        try:
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all('a', href=True):
                href = a["href"].strip()
                if href and not href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
                    absolute_url = urldefrag(urljoin(base_url, href))[0]
                    links.append(absolute_url)
        except Exception as e:
            send_log(f"Error extracting links from HTML: {e}", logging.ERROR, context="CRAWL")
        return links
    
    async def add_url_to_queue(self, url: str, depth: int, is_from_sitemap: bool = False) -> bool:
        """Add URL to queue if it meets criteria and isn't already in queue"""
        if self._should_skip_url(url) or url in self.url_in_queue:
            return False
        
        # Determine priority based on source
        priority = self.request.max_depth if is_from_sitemap else max(0, self.request.max_depth - depth)
        new_depth = min(depth, self.request.max_depth)
        
        # Add to queue
        await self.queue.put((priority, (url, new_depth)))
        self.url_in_queue.add(url)
        self.status.pages_valid += 1
        
        # Update metrics
        if is_from_sitemap:
            self.sitemap_urls_count += 1
        else:
            self.discovered_urls_count += 1
        
        return True
    
    def log_progress(self, force: bool = False):
        """Log progress at regular intervals"""
        if force or (self.status.pages_crawled - self.last_progress_log) >= self.progress_log_interval:
            progress = (self.status.pages_crawled / self.request.max_pages) * 100 if self.request.max_pages > 0 else 0
            send_log(
                f"Progress: {self.status.pages_crawled}/{self.request.max_pages} pages ({progress:.1f}%), " +
                f"Queue: {self.status.queue_size}, " +
                f"Discovered: {self.discovered_urls_count}, " + 
                f"Tokens: {self.status.token_count}",
                context="CRAWL"
            )
            self.last_progress_log = self.status.pages_crawled
    
    async def crawl(self) -> List[Dict[str, Any]]:
        """Main crawl function that processes the queue and returns results"""
        # Set up initial state
        self.status.state = CrawlerState.INITIALIZING
        self.status.start_time = datetime.datetime.now()
        send_log("Starting crawl initialization", context="CRAWL")
        
        # Initialize the queue
        await self.add_initial_tasks()
        if self.queue.empty():
            send_log("No URLs to crawl", logging.WARNING, context="CRAWL")
            self.status.state = CrawlerState.COMPLETED
            self.status.end_time = datetime.datetime.now()
            return self.results
        
        # Start crawling
        self.status.state = CrawlerState.CRAWLING
        send_log(f"Starting crawl process with {self.status.pages_valid} URLs in queue", context="CRAWL")
        
        # Configure aiohttp session with connection pooling
        connector = TCPConnector(
            limit=10,  # Overall concurrent connections
            limit_per_host=2,  # Connections per host
            enable_cleanup_closed=True,
            force_close=False,
            ttl_dns_cache=300  # Cache DNS lookups
        )
        timeout = ClientTimeout(total=30, connect=10, sock_read=20)
        
        # Track overall timeout
        overall_timeout = time.time() + 3600  # 1 hour max
        
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            # Process the queue
            while (not self.queue.empty() and 
                   self.status.pages_crawled < self.request.max_pages and 
                   not self.cancel_event.is_set() and
                   time.time() < overall_timeout):
                
                try:
                    # Get next URL with timeout
                    priority, (url, depth) = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Skip if URL should be skipped
                if self._should_skip_url(url):
                    self.queue.task_done()
                    continue
                
                # Mark as visited and currently crawling
                self.visited_urls.add(url)
                self.url_in_queue.discard(url)  # Remove from queue tracking
                self.currently_crawling.add(url)
                
                # Process the page
                page_result = await self._process_page(url, depth, session)
                
                if page_result:
                    # Update state for successful crawl
                    with self.result_lock:
                        # Only store what we need in the final results
                        result_to_store = {k: v for k, v in page_result.items() 
                                          if k not in ["html", "final_url"]}
                        self.results.append(result_to_store)
                    
                    self.status.pages_crawled += 1
                    self.status.token_count += len(page_result.get("markdown", "").split())
                    send_log(f"Completed URL: {url}", context="CRAWL")
                    
                    # Log progress periodically
                    self.log_progress()
                    
                    # Handle redirects
                    if page_result.get("final_url") and page_result["final_url"] != url:
                        final_url = page_result["final_url"]
                        self.visited_urls.add(final_url)
                        if final_url in self.url_in_queue:
                            self.url_in_queue.discard(final_url)
                            send_log(f"Handled redirect: {url} → {final_url}", logging.DEBUG, context="CRAWL")
                    
                    # Process all links from HTML regardless of content selector
                    if depth < self.request.max_depth and "html" in page_result:
                        # Extract all links from HTML
                        all_links = self.extract_links_from_html(page_result["html"], url)
                        send_log(f"Extracted {len(all_links)} links from {url}", logging.DEBUG, context="DISCOVERY")
                        
                        # Count metrics
                        new_urls_added = 0
                        sitemap_urls_added = 0
                        
                        # Process each link
                        for next_url in all_links:
                            # Skip if already visited or in queue
                            if next_url in self.visited_urls or next_url in self.url_in_queue:
                                self.duplicates_avoided += 1
                                continue
                            
                            # Check if URL is in scope and valid
                            if next_url.startswith(self.prefix) and not self._should_skip_url(next_url):
                                # Add to queue
                                is_from_sitemap = next_url in self.initial_urls
                                if await self.add_url_to_queue(next_url, depth + 1, is_from_sitemap):
                                    # Count for logging
                                    if is_from_sitemap:
                                        sitemap_urls_added += 1
                                    else:
                                        new_urls_added += 1
                        
                        # Log discovery summary (only if something was added)
                        if new_urls_added > 0 or sitemap_urls_added > 0:
                            log_parts = []
                            if new_urls_added > 0:
                                log_parts.append(f"{new_urls_added} new URLs")
                            if sitemap_urls_added > 0:
                                log_parts.append(f"{sitemap_urls_added} sitemap URLs")
                                
                            send_log(f"Added {' and '.join(log_parts)} from {url}", context="DISCOVERY")
                else:
                    send_log(f"Failed to crawl {url}", logging.WARNING, context="CRAWL")
                
                # Update status and finish task
                self.currently_crawling.discard(url)
                self.queue.task_done()
                self.status.queue_size = self.queue.qsize()
            
            # Log final progress
            self.log_progress(force=True)
            
            # Clean up and report status
            remaining = self.queue.qsize()
            if self.cancel_event.is_set():
                send_log("Crawl cancelled by user", context="CRAWL")
                self.status.state = CrawlerState.CANCELLING
            else:
                send_log(
                    f"Crawl completed. Processed {self.status.pages_crawled}/{self.request.max_pages} pages. " +
                    f"Queue remaining: {remaining}", 
                    context="CRAWL"
                )
                self.status.state = CrawlerState.COMPLETED
            
            self.status.end_time = datetime.datetime.now()
            duration = (self.status.end_time - self.status.start_time).total_seconds()
            send_log(f"Crawl duration: {duration:.1f} seconds", context="CRAWL")
            
            return self.results
    
    def run(self) -> None:
        """Run the crawler in a new event loop"""
        if self.status.state == CrawlerState.CRAWLING:
            send_log("Crawler already running", logging.WARNING, context="CRAWL")
            return
        
        # Set up a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Run the crawl
            results = loop.run_until_complete(self.crawl())
            
            # Process and save results
            with self.result_lock:
                # Prepare discovery metrics
                discovery_stats = {
                    "initial_sitemap_urls": self.sitemap_urls_count,
                    "discovered_urls_count": self.discovered_urls_count,
                    "duplicates_avoided": self.duplicates_avoided,
                    "total_unique_urls": len(self.visited_urls),
                    "discovery_ratio": round((self.discovered_urls_count / max(1, self.status.pages_valid)) * 100, 2)
                }
                
                self.result_data = {
                    "crawl_date": datetime.datetime.now().isoformat(),
                    "source": self.domain,
                    "start_url": self.start_url,
                    "pages": results,
                    "total_tokens": self.status.token_count,
                    "pages_crawled": self.status.pages_crawled,
                    "pages_valid": self.status.pages_valid,
                    "discovery_stats": discovery_stats,
                    "duration_seconds": (self.status.end_time - self.status.start_time).total_seconds() 
                        if self.status.end_time and self.status.start_time else 0
                }
            
            # Save to disk
            save_crawl_result(self.start_url, self.result_data)
            
            # Log discovery summary
            send_log(
                f"Crawl summary: {len(results)} pages, {self.status.token_count} tokens. " +
                f"Found {self.discovered_urls_count} new URLs beyond initial {self.sitemap_urls_count} sitemap URLs", 
                context="CRAWL"
            )
        
        except Exception as e:
            send_log(f"Crawl failed: {e}", logging.ERROR, context="CRAWL")
            self.status.state = CrawlerState.ERROR
            with self.result_lock:
                self.result_data = {"pages": [], "pages_valid": self.status.pages_valid}
        
        finally:
            # Clean up
            self.status.state = CrawlerState.IDLE if self.status.state != CrawlerState.ERROR else CrawlerState.ERROR
            send_log("Crawler shut down", context="CRAWL")
            loop.close()
            
            # Notify CrawlerManager
            if crawler_manager:
                crawler_manager.current_crawler_finished()

    def cancel(self) -> None:
        """Cancel the crawler"""
        if self.status.state == CrawlerState.CRAWLING:
            self.cancel_event.set()
            self.status.state = CrawlerState.CANCELLING
            send_log("Crawl cancellation requested by user", context="CRAWL")

# =======================================
# Crawler Manager - Central coordinator
# =======================================
class CrawlerManager:
    """Central manager for all crawlers and the queue"""
    def __init__(self):
        self.queue_lock = Lock()
        self.current_crawler = None
        self.current_crawler_lock = Lock()
        self.robots_manager = RobotsManager()
        self.rate_limiter = AdaptiveRateLimiter()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.crawled_mapping = self.load_crawled_mapping()
        send_log("Crawler manager initialized", context="INIT")
    
    def load_crawled_mapping(self) -> Dict[str, Any]:
        """Load the mapping of crawled URLs to result files"""
        try:
            if os.path.exists(CRAWLED_MAPPING_FILE):
                with open(CRAWLED_MAPPING_FILE, "r", encoding="utf-8") as f:
                    mapping = json.load(f)
                    send_log(f"Loaded {len(mapping)} cached crawl results", context="PERSIST")
                    return mapping
            send_log("No previous crawl results found", context="PERSIST")
            return {}
        except Exception as e:
            send_log(f"Error loading crawled mapping: {e}", logging.ERROR, context="PERSIST")
            return {}
    
    def save_crawled_mapping(self):
        """Save the crawled URL mapping to disk"""
        try:
            with open(CRAWLED_MAPPING_FILE, "w", encoding="utf-8") as f:
                json.dump(self.crawled_mapping, f, ensure_ascii=False, indent=2)
            send_log(f"Saved {len(self.crawled_mapping)} crawl mappings to disk", context="PERSIST")
        except Exception as e:
            send_log(f"Error saving crawled mapping: {e}", logging.ERROR, context="PERSIST")
    
    def load_queue(self) -> List[Dict[str, Any]]:
        """Load the crawl queue from disk"""
        try:
            if os.path.exists(QUEUE_FILE):
                with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                    queue = json.load(f)
                    return queue
            return []
        except Exception as e:
            send_log(f"Error loading queue: {e}", logging.ERROR, context="QUEUE")
            return []
    
    def save_queue(self, queue: List[Dict[str, Any]]):
        """Save the crawl queue to disk"""
        try:
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(queue, f, ensure_ascii=False, indent=2)
            send_log(f"Saved queue with {len(queue)} tasks", context="QUEUE")
        except Exception as e:
            send_log(f"Error saving queue: {e}", logging.ERROR, context="QUEUE")
    
    def add_to_queue(self, crawl_request: CrawlRequest) -> bool:
        """Add a crawl request to the queue"""
        with self.queue_lock:
            queue_data = self.load_queue()
            
            # Convert to dict for storage
            request_dict = {
                "url": crawl_request.url,
                "max_depth": crawl_request.max_depth,
                "max_pages": crawl_request.max_pages,
                "content_selector": crawl_request.content_selector,
                "sitemap_url": crawl_request.sitemap_url,
                "force": crawl_request.force,
                "submitted_at": crawl_request.submitted_at
            }
            
            # Check for duplicate
            if not any(req.get("url") == crawl_request.url for req in queue_data):
                queue_data.append(request_dict)
                self.save_queue(queue_data)
                send_log(f"Added crawl request for {crawl_request.url} to queue", context="QUEUE")
                return True
            else:
                send_log(f"Duplicate request for {crawl_request.url} ignored", context="QUEUE")
                return False
    
    def get_next_request(self) -> Optional[CrawlRequest]:
        """Get the next request from the queue"""
        with self.queue_lock:
            queue_data = self.load_queue()
            if not queue_data:
                return None
            
            request_dict = queue_data.pop(0)
            self.save_queue(queue_data)
            
            send_log(f"Processing next queue item: {request_dict.get('url')}", context="QUEUE")
            return CrawlRequest(
                url=request_dict.get("url"),
                max_depth=int(request_dict.get("max_depth", 2)),
                max_pages=int(request_dict.get("max_pages", 100)),
                content_selector=request_dict.get("content_selector", ""),
                sitemap_url=request_dict.get("sitemap_url", ""),
                force=request_dict.get("force", False),
                submitted_at=request_dict.get("submitted_at", datetime.datetime.now().isoformat())
            )
    
    def start_crawl(self, crawl_request: CrawlRequest) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Start a new crawl or queue it if busy"""
        # Check for cached results
        if crawl_request.url in self.crawled_mapping and not crawl_request.force:
            send_log(f"Using cached results for {crawl_request.url}", context="CRAWL")
            return False, "cached", self.crawled_mapping[crawl_request.url]
        
        # Check if crawler is busy
        with self.current_crawler_lock:
            if self.current_crawler is not None:
                send_log(f"Crawler busy, queueing request for {crawl_request.url}", context="QUEUE")
                # Queue the request
                queued = self.add_to_queue(crawl_request)
                return False, "queued" if queued else "duplicate", None
            
            # Start a new crawl
            send_log(f"Starting new crawl for {crawl_request.url}", context="CRAWL")
            self.current_crawler = RecursiveCrawlManager(
                crawl_request,
                self.robots_manager,
                self.rate_limiter
            )
            # Launch in a separate thread
            self.executor.submit(self.current_crawler.run)
            return True, "started", None
    
    def cancel_current_crawl(self) -> Tuple[bool, Dict[str, Any]]:
        """Cancel the current crawl if one is running"""
        with self.current_crawler_lock:
            if self.current_crawler is None:
                return False, {"message": "No crawl in progress"}
            
            send_log("Cancelling current crawl", context="CRAWL")
            self.current_crawler.cancel()
            status = self.current_crawler.status.to_dict()
            return True, status
    
    def get_status(self) -> Dict[str, Any]:
        """Get the current crawler status"""
        with self.current_crawler_lock:
            if self.current_crawler is None:
                return {
                    "is_busy": False,
                    "queue_length": len(self.load_queue())
                }
            
            # Return detailed status
            return {
                "is_busy": self.current_crawler.status.state == CrawlerState.CRAWLING,
                "state": self.current_crawler.status.state.name,
                "url": self.current_crawler.start_url,
                "domain": self.current_crawler.domain,
                "pages_crawled": self.current_crawler.status.pages_crawled,
                "pages_valid": self.current_crawler.status.pages_valid,
                "max_pages": self.current_crawler.request.max_pages,
                "token_count": self.current_crawler.status.token_count,
                "queue_size": self.current_crawler.status.queue_size,
                "error_count": self.current_crawler.status.error_count,
                "discovered_urls_count": self.current_crawler.discovered_urls_count, 
                "sitemap_urls_count": self.current_crawler.sitemap_urls_count,
                "start_time": self.current_crawler.status.start_time.isoformat() 
                    if self.current_crawler.status.start_time else None,
                "currently_crawling": list(self.current_crawler.currently_crawling),
                "queue_length": len(self.load_queue())
            }
    
    def get_current_result(self) -> Optional[Dict[str, Any]]:
        """Get the current crawl result if available"""
        with self.current_crawler_lock:
            if self.current_crawler is None or not hasattr(self.current_crawler, "result_data"):
                return None
            
            return self.current_crawler.result_data
    
    def current_crawler_finished(self):
        """Called when the current crawler finishes"""
        with self.current_crawler_lock:
            self.current_crawler = None
        
        # Process the queue
        self.process_queue()
    
    def process_queue(self):
        """Process the next request in the queue"""
        next_request = self.get_next_request()
        if next_request is not None:
            send_log(f"Processing queued crawl: {next_request.url}", context="QUEUE")
            self.start_crawl(next_request)
        else:
            send_log("No pending tasks in queue", logging.DEBUG, context="QUEUE")

# =======================================
# Persistence for Crawl Results
# =======================================
def save_crawl_result(url: str, result_data: Dict[str, Any]) -> str:
    """Save crawl results to disk and update the mapping"""
    domain = urlparse(url).netloc
    file_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    filename = f"{domain.replace('.', '_')}_{file_hash}.json"
    filepath = os.path.join(CRAWL_RESULTS_DIR, filename)
    
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result_data, f, ensure_ascii=False, indent=2)
        
        # Update the mapping
        if crawler_manager:
            with crawler_manager.queue_lock:
                page_count = len(result_data.get("pages", []))
                token_count = result_data.get("total_tokens", 0)
                discovered_count = result_data.get("discovery_stats", {}).get("discovered_urls_count", 0)
                
                crawler_manager.crawled_mapping[url] = {
                    "filename": filename,
                    "last_updated": datetime.datetime.now().isoformat(),
                    "page_count": page_count,
                    "total_tokens": token_count,
                    "discovered_urls": discovered_count
                }
                crawler_manager.save_crawled_mapping()
        
        send_log(f"Saved crawl result to {filename} ({len(result_data.get('pages', []))} pages)", context="PERSIST")
        return filename
    
    except Exception as e:
        send_log(f"Failed to save crawl result: {e}", logging.ERROR, context="PERSIST")
        return ""

# =====================
# Flask Application
# =====================
app = Flask(__name__)
visited_lock = threading.Lock()
crawler_manager = CrawlerManager()

# Add background thread for queue processing
def queue_worker():
    """Background thread to process the queue"""
    while True:
        try:
            crawler_manager.process_queue()
        except Exception as e:
            send_log(f"Error in queue worker: {e}", logging.ERROR, context="QUEUE")
        time.sleep(5)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start-crawl', methods=['POST'])
def start_crawl_route():
    """Start a new crawl or queue it"""
    try:
        data = request.get_json()
        if not isinstance(data, dict) or "url" not in data:
            return jsonify({"error": "Invalid JSON or missing 'url' parameter"}), 400
        
        url = data.get("url")
        send_log(f"Received crawl request for URL: {url}", context="API")
        
        # Create a crawl request from the data
        crawl_request = CrawlRequest(
            url=url,
            max_depth=int(data.get("max_depth", 2)),
            max_pages=int(data.get("max_pages", 100)),
            content_selector=data.get("content_selector", "").strip(),
            sitemap_url=data.get("sitemap_url", "").strip(),
            force=data.get("force", False)
        )
        
        # Try to start the crawl
        started, status, cached_data = crawler_manager.start_crawl(crawl_request)
        
        if status == "cached":
            send_log(f"Returning cached results for {url}", context="API")
            return jsonify({
                "message": "This URL has been crawled before.",
                "cached_data": cached_data
            }), 200
        
        elif status == "queued":
            queue_length = len(crawler_manager.load_queue())
            send_log(f"Request queued. Current queue length: {queue_length}", context="API")
            return jsonify({
                "message": "Server is busy; request queued.",
                "queue_length": queue_length
            }), 202
        
        elif status == "duplicate":
            return jsonify({
                "message": "This URL is already in the queue.",
                "queue_length": len(crawler_manager.load_queue())
            }), 200
        
        else:  # status == "started"
            send_log(f"Crawl started for {url}", context="API")
            return jsonify({
                "message": "Crawl started.",
                "start_url": crawl_request.url,
                "max_depth": crawl_request.max_depth,
                "max_pages": crawl_request.max_pages
            }), 200
    
    except Exception as e:
        send_log(f"Error in start_crawl endpoint: {e}", logging.ERROR, context="API")
        return jsonify({"error": str(e)}), 500

@app.route('/cancel', methods=['POST'])
def cancel_route():
    """Cancel the current crawl"""
    send_log("Received cancellation request", context="API")
    success, status = crawler_manager.cancel_current_crawl()
    if success:
        return jsonify({
            "message": "Crawl cancellation requested.",
            "status": status
        }), 200
    else:
        return jsonify(status), 400

@app.route('/stream')
def stream():
    """Stream logs to the client"""
    send_log("Client connected to log stream", logging.DEBUG, context="API")
    
    def event_stream():
        try:
            # Send initial message
            yield f"data: Log streaming started at {datetime.datetime.now().isoformat()}\n\n"
            
            while True:
                try:
                    message = log_queue.get(timeout=1)
                    yield f"data: {message}\n\n"
                except Empty:
                    # Keep-alive to prevent connection timeout
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            # Client disconnected
            send_log("Client disconnected from log stream", logging.DEBUG, context="API")
    
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/download')
def download():
    """Download the current crawl results"""
    result_data = crawler_manager.get_current_result()
    if not result_data:
        return jsonify({"error": "No crawl data available."}), 404
    
    # Get status to check if the crawl is still in progress
    status = crawler_manager.get_status()
    if status.get("is_busy") and status.get("state") == CrawlerState.CRAWLING.name:
        return jsonify({
            "error": "Crawl in progress.",
            "progress": {
                "pages_crawled": status.get("pages_crawled", 0),
                "pages_valid": status.get("pages_valid", 0),
                "max_pages": status.get("max_pages", 0)
            }
        }), 503
    
    # Return the results
    domain = urlparse(status.get("url", "unknown")).netloc
    filename = f"{domain.replace('.', '_')}_crawl_result.json"
    json_data = json.dumps(result_data, ensure_ascii=False, indent=2)
    response = Response(json_data, mimetype='application/json')
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    send_log(f"Results downloaded by client ({len(result_data.get('pages', []))} pages)", context="API")
    return response

@app.route('/crawled-files')
def crawled_files():
    """Get a list of previously crawled files"""
    files = []
    for url, info in crawler_manager.crawled_mapping.items():
        files.append({
            "url": url,
            "filename": info["filename"],
            "last_updated": info["last_updated"],
            "page_count": info.get("page_count", 0),
            "total_tokens": info.get("total_tokens", 0),
            "discovered_urls": info.get("discovered_urls", 0)
        })
    
    return jsonify({
        "message": "List of crawled files.",
        "files": files,
        "total": len(files)
    }), 200

@app.route('/download-file')
def download_file():
    """Download a specific crawl result file"""
    filename = request.args.get("file")
    if not filename:
        return jsonify({"error": "Missing 'file' parameter."}), 400
    
    filepath = os.path.join(CRAWL_RESULTS_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": f"File '{filename}' not found."}), 404
    
    send_log(f"Downloading file: {filename}", context="API")
    return send_file(filepath, as_attachment=True, download_name=filename)

@app.route('/queue')
def queue_status():
    """Get the current queue status"""
    queue_data = crawler_manager.load_queue()
    send_log(f"Client requested queue status ({len(queue_data)} items)", logging.DEBUG, context="API")
    return jsonify({
        "message": "Current crawl queue status.",
        "queue": queue_data,
        "queue_length": len(queue_data)
    }), 200

@app.route('/status')
def status_endpoint():
    """Get basic system and crawler status"""
    # Get system info
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory().percent
    temps = psutil.sensors_temperatures()
    temperature = "N/A"
    if temps:
        for entries in temps.values():
            if entries:
                temperature = entries[0].current
                break
    
    # Get crawler status
    crawler_status = crawler_manager.get_status()
    
    send_log(f"Client requested status (CPU: {cpu}%, RAM: {ram}%)", logging.DEBUG, context="API")
    return jsonify({
        "message": "Server status.",
        "system": {"cpu": cpu, "ram": ram, "temperature": temperature},
        "crawler": crawler_status
    }), 200

@app.route('/detailed-status')
def detailed_status():
    """Get detailed crawler status"""
    try:
        # Get system info
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        temps = psutil.sensors_temperatures()
        temperature = "N/A"
        if temps:
            for entries in temps.values():
                if entries:
                    temperature = entries[0].current
                    break
        
        # Get crawler status
        crawler_status = crawler_manager.get_status()
        
        send_log("Client requested detailed status", logging.DEBUG, context="API")
        return jsonify({
            "server": {"cpu": cpu, "ram": ram, "temperature": temperature},
            "crawler": crawler_status
        }), 200
    
    except Exception as e:
        send_log(f"Error in detailed status: {e}", logging.ERROR, context="STATUS")
        return jsonify({"error": str(e)}), 500

@app.route('/resume-queue', methods=['POST'])
def resume_queue():
    """Manually resume queue processing"""
    status = crawler_manager.get_status()
    if status.get("is_busy"):
        send_log("Cannot resume: Crawler is busy", logging.WARNING, context="QUEUE")
        return jsonify({"message": "Crawler is busy; queue is active."}), 200
    
    send_log("Queue processing resumed by user", context="QUEUE")
    crawler_manager.process_queue()
    return jsonify({"message": "Queue resumed."}), 200

if __name__ == '__main__':
    # Start the queue worker thread
    import random  # Make sure to import random for jitter in backoff
    send_log("Starting queue worker thread", context="INIT")
    Thread(target=queue_worker, daemon=True).start()
    
    send_log("Starting web server on port 5007", context="INIT")
    app.run(host='0.0.0.0', port=5006, threaded=True)
