"""
gunicorn_config.py – Gunicorn Production WSGI Configuration for BUA Portal
Optimized for 1 vCPU / 4GB RAM Hostinger VPS.
"""
import multiprocessing

# Network binding
bind = "127.0.0.1:5000"

# Workers & Threads
# Standard formula for 1 core: (2 * cores) + 1 = 3 workers
workers = 3
worker_class = "gthread"
threads = 4
worker_connections = 1000

# Timeouts & Keep-alive
timeout = 60
graceful_timeout = 30
keepalive = 5

# Memory & Process Management
max_requests = 2000
max_requests_jitter = 100

# Logging
accesslog = "/var/log/bua/access.log"
errorlog = "/var/log/bua/error.log"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" (%(L)ss)'

# Process Name
proc_name = "bua_portal"
