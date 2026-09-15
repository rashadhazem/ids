"""
gunicorn_config.py – Gunicorn High-Concurrency Production Configuration for BUA Portal
Async Gevent Coroutines / 500+ simultaneous requests & 400 concurrent uploads.
"""
import multiprocessing
import os

# Network binding & socket queue
bind = os.getenv("GUNICORN_BIND", "127.0.0.1:5000")
backlog = 2048  # High socket queue to prevent connection refusal under surge traffic

# High-Concurrency Async Workers (Gevent Greenlets)
# Standard formula: (2 * cores) + 1
workers = max(3, multiprocessing.cpu_count() * 2 + 1)
worker_class = "gevent"
worker_connections = 1000  # Capable of 3,000+ simultaneous persistent connections

# Timeouts & Keep-alive
timeout = 120
graceful_timeout = 30
keepalive = 65

# Memory & Process Lifecycle
max_requests = 5000
max_requests_jitter = 200

# Logging
accesslog = os.getenv("GUNICORN_ACCESS_LOG", "/var/log/bua/access.log")
errorlog = os.getenv("GUNICORN_ERROR_LOG", "/var/log/bua/error.log")
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" (%(L)ss)'

# Process Name
proc_name = "bua_portal"
