import multiprocessing
import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:5000")
backlog = 2048

workers = int(os.getenv("GUNICORN_WORKERS", max(3, multiprocessing.cpu_count() * 2 + 1)))
worker_class = "gevent"
worker_connections = int(os.getenv("GUNICORN_WORKER_CONNECTIONS", 1000))

timeout = 120
graceful_timeout = 30
keepalive = 65

max_requests = 5000
max_requests_jitter = 200

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" (%(L)ss)'

proc_name = "bua_portal"
