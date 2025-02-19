#!/bin/bash
# run.sh - Script setup & run server với Gunicorn (Flask production)
# Script này sẽ kill tiến trình đang chạy dựa trên server.pid nếu tồn tại

# ANSI colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Hiển thị header
echo -e "${BLUE}=========================================="
echo -e "        WEB CRAWLER - Production Mode"
echo -e "==========================================${NC}"
echo ""

# Xác định lệnh Python: ưu tiên python3 nếu có
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  PYTHON=python
fi

echo -e "${YELLOW}Using Python command: ${PYTHON}${NC}"

# ---------------------------
# Kiểm tra và kill tiến trình cũ nếu tồn tại
# ---------------------------
if [ -f server.pid ]; then
  OLD_PID=$(cat server.pid)
  echo -e "${YELLOW}Found existing server process with PID: ${OLD_PID}. Killing it...${NC}"
  kill ${OLD_PID}
  if [ $? -eq 0 ]; then
    echo -e "${GREEN}Process ${OLD_PID} killed successfully.${NC}"
  else
    echo -e "${RED}Failed to kill process ${OLD_PID}.${NC}"
  fi
  rm server.pid
fi

# ---------------------------
# Setup: Cài đặt các thư viện và trình duyệt cho Playwright
# ---------------------------
echo -e "${YELLOW}Installing dependencies from requirements.txt...${NC}"
pip install -r requirements.txt

echo -e "${YELLOW}Installing browsers for Playwright...${NC}"
$PYTHON -m playwright install

# ---------------------------
# Khởi chạy ứng dụng với Gunicorn ở chế độ background
# ---------------------------
# Sử dụng biến môi trường PORT (nếu có, nếu không mặc định là 5006)
PORT=${PORT:-5006}

echo -e "${YELLOW}Starting server on port ${PORT} with Gunicorn...${NC}"
nohup gunicorn --worker-class gevent -w 1 app:app --bind 0.0.0.0:${PORT} > server.log 2>&1 &

# Lưu PID của tiến trình chạy vào file server.pid
echo $! > server.pid

echo -e "${GREEN}Server started in background on port ${PORT}.${NC}"
echo -e "${GREEN}PID stored in server.pid.${NC}"
echo -e "${BLUE}To view logs, use: tail -f server.log${NC}"
echo ""
echo -e "${BLUE}==========================================${NC}"
