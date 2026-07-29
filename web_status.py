# =============================================================
#   HEALTH / STATUS SERVER
# =============================================================
#
#   Render Web Services must bind the port in $PORT or the deploy is
#   marked unhealthy. This also gives you a real status page rather
#   than a bare "alive" string — useful when the bot is running
#   somewhere you cannot see the logs.
#
#   Deployed as a Background Worker instead? Leave ENABLE_WEB off;
#   workers need no port.
#
#   Endpoints:
#     /         human-readable status
#     /health   200 while the bot loop is ticking, 503 if it has stalled
#     /status   JSON snapshot
#
#   Nothing here can place, modify or cancel an order. It is read-only
#   by construction — no state is mutated and the bot object is only
#   ever read from.
# =============================================================

import json
import time
from threading import Thread

from flask import Flask, Response

# A cycle taking longer than this means the loop has stalled.
STALL_AFTER_SECONDS = 900


def start_status_server(bot, port, get_config_summary=None):
    app = Flask(__name__)
    # Werkzeug's request log would drown the trading log.
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    def snapshot():
        state = getattr(bot, "state", {}) or {}
        last = getattr(bot, "last_cycle_ts", 0) or 0
        age = time.time() - last if last else None
        daily = state.get("daily", {})
        start_eq = daily.get("start_equity")
        equity = getattr(bot, "equity", 0.0)

        day_pct = None
        if start_eq:
            day_pct = (equity - start_eq) / start_eq * 100

        return {
            "mode": "live" if getattr(bot, "live", False) else "dry_run",
            "halted": bool(state.get("halted")),
            "halt_reason": state.get("halt_reason", ""),
            "equity": round(equity, 4),
            "day_start_equity": start_eq,
            "day_change_pct": round(day_pct, 3) if day_pct is not None else None,
            "open_positions": len(state.get("positions", {})),
            "resting_orders": len(state.get("orders", {})),
            "positions": sorted(state.get("positions", {}).keys()),
            "cycles": getattr(bot, "cycle_count", 0),
            "last_cycle_age_seconds": round(age, 1) if age is not None else None,
            "stalled": bool(age is not None and age > STALL_AFTER_SECONDS),
            "started_at": getattr(bot, "started_at", None),
        }

    @app.route("/")
    def home():
        s = snapshot()
        lines = [
            "FARUEXEE Trade Bot",
            "=" * 40,
            f"mode            : {s['mode']}",
            f"halted          : {s['halted']}"
            + (f"  ({s['halt_reason']})" if s["halt_reason"] else ""),
            f"equity          : {s['equity']}",
            f"today           : {s['day_change_pct']}%"
            if s["day_change_pct"] is not None else "today           : n/a",
            f"open positions  : {s['open_positions']}  {s['positions']}",
            f"resting orders  : {s['resting_orders']}",
            f"cycles          : {s['cycles']}",
            f"last cycle      : {s['last_cycle_age_seconds']}s ago",
            f"stalled         : {s['stalled']}",
        ]
        if get_config_summary:
            lines += ["", "-" * 40, get_config_summary()]
        return Response("\n".join(lines), mimetype="text/plain")

    @app.route("/health")
    def health():
        s = snapshot()
        # Report unhealthy once the loop has clearly stopped ticking, so
        # the platform restarts the service instead of leaving a dead bot
        # holding open positions.
        code = 503 if s["stalled"] else 200
        return Response(json.dumps({"ok": code == 200, **s}),
                        status=code, mimetype="application/json")

    @app.route("/status")
    def status():
        return Response(json.dumps(snapshot(), indent=2),
                        mimetype="application/json")

    def run():
        app.run(host="0.0.0.0", port=port, threaded=True)

    t = Thread(target=run, daemon=True)
    t.start()
    return t
