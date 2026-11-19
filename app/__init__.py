from flask import Flask, jsonify, request

from . import db, points
from .config import Config
from .recorder import RecorderClient


def create_app(cfg=None, client=None):
    cfg = cfg or Config.from_env()
    client = client or RecorderClient(cfg)
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.get("/api/day/<date>/points")
    def day_points(date):
        conn = db.connect(cfg.data_dir)
        try:
            pts = points.load_day(
                conn, client, cfg, date,
                refresh=request.args.get("refresh") == "1")
        finally:
            conn.close()
        kept = points.filter_accuracy(pts, cfg.max_acc_m)
        return jsonify(date=date, raw_count=len(pts), kept_count=len(kept))

    return app
