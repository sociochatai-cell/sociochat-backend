# crm_management/routes/dashboard.py
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, timedelta

bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s) if "T" in s else datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None

def resolve_date_range(start_raw, end_raw):
    now = datetime.utcnow()

    # Handle "undefined"
    if start_raw in (None, "", "undefined"):
        start_raw = None
    if end_raw in (None, "", "undefined"):
        end_raw = None

    # Handle presets like 7d, 30d, 90d
    if start_raw and start_raw.endswith("d"):
        try:
            days = int(start_raw[:-1])
            return now - timedelta(days=days), now
        except ValueError:
            pass
    elif start_raw and start_raw.endswith("m"):
        try:
            months = int(start_raw[:-1])
            # Approx 30 days per month
            return now - timedelta(days=months*30), now
        except ValueError:
            pass

    start = parse_date(start_raw)
    end = parse_date(end_raw)

    if start and end and end.hour == 0:
        end += timedelta(days=1)

    return start, end


@bp.route("/stats", methods=["GET"])
def stats():
    """
    Chat/CRM dashboard KPIs derived from Leads (and Deals for conversion).

    Returns a FLAT shape matching the frontend DashboardStats contract:
        {
            "total_leads": int,
            "new_leads": int,        # leads created within the selected range
            "active_leads": int,     # leads whose status != "closed"
            "conversion_rate": float # won deals / total leads * 100, guarded
        }
    """
    db = current_app.db
    models = current_app.crm_models
    Lead = models["Lead"]
    Deal = models.get("Deal")

    workspace_id = request.args.get("workspace_id")  # TEXT in DB → compare as string
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400

    start, end = resolve_date_range(
        request.args.get("startDate") or request.args.get("range"),
        request.args.get("endDate")
    )

    try:
        # All leads in the workspace (range-independent totals).
        base_q = db.session.query(Lead).filter(Lead.workspace_id == workspace_id)

        total_leads = base_q.count()
        active_leads = base_q.filter(Lead.status != "closed").count()

        # New leads = created within the selected range (fall back to all if no range).
        new_q = base_q
        if start:
            new_q = new_q.filter(Lead.created_at >= start)
        if end:
            new_q = new_q.filter(Lead.created_at < end)
        new_leads = new_q.count()

        # Conversion rate = won deals / total leads * 100 (guard divide-by-zero).
        won_deals = 0
        if Deal is not None:
            try:
                won_deals = (
                    db.session.query(Deal)
                    .filter(Deal.workspace_id == workspace_id)
                    .filter(Deal.stage == "won")
                    .count()
                )
            except Exception:
                # Deal.workspace_id type may differ (Integer vs TEXT); fail soft.
                current_app.logger.exception("dashboard.stats: won deals query failed")
                try:
                    db.session.rollback()
                except Exception:
                    pass
                won_deals = 0

        conversion_rate = (won_deals / total_leads * 100) if total_leads > 0 else 0.0

        return jsonify({
            "total_leads": int(total_leads),
            "new_leads": int(new_leads),
            "active_leads": int(active_leads),
            "conversion_rate": round(float(conversion_rate), 2),
        })
    except Exception:
        current_app.logger.exception("dashboard.stats failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({
            "total_leads": 0,
            "new_leads": 0,
            "active_leads": 0,
            "conversion_rate": 0.0,
        })


@bp.route("/charts/revenue", methods=["GET"])
def revenue_chart():
    """
    Leads-created-per-day trend over the selected range.

    Returns ChartPoint[] → [{"label": <day>, "value": <count>}].
    NOTE: the frontend titles this card "Revenue Trend", but the Campaign
    revenue table is empty in this app, so we surface leads-per-day instead —
    the meaningful signal here. Label is cosmetic only.
    """
    db = current_app.db
    Lead = current_app.crm_models["Lead"]

    workspace_id = request.args.get("workspace_id")  # TEXT in DB → compare as string
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400

    start, end = resolve_date_range(
        request.args.get("startDate") or request.args.get("range"),
        request.args.get("endDate")
    )

    try:
        q = (
            db.session.query(
                db.func.date(Lead.created_at).label("day"),
                db.func.count(Lead.id),
            )
            .filter(Lead.workspace_id == workspace_id)
        )

        if start:
            q = q.filter(Lead.created_at >= start)
        if end:
            q = q.filter(Lead.created_at < end)

        rows = (
            q
            .group_by("day")
            .order_by("day")
            .all()
        )

        out = []
        for day, count in rows:
            # day may be a date/datetime or a string depending on the DB driver.
            if hasattr(day, "strftime"):
                label = day.strftime("%a")
            else:
                label = str(day)
            out.append({"label": label, "value": int(count)})

        return jsonify(out)
    except Exception:
        current_app.logger.exception("dashboard.revenue_chart failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify([])


@bp.route("/charts/sources", methods=["GET"])
def sources_chart():
    """
    Leads grouped by source.

    Returns ChartPoint[] → [{"label": <source>, "value": <count>}].
    """
    db = current_app.db
    Lead = current_app.crm_models["Lead"]

    workspace_id = request.args.get("workspace_id")  # TEXT in DB → compare as string
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400

    try:
        rows = (
            db.session.query(
                Lead.source,
                db.func.count(Lead.id)
            )
            .filter(Lead.workspace_id == workspace_id)
            .group_by(Lead.source)
            .all()
        )

        return jsonify([
            {"label": source or "Unknown", "value": int(count)}
            for source, count in rows
        ])
    except Exception:
        current_app.logger.exception("dashboard.sources_chart failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify([])
