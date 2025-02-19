import asyncio
import datetime
import json
from queue import Queue, Empty
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.robotparser import RobotFileParser

import aiohttp
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, Response, render_template
import threading
import concurrent.futures
from playwright.async_api import async_playwright
import psutil  # pip install psutil

# ------------------------------
# Flask app initialization
# ------------------------------
app = Flask(__name__)

# Global log queue dùng cho SSE (Server Sent Events)
log_queue = Queue()


def send_log(message: str):
    """
    Ghi log kèm thời gian vào console và đẩy vào log_queue.
    Dùng để thông báo tiến trình crawl và các thông tin liên quan.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    print(full_message)
    log_queue.put(full_message)


# ------------------------------
# Các hàm hỗ trợ crawl
# ------------------------------
async def get_robot_parser(session: aiohttp.ClientSession, domain: str, timeout: ClientTimeout):
    """
    Lấy và phân tích nội dung robots.txt từ domain cho trước.

    :param session: Phiên làm việc aiohttp.
    :param domain: Tên domain (ví dụ: 'example.com').
    :param timeout: Thời gian timeout cho request.
    :return: RobotFileParser đã được parse, hoặc None nếu không tìm thấy.
    """
    rp = RobotFileParser()
    for scheme in ["https", "http"]:
        robots_url = f"{scheme}://{domain}/robots.txt"
        try:
            async with session.get(robots_url, timeout=timeout) as response:
                if response.status == 200:
                    text = await response.text()
                    rp.parse(text.splitlines())
                    send_log(f"Loaded robots.txt from {robots_url}")
                    return rp
                else:
                    send_log(f"Robots.txt not found at {robots_url} (status {response.status})")
        except Exception as e:
            send_log(f"Failed to load robots.txt from {robots_url}: {e}")
    send_log(f"No valid robots.txt for {domain}")
    return None


async def fetch_page_content(browser, url: str, timeout_seconds: int):
    """
    Sử dụng Playwright để mở trang, cuộn trang cho đến khi tải xong nội dung
    (để kích hoạt lazy loading) và trả về HTML của trang.

    :param browser: Đối tượng browser từ Playwright.
    :param url: URL cần crawl.
    :param timeout_seconds: Timeout tính bằng giây.
    :return: HTML của trang, hoặc None nếu có lỗi.
    """
    try:
        page = await browser.new_page()
        await page.goto(url, timeout=timeout_seconds * 1000)
        # Cuộn trang cho đến khi chiều cao không thay đổi
        prev_height = await page.evaluate("document.body.scrollHeight")
        while True:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == prev_height:
                break
            prev_height = new_height
        content = await page.content()
        await page.close()
        send_log(f"Page content loaded for: {url}")
        return content
    except Exception as e:
        send_log(f"Error fetching {url} with Playwright: {e}")
        return None


# ------------------------------
# CrawlManager class
# ------------------------------
class CrawlManager:
    """
    Quản lý quá trình crawl:
      - Thiết lập các tham số (URL gốc, độ sâu, concurrency, timeout, max_pages).
      - Quản lý queue và thực hiện crawl đồng thời.
      - Lưu kết quả crawl vào result_data.
      - Hỗ trợ hủy crawl.
    """
    def __init__(self, start_url, max_tokens, max_depth, concurrency, timeout_seconds, max_pages):
        self.start_url = start_url
        self.max_tokens = max_tokens
        self.max_depth = max_depth
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.result_data = None
        self.cancel_event = asyncio.Event()
        self.is_busy = True

    async def crawl(self):
        """
        Thực hiện quá trình crawl.
        
        :return: Danh sách kết quả crawl.
        """
        # Xử lý URL gốc: loại bỏ fragment
        start_url = urldefrag(self.start_url)[0]
        visited = set()
        visited_lock = asyncio.Lock()
        results = []
        q = asyncio.Queue()
        await q.put((start_url, 0))
        semaphore = asyncio.Semaphore(self.concurrency)
        robots_cache = {}
        prefix = start_url if start_url.endswith('/') else start_url + '/'

        session_timeout = ClientTimeout(total=self.timeout_seconds)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            async with aiohttp.ClientSession(timeout=session_timeout) as session:

                async def worker():
                    nonlocal results
                    while True:
                        try:
                            try:
                                url, depth = await asyncio.wait_for(q.get(), timeout=1)
                            except asyncio.TimeoutError:
                                if self.cancel_event.is_set():
                                    send_log("Crawl cancellation detected. Data crawled so far is available.")
                                    break
                                continue

                            async with visited_lock:
                                if url in visited:
                                    q.task_done()
                                    continue
                                if url != start_url and not url.startswith(prefix):
                                    send_log(f"Skipping out-of-scope URL: {url}")
                                    visited.add(url)
                                    q.task_done()
                                    continue
                                visited.add(url)

                            domain = urlparse(url).netloc
                            # Lấy robots.txt từ cache hoặc request mới
                            if domain not in robots_cache:
                                rp = await get_robot_parser(session, domain, session_timeout)
                                robots_cache[domain] = rp
                            else:
                                rp = robots_cache[domain]

                            if rp is not None and not rp.can_fetch("*", url):
                                send_log(f"Blocked by robots.txt: {url}")
                                q.task_done()
                                continue

                            send_log(f"Crawling: {url} (Depth: {depth})")
                            async with semaphore:
                                content = await fetch_page_content(browser, url, self.timeout_seconds)
                            if not content:
                                send_log(f"Failed to retrieve content for {url}")
                                q.task_done()
                                continue

                            soup = BeautifulSoup(content, "html.parser")
                            title = soup.title.string.strip() if soup.title and soup.title.string else ""
                            text_content = soup.get_text(separator=" ", strip=True)
                            if self.max_tokens is not None:
                                tokens = text_content.split()
                                if len(tokens) > self.max_tokens:
                                    text_content = " ".join(tokens[:self.max_tokens])
                            results.append({
                                "url": url,
                                "title": title,
                                "content": text_content
                            })
                            send_log(f"Completed: {url}")

                            if len(results) >= self.max_pages:
                                send_log("Reached max pages limit. Initiating crawl cancellation.")
                                self.cancel_event.set()

                            if depth < self.max_depth and not self.cancel_event.is_set():
                                for a in soup.find_all("a", href=True):
                                    next_url = urldefrag(urljoin(url, a["href"]))[0]
                                    async with visited_lock:
                                        if next_url not in visited:
                                            await q.put((next_url, depth + 1))
                            q.task_done()
                        except Exception as e:
                            send_log(f"Worker error processing URL: {url if 'url' in locals() else 'Unknown'} - {e}")
                            q.task_done()

                # Tạo worker tasks
                worker_tasks = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
                try:
                    await q.join()
                except Exception as e:
                    send_log(f"Queue join error: {e}")
                for task in worker_tasks:
                    task.cancel()
                await browser.close()
                return results

    def run(self):
        """
        Chạy crawl trong một event loop mới, tách biệt với event loop của Flask.
        """
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            results = new_loop.run_until_complete(self.crawl())
            self.result_data = {"pages": results if results is not None else []}
            send_log(f"Crawl completed. {len(results)} pages found.")
        except Exception as e:
            send_log(f"Crawl terminated with error: {e}")
            self.result_data = {"pages": []}
        finally:
            self.is_busy = False
            send_log("Crawler has been completely shut down.")
            new_loop.close()

    def cancel(self):
        """
        Yêu cầu hủy quá trình crawl.
        """
        self.cancel_event.set()
        send_log("Cancellation requested via CrawlManager.")


# Global đối tượng quản lý crawl hiện tại
current_crawler = None

# Executor để chạy crawl trong background
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# ----------------------
# Flask Endpoints
# ----------------------

@app.route('/')
def index():
    """
    Render giao diện chính từ template index.html.
    """
    return render_template('index.html')


@app.route('/start-crawl', methods=['POST'])
def start_crawl_route():
    """
    Khởi tạo quá trình crawl dựa trên dữ liệu từ client (JSON).
    Nếu có crawl đang chạy, trả về lỗi.
    """
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
    send_log("Crawl process initiated.")
    # Chạy crawl trong background thông qua executor
    executor.submit(current_crawler.run)
    return jsonify({"message": "Crawl started."})


@app.route('/cancel', methods=['POST'])
def cancel():
    """
    Hủy quá trình crawl nếu đang chạy.
    """
    global current_crawler
    if current_crawler is not None and current_crawler.is_busy:
        current_crawler.cancel()
        return jsonify({"message": "Crawl cancellation requested. Data crawled so far is available."})
    else:
        return jsonify({"message": "No crawl in progress."}), 400


@app.route('/stream')
def stream():
    """
    SSE endpoint trả về các log message.
    """
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
    """
    Download file JSON chứa kết quả crawl.
    Trả về 503 nếu crawl đang chạy, 404 nếu không có dữ liệu.
    """
    global current_crawler
    if current_crawler is not None and current_crawler.is_busy:
        send_log("Download requested but crawl is still in progress.")
        return "Crawl is still in progress. Please wait until it finishes.", 503
    if current_crawler is None or current_crawler.result_data is None or not current_crawler.result_data.get("pages"):
        send_log("Download requested but no crawl data is available.")
        return "No crawl data available.", 404
    send_log("Download requested; sending file.")
    json_data = json.dumps(current_crawler.result_data, ensure_ascii=False, indent=2)
    response = Response(json_data, mimetype='application/json')
    response.headers["Content-Disposition"] = "attachment; filename=crawl_result.json"
    return response


@app.route('/status')
def status_endpoint():
    """
    Trả về thông tin server: CPU, RAM, nhiệt độ (nếu có) và trạng thái crawl (busy/idle).
    """
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
        send_log(f"Error fetching server status: {e}")
        return jsonify({"cpu": "--", "ram": "--", "temperature": "--", "busy": False, "error": str(e)})


if __name__ == '__main__':
    # Chạy ứng dụng dưới chế độ debug (chỉ dùng cho phát triển)
    app.run(host='0.0.0.0', port=5006, threaded=True)
