#!/bin/bash
# run.sh - Script to setup & run server with Gunicorn (Flask production)
# Script will kill running process based on server.pid if it exists

# ANSI colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Loading animation function
spinner() {
  local pid=$1
  local message=$2
  local spin='-\|/'
  local i=0
  
  # Clear line and show initial message
  echo -ne "\r                                                              \r"
  echo -ne "${YELLOW}$message ${NC}"
  
  # Display spinner while process is running
  while kill -0 $pid 2>/dev/null; do
    i=$(( (i+1) % 4 ))
    echo -ne "\r${YELLOW}$message ${spin:$i:1} ${NC}"
    sleep 0.1
  done
  
  # Clear spinner
  echo -ne "\r                                                              \r"
}

# Display header
echo -e "${BLUE}=========================================="
echo -e "        WEB CRAWLER - Production Mode"
echo -e "==========================================${NC}"
echo ""

# Process command line arguments
INSTALL_DEPS=0
INSTALL_BROWSER=0
RELOAD=0

usage() {
  echo -e "Usage: $0 [-d] [-b] [-r] [-p PORT]"
  echo -e "  -d  Install dependencies from requirements.txt"
  echo -e "  -b  Install browsers for Playwright"
  echo -e "  -r  Enable hot reload (development mode)"
  echo -e "  -p  Specify port number (default: 5006)"
  exit 1
}

while getopts "dbrp:" opt; do
  case ${opt} in
    d ) INSTALL_DEPS=1 ;;
    b ) INSTALL_BROWSER=1 ;;
    r ) RELOAD=1 ;;
    p ) PORT=$OPTARG ;;
    * ) usage ;;
  esac
done

# Identify Python command: prefer python3 if available
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  PYTHON=python
fi

echo -e "${YELLOW}Using Python command: ${PYTHON}${NC}"

# ---------------------------
# Check and kill existing process if running
# ---------------------------
if [ -f server.pid ]; then
  OLD_PID=$(cat server.pid)
  if ps -p $OLD_PID > /dev/null; then
    echo -e "${YELLOW}Found existing server process with PID: ${OLD_PID}. Killing it...${NC}"
    kill -15 ${OLD_PID} # Use SIGTERM for graceful shutdown
    
    # Start async process to wait and show animation
    (
      # Wait for process to terminate (max 5 seconds)
      WAIT_COUNT=0
      while ps -p ${OLD_PID} > /dev/null && [ $WAIT_COUNT -lt 5 ]; do
        sleep 1
        ((WAIT_COUNT++))
      done
      
      # If process still running, force kill
      if ps -p ${OLD_PID} > /dev/null; then
        echo -e "\r${YELLOW}Process didn't terminate gracefully, forcing kill...${NC}"
        kill -9 ${OLD_PID}
      fi
    ) & 
    
    # Show spinner while waiting
    spinner $! "Waiting for process to terminate..."
    
    # Check if process was killed
    if ! ps -p ${OLD_PID} > /dev/null; then
      echo -e "${GREEN}Process ${OLD_PID} killed successfully.${NC}"
    fi
  else
    echo -e "${YELLOW}PID ${OLD_PID} not running, cleaning stale PID file.${NC}"
  fi
  rm server.pid
fi

# ---------------------------
# Setup: Install dependencies and browsers if requested
# ---------------------------
if [ $INSTALL_DEPS -eq 1 ]; then
  echo -e "${YELLOW}Installing dependencies from requirements.txt...${NC}"
  
  # Start dependency installation in background
  pip install -r requirements.txt > /tmp/pip_install.log 2>&1 &
  PIP_PID=$!
  
  # Show animation while installing
  spinner $PIP_PID "Installing dependencies..."
  
  # Check if installation was successful
  if [ $? -ne 0 ] || ! grep -q "Successfully installed" /tmp/pip_install.log; then
    echo -e "${RED}Failed to install dependencies. Check /tmp/pip_install.log for details.${NC}"
    exit 1
  fi
  echo -e "${GREEN}Dependencies installed successfully.${NC}"
  rm /tmp/pip_install.log
fi

if [ $INSTALL_BROWSER -eq 1 ]; then
  echo -e "${YELLOW}Installing browsers for Playwright...${NC}"
  
  # Start browser installation in background
  $PYTHON -m playwright install > /tmp/playwright_install.log 2>&1 &
  PLAYWRIGHT_PID=$!
  
  # Show animation while installing
  spinner $PLAYWRIGHT_PID "Installing browsers..."
  
  # Check if installation was successful
  if [ $? -ne 0 ]; then
    echo -e "${RED}Failed to install browsers. Check /tmp/playwright_install.log for details.${NC}"
    exit 1
  fi
  echo -e "${GREEN}Browsers installed successfully.${NC}"
  rm /tmp/playwright_install.log
fi

# ---------------------------
# Start application with Gunicorn in background
# ---------------------------
# Use PORT environment variable (default to 5006)
PORT=${PORT:-5006}

# Check if port is already in use
if lsof -i:${PORT} >/dev/null 2>&1; then
  echo -e "${RED}Error: Port ${PORT} is already in use.${NC}"
  exit 1
fi

echo -e "${YELLOW}Starting server on port ${PORT} with Gunicorn...${NC}"

# Configure Gunicorn options
GUNICORN_OPTS="--worker-class gevent -w 1 app:app --bind 0.0.0.0:${PORT}"

# Add reload option if enabled
if [ $RELOAD -eq 1 ]; then
  GUNICORN_OPTS="${GUNICORN_OPTS} --reload"
  echo -e "${YELLOW}Hot reload enabled (development mode)${NC}"
fi

# Start Gunicorn
nohup gunicorn $GUNICORN_OPTS > server.log 2>&1 &

# Save PID of running process to server.pid file
echo $! > server.pid
PID=$(cat server.pid)

# Start animation while server is initializing
(sleep 3) & # Simple delay process
spinner $! "Starting server..."

# Verify server started successfully by checking process
if ! ps -p $PID > /dev/null; then
  echo -e "${RED}Server failed to start. Check server.log for details.${NC}"
  exit 1
fi

echo -e "${GREEN}Server started in background on port ${PORT}.${NC}"
echo -e "${GREEN}PID stored in server.pid: ${PID}${NC}"
echo -e "${BLUE}To view logs, use: tail -f server.log${NC}"
echo -e "${BLUE}To stop the server, run: kill $(cat server.pid)${NC}"
echo ""
echo -e "${BLUE}==========================================${NC}"

# Perform a quick health check with spinner
echo -e "${YELLOW}Performing health check...${NC}"
(sleep 2; curl -s http://localhost:${PORT}/health > /dev/null) &
spinner $! "Checking server health..."

if curl -s http://localhost:${PORT}/health > /dev/null 2>&1; then
  echo -e "${GREEN}Health check passed! Server is responding.${NC}"
else
  echo -e "${YELLOW}Warning: Health check failed. Server might not be running properly.${NC}"
  echo -e "${YELLOW}Check server.log for errors.${NC}"
fi
