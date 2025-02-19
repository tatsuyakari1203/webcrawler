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
import xml.etree.ElementTree as ET

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
# Hàm hỗ trợ lấy và parse robots.txt, cũng như lấy danh sách sitemap
# ------------------------------
async def get_robot_parser(session: aiohttp.ClientSession, domain: str, timeout: ClientTimeout):
    """
    Lấy và phân tích nội dung robots.txt từ domain cho trước.
    Nếu tìm được các sitemap, tiến hành lấy danh sách URL từ sitemap đệ quy.
    """
    rp = RobotFileParser()
    sitemap_urls = []
    for scheme in ["https", "http"]:
        robots_url = f"{scheme}://{domain}/robots.txt"
        try:
            async with session.get(robots_url, timeout=timeout) as response:
                if response.status == 200:
                    text = await response.text()
                    rp.parse(text.splitlines())
                    send_log(f"Loaded robots.txt from {robots_url}")
                    # Tìm directive Sitemap trong robots.txt
                    for line in text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            sitemap = line.split(":", 1)[1].strip()
                            sitemap_urls.append(sitemap)
                    break  # Dừng sau khi tìm được robots.txt hợp lệ
                else:
                    send_log(f"Robots.txt not found at {robots_url} (status {response.status})")
        except Exception as e:
            send_log(f"Failed to load robots.txt from {robots_url}: {e}")
    if not sitemap_urls:
        send_log(f"No sitemap found in robots.txt for {domain}")
    return rp, sitemap_urls

async def fetch_sitemap_urls(sitemap_url: str, session: aiohttp.ClientSession, timeout: ClientTimeout, recursive=True):
    """
    Lấy và parse sitemap từ sitemap_url.
    Hỗ trợ cả sitemap index (chứa các sitemap khác) nếu recursive=True.
    Trả về danh sách URL thu được.
    """
    urls = []
    try:
        async with session.get(sitemap_url, timeout=timeout) as response:
            if response.status != 200:
                send_log(f"Failed to fetch sitemap: {sitemap_url} (status {response.status})")
                return urls
            text = await response.text()
            try:
                root = ET.fromstring(text)
            except Exception as e:
                send_log(f"XML parse error for sitemap {sitemap_url}: {e}")
                return urls
            namespace = {'ns': root.tag.split('}')[0].strip('{')} if '}' in root.tag else {}
            # Kiểm tra xem đây có phải sitemap index không
            if root.tag.endswith("sitemapindex"):
                send_log(f"Sitemap index detected: {sitemap_url}")
                for sitemap in root.findall("ns:sitemap", namespace) if namespace else root.findall("sitemap"):
                    loc = sitemap.find("ns:loc", namespace).text if namespace else sitemap.find("loc").text
                    if recursive:
                        urls.extend(await fetch_sitemap_urls(loc, session, timeout, recursive))
            elif root.tag.endswith("urlset"):
                for url in root.findall("ns:url", namespace) if namespace else root.findall("url"):
                    loc = url.find("ns:loc", namespace).text if namespace else url.find("loc").text
                    urls.append(loc)
                send_log(f"Found {len(urls)} URLs in sitemap: {sitemap_url}")
            else:
                send_log(f"Unknown sitemap format at {sitemap_url}")
    except Exception as e:
        send_log(f"Error fetching sitemap {sitemap_url}: {e}")
    return urls

# ------------------------------
# Hàm lấy nội dung trang với Playwright
# ------------------------------
async def fetch_page_content(browser, url: str, timeout_seconds: int):
    """
    Sử dụng Playwright để mở trang, cuộn trang cho đến khi tải xong nội dung
    (để kích hoạt lazy loading) và trả về HTML của trang.
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
      - Thiết lập các tham số (URL gốc, độ sâu, concurrency, timeout, max_pages, max_tokens).
      - Quản lý queue và thực hiện crawl đồng thời.
      - Lưu kết quả crawl vào result_data.
      - Khi có lệnh stop hoặc đạt giới hạn, các task đang chạy sẽ bị hủy ngay lập tức
        và dữ liệu đã crawl được lưu lại để người dùng download.
    """
    def __init__(self, start_url, max_tokens, max_depth, concurrency, timeout_seconds, max_pages):
        self.start_url = start_url
        self.max_tokens = max_tokens        # Giới hạn tổng số token cho toàn bộ crawl
        self.max_depth = max_depth
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.result_data = None
        self.cancel_event = asyncio.Event()
        self.is_busy = True
        self.token_count = 0  # Tổng số token đã crawl

    async def crawl(self):
        """
        Thực hiện quá trình crawl.
        Trả về danh sách kết quả crawl đã thu thập được.
        """
        start_url = urldefrag(self.start_url)[0]
        visited = set()
        visited_lock = asyncio.Lock()
        results = []
        q = asyncio.Queue()
        # Đưa URL gốc vào queue ban đầu
        await q.put((start_url, 0))
        semaphore = asyncio.Semaphore(self.concurrency)
        prefix = start_url if start_url.endswith('/') else start_url + '/'

        session_timeout = ClientTimeout(total=self.timeout_seconds)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            async with aiohttp.ClientSession(timeout=session_timeout) as session:
                # Lấy robots.txt và sitemap từ domain của URL gốc
                domain = urlparse(start_url).netloc
                rp, sitemap_urls = await get_robot_parser(session, domain, session_timeout)
                # Nếu có sitemap, lấy đệ quy các URL từ sitemap và đưa vào queue
                if sitemap_urls:
                    for sitemap in sitemap_urls:
                        sitemap_link_urls = await fetch_sitemap_urls(sitemap, session, session_timeout)
                        for url in sitemap_link_urls:
                            # Chỉ thêm vào queue nếu thuộc cùng domain
                            if url.startswith(prefix):
                                await q.put((url, self.max_depth))
                else:
                    send_log("No sitemap available. Proceeding with recursive crawl as per original logic.")

                robots_cache = {domain: rp}

                async def worker():
                    nonlocal results
                    while True:
                        # Nếu có lệnh hủy, thoát ngay
                        if self.cancel_event.is_set():
                            break
                        try:
                            try:
                                url, depth = await asyncio.wait_for(q.get(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue

                            # Kiểm tra ngay sau khi lấy job
                            if self.cancel_event.is_set():
                                q.task_done()
                                break

                            async with visited_lock:
                                if url in visited:
                                    q.task_done()
                                    continue
                                # Nếu url không cùng phạm vi, bỏ qua
                                if url != start_url and not url.startswith(prefix):
                                    send_log(f"Skipping out-of-scope URL: {url}")
                                    visited.add(url)
                                    q.task_done()
                                    continue
                                visited.add(url)

                            current_domain = urlparse(url).netloc
                            # Lấy robots.txt từ cache hoặc request mới
                            if current_domain not in robots_cache:
                                rp_current, _ = await get_robot_parser(session, current_domain, session_timeout)
                                robots_cache[current_domain] = rp_current
                            else:
                                rp_current = robots_cache[current_domain]

                            if rp_current is not None and not rp_current.can_fetch("*", url):
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
                            tokens = text_content.split()
                            page_token_count = len(tokens)
                            self.token_count += page_token_count
                            # Nếu vượt quá giới hạn token, hủy crawl ngay
                            if self.token_count >= self.max_tokens:
                                send_log("Token limit reached. Initiating immediate crawl cancellation.")
                                self.cancel_event.set()

                            results.append({
                                "url": url,
                                "title": title,
                                "content": text_content if self.token_count < self.max_tokens else "Token limit reached."
                            })
                            send_log(f"Completed: {url}")

                            # Nếu đạt đến giới hạn số trang, hủy crawl ngay
                            if len(results) >= self.max_pages:
                                send_log("Reached max pages limit. Initiating immediate crawl cancellation.")
                                self.cancel_event.set()

                            # Nếu depth cho phép, duyệt thêm các liên kết từ trang
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
                    return

                # Tạo worker tasks
                worker_tasks = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
                try:
                    # Vòng lặp chính kiểm tra trạng thái của cancel_event và queue
                    while not self.cancel_event.is_set():
                        if q.empty():
                            await asyncio.sleep(0.1)
                        else:
                            await asyncio.sleep(0.1)
                except Exception as e:
                    send_log(f"Error in main loop: {e}")

                # Khi có lệnh hủy, hủy ngay tất cả các worker tasks
                for task in worker_tasks:
                    task.cancel()
                await asyncio.gather(*worker_tasks, return_exceptions=True)
                await browser.close()
                return results

    def run(self):
        """
        Chạy crawl trong một event loop mới, tách biệt với event loop của Flask.
        Sau khi crawl dừng (do hủy hoặc hoàn thành), dữ liệu đã crawl sẽ được lưu lại.
        """
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            results = new_loop.run_until_complete(self.crawl())
            self.result_data = {"pages": results if results is not None else []}
            send_log(f"Crawl completed. {len(results)} pages found. Total tokens: {self.token_count}")
        except Exception as e:
            send_log(f"Crawl terminated with error: {e}")
            self.result_data = {"pages": []}
        finally:
            self.is_busy = False
            send_log("Crawler has been completely shut down.")
            new_loop.close()

    def cancel(self):
        """
        Yêu cầu hủy quá trình crawl ngay lập tức.
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
    Khi hủy, dữ liệu đã crawl sẽ được giữ lại để download.
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
    Tên file sẽ được đặt theo domain của URL gốc.
    """
    global current_crawler
    if current_crawler is not None and current_crawler.is_busy:
        send_log("Download requested but crawl is still in progress.")
        return "Crawl is still in progress. Please wait until it finishes.", 503
    if current_crawler is None or current_crawler.result_data is None or not current_crawler.result_data.get("pages"):
        send_log("Download requested but no crawl data is available.")
        return "No crawl data available.", 404

    send_log("Download requested; sending file.")
    from urllib.parse import urlparse
    domain = urlparse(current_crawler.start_url).netloc
    filename = f"{domain.replace('.', '_')}_crawl_result.json"

    json_data = json.dumps(current_crawler.result_data, ensure_ascii=False, indent=2)
    response = Response(json_data, mimetype='application/json')
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
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
