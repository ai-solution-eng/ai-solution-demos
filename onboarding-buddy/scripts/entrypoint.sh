#!/bin/sh
set -e

# Log function for consistent output
log() {
    echo "[$(date +'%Y-%m-%dT%H:%M:%S%z')] $1" | tee -a /var/log/startup.log 2>/dev/null || \
    echo "[$(date +'%Y-%m-%dT%H:%M:%S%z')] $1"
}

# Ensure log directory exists
mkdir -p /var/log/nginx

# Error handling function
error_exit() {
    log "ERROR: $1"
    exit 1
}

# Check if proxy script exists
if [ ! -f "/app/scripts/proxy.mjs" ]; then
    error_exit "Proxy script not found at /app/scripts/proxy.mjs"
fi

# Start proxy with detailed logging
log "Starting proxy with command: node /app/scripts/proxy.mjs"
cd /app

# Start the proxy in the background
node scripts/proxy.mjs >> /var/log/proxy.log 2>&1 &
proxy_pid=$!

# Give it a moment to start
sleep 2

# Check if the process is still running
if ! kill -0 $proxy_pid 2>/dev/null; then
    log "Proxy failed to start. Last 20 lines of log:"
    tail -n 20 /var/log/proxy.log || true
    error_exit "Failed to start proxy server"
fi

log "Proxy server started with PID $proxy_pid"

# Function to stop services
stop_services() {
    log "Stopping services..."
    if kill -0 $proxy_pid 2>/dev/null; then
        log "Stopping proxy server (PID: $proxy_pid)"
        kill -TERM $proxy_pid || true
    fi
    log "Stopping nginx"
    nginx -s stop 2>/dev/null || true
    exit 0
}

# Set up trap to ensure services are stopped on exit
trap stop_services SIGTERM SIGINT

log "Proxy started with PID: $proxy_pid"

# Wait for the proxy server to be ready
log "Waiting for proxy server to be ready on port 3001..."
max_attempts=30
attempt=1

while [ $attempt -le $max_attempts ]; do
  if wget -qO- http://127.0.0.1:3001/health >/dev/null 2>&1; then
    log "Proxy server is ready!"
    break
  fi

  if [ $attempt -eq $max_attempts ]; then
    log "ERROR: Proxy server did not become ready after $max_attempts attempts"
    exit 1
  fi

  log "Attempt $attempt/$max_attempts - Proxy server not ready yet, retrying in 1 second..."
  sleep 1
  attempt=$((attempt + 1))
done

# Test nginx configuration
log "Testing nginx configuration..."
if ! nginx -t 2>&1 | tee -a /var/log/startup.log; then
    error_exit "Nginx configuration test failed. Check the logs above for details."
fi

# Start nginx in the foreground
log "Starting nginx in the foreground..."
exec nginx -g 'daemon off;'
