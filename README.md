# Web Crawler

This **Web Crawler** project enables you to crawl data from documentation websites (subpages within the same domain only) and displays real-time server information (CPU, RAM, temperature, and busy/idle state). It uses **Flask** as the web framework, **Playwright** for headless crawling, **aiohttp** to fetch robots.txt, **psutil** to monitor server resources, and **Gunicorn** (with gevent workers) for a production environment.

---

## Table of Contents

1. [Features](#features)  
2. [Project Structure](#project-structure)  
3. [Installation](#installation)  
4. [Usage](#usage)  
5. [Configuration](#configuration)  
6. [Common Issues](#common-issues)  
7. [License](#license)

---

## Features

1. **Crawl Documentation**  
   - Enter a root URL and automatically crawl all subpages (within the same domain).  
   - Limit crawl depth, number of pages, request concurrency, request timeout, etc.  

2. **Real-Time Server Monitoring**  
   - The interface displays CPU usage, RAM usage, temperature (if sensors are supported), and whether the server is busy (crawling) or idle.

3. **Live Log Streaming**  
   - Uses Server-Sent Events (SSE) to stream the crawl log to the web interface in real time.

4. **Cancellation & Download**  
   - Allows you to cancel an ongoing crawl.  
   - Once the crawl is finished (or canceled), you can download the JSON result.

---

## Project Structure

```
.
├── app.py               # Main Flask application with crawl logic, SSE endpoint, etc.
├── templates/
│   └── index.html       # Web interface (dark mode UI, real-time logs, server status)
├── run.sh               # Script to run Gunicorn in production mode (with gevent worker)
├── requirements.txt     # Python dependencies
└── server.log           # Log file created at runtime (after running run.sh)
```

---

## Installation
---

1. **Clone or Download the Project**  
   Open your terminal and run:
   ```bash
   git clone https://github.com/tatsuyakari1203/webcrawler.git
   cd webcrawler
   ```
   
This will clone the repository to your local machine and change your working directory to the project folder.

2. **Install Dependencies**  
   This project requires Python 3.10+ (or a suitable Python 3.x environment). You can install dependencies either by using the included `run.sh` script or manually:
   ```bash
   pip install -r requirements.txt
   ```
   Make sure the following are installed:
   - **Flask**  
   - **aiohttp**  
   - **beautifulsoup4**  
   - **playwright**  
   - **psutil**  
   - **gunicorn**  
   - **gevent**  

3. **Install Playwright Browsers**  
   After installing the `playwright` package, you need to install the required browsers:
   ```bash
   python -m playwright install
   ```
   *(If your environment only has `python3`, use `python3 -m playwright install`.)*

---

## Usage

### 1. Production Mode (Recommended)
Use the `run.sh` script to start Gunicorn in the background:

```bash
chmod +x run.sh
./run.sh
```

What the script does:
1. **Checks & Kills Existing Server** (based on `server.pid`, if it exists).
2. **Installs Dependencies** from `requirements.txt`.
3. **Installs Playwright Browsers**.
4. **Starts Gunicorn** in the background (with gevent worker), binding to port 5006 by default (or the `$PORT` environment variable).
5. Logs are written to `server.log`, and the PID is saved to `server.pid`.

To view logs in real time:
```bash
tail -f server.log
```

To stop the server manually:
```bash
kill $(cat server.pid)
rm server.pid
```

### 2. Development Mode (Optional)
For quick testing, you can run the Flask development server:
```bash
python app.py
```
This will start the server at `http://0.0.0.0:5006` in debug mode. **Do not use this for production**.

---

## Configuration

1. **Port**  
   - By default, the script listens on port `5006`.  
   - To change the port, set the `PORT` environment variable before running the script:
     ```bash
     PORT=8080 ./run.sh
     ```

2. **Crawl Parameters**  
   - **URL Documentation**: Root URL to crawl (e.g., `https://example.com/docs`).  
   - **Max Tokens**: Limit on the number of tokens for each page’s text content.  
   - **Max Depth**: Maximum depth to crawl from the root page.  
   - **Concurrency**: Number of concurrent requests.  
   - **Timeout**: Timeout (in seconds) for each request.  
   - **Max Pages**: Maximum number of pages to crawl overall.

---

## Common Issues

1. **Server is Busy**  
   - If you see the message `"Server is busy. Please try again later."` in the logs or UI, it means there is already a crawl in progress. Wait until it finishes or cancel the current crawl before starting a new one.

2. **Download Requested but No Crawl Data**  
   - This occurs if you attempt to download JSON before any crawl has finished (or if no data is available). Make sure a crawl has completed or been canceled, and that data is present.


3. **Port Already in Use**  
   - If port 5006 is in use, kill the old process or specify a different port using the `PORT` variable.  
   - Check running processes:
     ```bash
     lsof -i :5006
     ```
     or
     ```bash
     netstat -tulnp | grep 5006
     ```
     Then kill if necessary.

4. **Temperature Sensor Not Found**  
   - If your system doesn’t support temperature sensors, the temperature field will show `"N/A"`.

5. **Playwright Errors**  
   - Ensure you have installed browsers via `python -m playwright install` and that the environment matches the Python environment used to run the server.

---

## License

This project is released under the MIT License and is free for modification, extension, and redistribution. You are allowed to use it for both personal and commercial purposes, provided you adhere to the terms of the MIT License, including preserving the copyright.

Please ensure you review and comply with the licenses of all dependencies (e.g., Flask, Playwright, Gunicorn, etc.) if you use this project commercially.

This project includes code and support generated with assistance from ChatGPT.
---

**Thank you for using Web Crawler!**  
Feel free to contribute or report issues to improve the project further.
