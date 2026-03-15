"""
file-service: Stateful backend.
Creates files on a mounted PVC (EBS volume) and logs every event.
Demonstrates: StatefulSets, PVCs, PDB protection, volume persistence.
"""
import logging
import json
import os
import time
from datetime import datetime
from flask import Flask, jsonify, request, Response
import prometheus_client
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)

# ──────────────────────────────────────────────
# Structured JSON Logger
# ──────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "service": "file-service",
            "message": record.getMessage(),
        })

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("file-service")
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ──────────────────────────────────────────────
# Prometheus Metrics
# ──────────────────────────────────────────────
REQUEST_COUNT = Counter(
    "fileservice_http_requests_total",
    "Total HTTP requests to file-service",
    ["method", "endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "fileservice_http_request_duration_seconds",
    "Request latency in seconds for file-service",
    ["endpoint"]
)
FILES_CREATED = Counter(
    "files_created_total",
    "Total files written to the PVC storage volume"
)

# ──────────────────────────────────────────────
# Storage path (this will be the PVC mount point)
# ──────────────────────────────────────────────
STORAGE_PATH = os.getenv("STORAGE_PATH", "/data")


@app.route("/health")
def health():
    """
    Health check also verifies the PVC mount is accessible.
    If /data is not writable, this returns 503 — ALB will stop routing here.
    """
    try:
        test_path = os.path.join(STORAGE_PATH, ".health-check")
        with open(test_path, "w") as f:
            f.write("ok")
        return jsonify({"status": "ok", "service": "file-service", "storage": STORAGE_PATH}), 200
    except Exception as e:
        logger.error(f"Storage health check FAILED: {e}")
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/create", methods=["POST"])
def create_file():
    """
    Creates a file on the mounted PVC.
    The file persists even if the pod restarts — demonstrates PVC durability.
    """
    start = time.time()
    payload = request.get_json(silent=True) or {}
    filename = payload.get("filename", f"file-{int(time.time())}.txt")

    # Sanitize filename (prevent directory traversal)
    filename = os.path.basename(filename)
    filepath = os.path.join(STORAGE_PATH, filename)

    try:
        with open(filepath, "w") as f:
            content = {
                "created_at": datetime.utcnow().isoformat(),
                "pod": os.getenv("HOSTNAME", "unknown"),
                "node": os.getenv("NODE_NAME", "unknown"),  # Injected via valueFrom fieldRef
                "version": os.getenv("APP_VERSION", "v1.0.0"),
                "data": payload.get("data", "default-file-content"),
            }
            f.write(json.dumps(content, indent=2))

        FILES_CREATED.inc()
        duration = time.time() - start
        REQUEST_COUNT.labels("POST", "/create", "200").inc()
        REQUEST_LATENCY.labels("/create").observe(duration)
        logger.info(f"File created: {filepath} in {duration:.3f}s by pod {os.getenv('HOSTNAME')}")

        return jsonify({
            "status": "created",
            "filename": filename,
            "path": filepath,
            "pod": os.getenv("HOSTNAME", "unknown"),
            "duration": round(duration, 3),
        })

    except Exception as e:
        logger.error(f"Failed to create file {filename}: {e}")
        REQUEST_COUNT.labels("POST", "/create", "500").inc()
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/list", methods=["GET"])
def list_files():
    """Lists all files in the PVC. Proves persistence across pod restarts."""
    try:
        files = os.listdir(STORAGE_PATH)
        files = [f for f in files if not f.startswith(".")]  # skip hidden files
        logger.info(f"Listed {len(files)} files from storage")
        return jsonify({
            "storage_path": STORAGE_PATH,
            "file_count": len(files),
            "files": files,
            "pod": os.getenv("HOSTNAME", "unknown"),
        })
    except Exception as e:
        logger.error(f"Failed to list files: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    """Deletes a specific file from the PVC."""
    filename = os.path.basename(filename)
    filepath = os.path.join(STORAGE_PATH, filename)
    try:
        os.remove(filepath)
        logger.info(f"Deleted file: {filepath}")
        return jsonify({"status": "deleted", "filename": filename})
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
