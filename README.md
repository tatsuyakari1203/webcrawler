# Web Crawler

This **Web Crawler** project enables you to crawl data from documentation websites (only subpages within the same domain) and display real-time server information (CPU, RAM, temperature, and busy/idle state). It uses **Flask** as the web framework, **Playwright** for headless crawling, **aiohttp** to fetch robots.txt, **psutil** to monitor server resources, and **Gunicorn** (with gevent workers) for production deployment.

---

## Table of Contents

- [English Version](#english-version)
  - [Features](#features)
  - [Project Structure](#project-structure)
  - [Installation](#installation)
  - [Usage](#usage)
  - [Configuration](#configuration)
  - [Common Issues](#common-issues)
  - [License](#license)
- [Phiên Bản Tiếng Việt](#phiên-bản-tiếng-việt)
  - [Tính Năng](#tính-năng)
  - [Cấu Trúc Dự Án](#cấu-trúc-dự-án)
  - [Hướng Dẫn Cài Đặt](#hướng-dẫn-cài-đặt)
  - [Hướng Dẫn Sử Dụng](#hướng-dẫn-sử-dụng)
  - [Cấu Hình](#cấu-hình-1)
  - [Các Vấn Đề Thường Gặp](#các-vấn-đề-thường-gặp)
  - [Giấy Phép](#giấy-phép)

---

## English Version

### Features

- **Crawl Documentation:**  
  - Crawl data from a given root URL and automatically process subpages within the same domain.
  - Configure parameters such as maximum tokens per page, crawl depth, number of pages, concurrency, and request timeout.

- **Real-Time Server Monitoring:**  
  - Displays live server metrics: CPU usage, RAM usage, temperature (if available), and whether the crawler is busy or idle.

- **Live Log Streaming:**  
  - Uses Server-Sent Events (SSE) to stream crawl logs to the web interface in real time.

- **Cancellation & Download:**  
  - Allows you to cancel an ongoing crawl.
  - Once the crawl is finished or canceled, you can download the JSON result.

### Project Structure

```
.
├── app.py               # Main Flask application containing crawl logic, SSE endpoint, etc.
├── templates/
│   └── index.html       # Web interface (Dark Mode, live logs, server status)
├── run.sh               # Shell script to set up and run the server using Gunicorn (with gevent worker)
├── requirements.txt     # Python dependencies
└── server.log           # Log file generated at runtime (after running run.sh)
```

### Installation

1. **Clone or Download the Project**  
   Open your terminal and run:
   ```bash
   git clone https://github.com/tatsuyakari1203/webcrawler.git
   cd webcrawler
   ```
   This clones the repository and sets your working directory to the project folder.

2. **Install Dependencies**  
   Ensure you have Python 3.10+ (or a compatible Python 3.x environment). Install required packages by running:
   ```bash
   pip install -r requirements.txt
   ```
   This will install Flask, aiohttp, beautifulsoup4, playwright, psutil, gunicorn, gevent, and other dependencies.

3. **Install Playwright Browsers**  
   After installing the `playwright` package, run:
   ```bash
   python -m playwright install
   ```
   *(If only `python3` is available, use `python3 -m playwright install`.)*

### Usage

#### Production Mode (Recommended)

Use the provided `run.sh` script to start the server with Gunicorn:

```bash
chmod +x run.sh
./run.sh
```

**What the script does:**
- Checks for an existing server process (using `server.pid`) and kills it if found.
- Installs dependencies from `requirements.txt` and Playwright browsers.
- Starts Gunicorn with a gevent worker in the background on port 5006 (or the port specified by the `PORT` environment variable).
- Writes logs to `server.log` and saves the server PID in `server.pid`.

To monitor logs in real time:
```bash
tail -f server.log
```

To stop the server manually:
```bash
kill $(cat server.pid)
rm server.pid
```

#### Development Mode (Optional)

For quick testing, run:
```bash
python app.py
```
This launches the Flask development server at `http://0.0.0.0:5006` in debug mode. **Do not use the development server for production.**

### Configuration

- **Port:**  
  The default port is `5006`. To change the port, set the `PORT` environment variable:
  ```bash
  PORT=8080 ./run.sh
  ```

- **Crawl Parameters (via Web Interface):**  
  - **URL Documentation:** The root URL to crawl (e.g., `https://example.com/docs`).
  - **Max Tokens:** Token limit per page's text.
  - **Max Depth:** Maximum crawl depth from the root page.
  - **Concurrency:** Number of concurrent requests.
  - **Timeout:** Request timeout in seconds.
  - **Max Pages:** Maximum number of pages to crawl.

### Common Issues

1. **Server is Busy:**  
   If you see `"Server is busy. Please try again later."`, it means a crawl is already in progress. Wait or cancel the ongoing crawl before starting a new one.

2. **No Crawl Data:**  
   If you attempt to download results before any crawl data is available, you’ll receive an error. Ensure that the crawl has finished or been canceled, and data is present.

3. **Port Already in Use:**  
   If port 5006 is occupied, either kill the existing process or change the port using the `PORT` variable:
   ```bash
   lsof -i :5006
   netstat -tulnp | grep 5006
   ```
   Then kill the process if needed.

4. **Temperature Sensor Not Available:**  
   If your system doesn’t support temperature sensors, the temperature field will display `"N/A"`.

5. **Playwright Errors:**  
   Ensure you have installed browsers via `python -m playwright install` (or `python3 -m playwright install`) and that the correct Python environment is being used.

6. **Feather Icons Issues:**  
   If you see icon errors (e.g., using `download-cloud`), verify the icon names at [Feather Icons](https://feathericons.com/) and update accordingly.

### License

This project is released under the MIT License and is free for modification, extension, and redistribution. You may use it for both personal and commercial purposes provided you adhere to the terms of the MIT License, including preserving the original copyright.

Please ensure you review and comply with the licenses of all dependencies (e.g., Flask, Playwright, Gunicorn, etc.) if you use this project commercially.

This project includes code and support generated with assistance from ChatGPT.

---

## Phiên Bản Tiếng Việt

### Tính Năng

- **Crawl Tài Liệu:**  
  - Nhập URL gốc và tự động crawl các trang con trong cùng domain.  
  - Cấu hình giới hạn số token, độ sâu crawl, số trang tối đa, concurrency, timeout, v.v.

- **Giám Sát Máy Chủ Theo Thời Gian Thực:**  
  - Hiển thị thông số CPU, RAM, nhiệt độ (nếu có) và trạng thái của quá trình crawl (bận/rảnh).

- **Streaming Log Trực Tiếp:**  
  - Sử dụng SSE để hiển thị log crawl theo thời gian thực trên giao diện web.

- **Hủy Crawl & Tải Kết Quả:**  
  - Cho phép hủy crawl đang chạy.  
  - Sau khi crawl hoàn thành (hoặc bị hủy), bạn có thể tải kết quả ở dạng file JSON.

### Cấu Trúc Dự Án

```
.
├── app.py               # Ứng dụng Flask chứa logic crawl, endpoint SSE, vv.
├── templates/
│   └── index.html       # Giao diện web (Dark Mode, hiển thị log, trạng thái máy chủ)
├── run.sh               # Script chạy Gunicorn ở chế độ production
├── requirements.txt     # Danh sách các gói Python cần thiết
└── server.log           # File log được tạo khi chạy run.sh
```

### Hướng Dẫn Cài Đặt

1. **Clone hoặc Tải Dự Án:**  
   Mở terminal và chạy:
   ```bash
   git clone https://github.com/tatsuyakari1203/webcrawler.git
   cd webcrawler
   ```
   Lệnh này sẽ clone repo về máy của bạn và chuyển vào thư mục dự án.

2. **Cài Đặt Dependencies:**  
   Dự án yêu cầu Python 3.10+ (hoặc phiên bản Python 3.x tương thích). Cài đặt các gói cần thiết:
   ```bash
   pip install -r requirements.txt
   ```
   Các gói cài đặt gồm: Flask, aiohttp, beautifulsoup4, playwright, psutil, gunicorn, gevent.

3. **Cài Đặt Trình Duyệt cho Playwright:**  
   Sau khi cài đặt gói `playwright`, chạy:
   ```bash
   python -m playwright install
   ```
   *(Nếu hệ thống của bạn chỉ có python3, sử dụng: `python3 -m playwright install`.)*

### Hướng Dẫn Sử Dụng

#### 1. Chế Độ Production (Khuyến nghị)

Sử dụng script `run.sh` để chạy server với Gunicorn:

```bash
chmod +x run.sh
./run.sh
```

**Script sẽ:**
- Kiểm tra và kill tiến trình server cũ (dựa trên file `server.pid` nếu tồn tại).
- Cài đặt dependencies và trình duyệt cho Playwright.
- Khởi chạy Gunicorn với worker gevent ở chế độ background trên port 5006 (hoặc theo biến môi trường `PORT`).
- Ghi log vào `server.log` và lưu PID vào `server.pid`.

Để theo dõi log:
```bash
tail -f server.log
```

Để dừng server:
```bash
kill $(cat server.pid)
rm server.pid
```

#### 2. Chế Độ Phát Triển

Để chạy nhanh trên môi trường phát triển, bạn có thể chạy:
```bash
python app.py
```
Server sẽ chạy tại `http://0.0.0.0:5006` trong chế độ debug. **Không sử dụng cho production.**

### Cấu Hình

- **Cổng (Port):**  
  Mặc định server chạy trên port `5006`. Để thay đổi, đặt biến môi trường `PORT` trước khi chạy:
  ```bash
  PORT=8080 ./run.sh
  ```

- **Tham Số Crawl (Nhập qua giao diện web):**  
  - **URL Documentation:** URL gốc để crawl (ví dụ: `https://example.com/docs`).
  - **Max Tokens:** Giới hạn số token cho mỗi trang.
  - **Max Depth:** Độ sâu crawl từ trang gốc.
  - **Concurrency:** Số lượng request đồng thời.
  - **Timeout:** Thời gian timeout (giây) cho mỗi request.
  - **Max Pages:** Số trang tối đa cần crawl.

### Các Vấn Đề Thường Gặp

1. **Server Bận:**  
   Nếu nhận được thông báo `"Server is busy. Please try again later."`, nghĩa là đã có một quá trình crawl đang diễn ra. Hãy đợi cho đến khi crawl hiện tại kết thúc hoặc hủy quá trình đó.

2. **Không Có Dữ Liệu Crawl:**  
   Lỗi `"Download requested but no crawl data is available."` xảy ra khi bạn tải kết quả trước khi crawl hoàn thành hoặc khi không có dữ liệu. Đảm bảo crawl đã kết thúc (hoặc bị hủy) và có dữ liệu.

3. **Port Đã Bị Chiếm Dụng:**  
   Nếu port 5006 đã được sử dụng, hãy kill tiến trình cũ hoặc thay đổi cổng bằng biến `PORT`:
   ```bash
   lsof -i :5006
   netstat -tulnp | grep 5006
   ```
   Sau đó kill tiến trình nếu cần.

4. **Không Tìm Thấy Cảm Biến Nhiệt Độ:**  
   Nếu hệ thống của bạn không hỗ trợ cảm biến nhiệt độ, trường nhiệt độ sẽ hiển thị `"N/A"`.

5. **Lỗi Playwright:**  
   Đảm bảo đã chạy `python -m playwright install` (hoặc `python3 -m playwright install`) và môi trường Python đúng được sử dụng.

6. **Lỗi Icon Feather:**  
   Nếu gặp lỗi về icon (ví dụ: icon `download-cloud`), hãy kiểm tra tên icon tại [Feather Icons](https://feathericons.com/) và cập nhật cho phù hợp.

---

## License

This project is released under the MIT License and is free for modification, extension, and redistribution. You may use it for both personal and commercial purposes provided you adhere to the terms of the MIT License, including preserving the original copyright.

Please ensure you review and comply with the licenses of all dependencies (e.g., Flask, Playwright, Gunicorn, etc.) if you use this project commercially.

This project includes code and support generated with assistance from ChatGPT.

---

**Thank you for using Web Crawler!**  
Feel free to contribute, report issues, or provide feedback to further improve the project.

--- 

*End of README*
