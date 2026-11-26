"""灰度升档演练单服务：Flask 应用工厂与路由。"""
from __future__ import annotations

import os

from flask import Flask, flash, redirect, render_template, request, url_for

from . import services
from .db import connect, init_db
from .gateway import start_answer_services, trial_request
from .timeutil import (
    ValidationError,
    bp_to_percent,
    parse_limit_bp,
    parse_ratio_bp,
    parse_utc_minute,
)


def create_app(start_services: bool = True) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("GRAY_SECRET_KEY", "gray-release-demo-secret")
    init_db()
    if start_services:
        start_answer_services()

    app.template_filter("pct")(bp_to_percent)

    def db():
        return connect()

    @app.route("/", methods=["GET"])
    def index():
        conn = db()
        try:
            state = services.get_state(conn)
            return render_template(
                "index.html",
                state=state,
                overview=services.overview(conn),
                logs=services.recent_logs(conn),
                latest=services.latest_correction(conn),
                endpoints=start_answer_services() if start_services else None,
            )
        finally:
            conn.close()

    @app.route("/ramp", methods=["POST"])
    def ramp():
        try:
            start = parse_utc_minute(request.form.get("window_start"), "起始分钟")
            end = parse_utc_minute(request.form.get("window_end"), "结束分钟")
            ratio_bp = parse_ratio_bp(request.form.get("target_ratio"),
                                      "目标分流比例")
            limit_bp = parse_limit_bp(request.form.get("error_limit"),
                                      "错误率上限")
            conn = db()
            try:
                result = services.submit_ramp(conn, start, end, ratio_bp, limit_bp)
            finally:
                conn.close()
            flash(result["message"],
                  "success" if result["accepted"] else "warning")
        except ValidationError as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.route("/correct", methods=["POST"])
    def correct():
        minutes = request.form.getlist("minute[]")
        reqs = request.form.getlist("requests[]")
        fails = request.form.getlist("failures[]")
        try:
            conn = db()
            try:
                result = services.submit_correction(
                    conn, list(zip(minutes, reqs, fails)))
            finally:
                conn.close()
            flash(result["message"],
                  "success" if result["accepted"] else "error")
        except ValidationError as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.route("/trial", methods=["POST"])
    def trial():
        conn = db()
        try:
            state = services.get_state(conn)
            payload = trial_request(bool(state["active"]),
                                    state["target_ratio_bp"])
        finally:
            conn.close()
        flash(
            f"试请求实际应答版本：{payload['version']}"
            f"（{payload['service']} 服务，按当前分流规则命中 {payload['target']}）",
            "info")
        return redirect(url_for("index"))

    return app
