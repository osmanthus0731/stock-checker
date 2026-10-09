"""Isolated PO blueprint. Inventory and pricing collections are read-only dependencies."""
from datetime import datetime
from io import BytesIO
import hmac
import re
import secrets
from zoneinfo import ZoneInfo
from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session, url_for
from flask import send_file
from pymongo.errors import PyMongoError
from .domain import ValidationError, snapshot, product_snapshot, purchasing_options, date_value
from .repository import Repository, Conflict
from .document import svg_pages, pdf_bytes, layout


def init_app(app, database, products, pricing):
    app.config.setdefault("PO_CURRENCIES", ("MYR", "USD", "SGD", "EUR", "GBP", "CNY", "JPY", "THB", "AUD"))
    bp = Blueprint("purchase_orders", __name__, url_prefix="/purchase-orders")
    repo = Repository(database.get_collection("purchase_orders"))
    app.extensions["purchase_orders"] = repo

    @app.cli.command("po-init-indexes")
    def init_indexes():
        """Create optional dashboard indexes in the PO collection only."""
        repo.collection.create_index([("date", -1), ("created_at", -1), ("_id", -1)])
        repo.collection.create_index([("created_by", 1), ("date", -1), ("created_at", -1), ("_id", -1)])
        repo.collection.create_index([("status", 1), ("date", -1), ("created_at", -1), ("_id", -1)])
        print("Purchase-order indexes are ready.")

    def scope():
        return {} if session.get("role") == "admin" else {"created_by": session["username"]}

    def csrf_token():
        if "po_csrf" not in session:
            session["po_csrf"] = secrets.token_urlsafe(32)
        return session["po_csrf"]

    def today():
        return datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date().isoformat()

    def get_doc(number):
        doc = repo.get(number, scope())
        if not doc:
            abort(404)
        return doc

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValidationError("Send a JSON object.")
        return data

    def revision(data):
        value = data.get("revision")
        if type(value) is not int or value < 1:
            raise ValidationError("A valid revision is required; reload this PO.")
        return value

    def validated(data, previous=None):
        doc = snapshot(data, app.config["PO_CURRENCIES"])
        if previous:
            # Company identity is historical too, even when editing an old draft.
            doc["company"] = previous["company"]
        try:
            layout({**doc, "number": (previous or {}).get("number", "LOCAL-PO-PENDING")})
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return doc

    @bp.before_request
    def protect():
        if "username" not in session:
            if request.path.startswith(bp.url_prefix + "/api/"):
                return jsonify(error="Please sign in again."), 401
            session["post_login_next"] = request.url
            return redirect(url_for("login", next=request.path))
        if session.get("role") not in ("admin", "worker"):
            abort(403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.content_length and request.content_length > 1024 * 1024:
                abort(413)
            token = request.headers.get("X-CSRF-Token", "")
            if not session.get("po_csrf") or not hmac.compare_digest(token, session["po_csrf"]):
                return jsonify(error="Security token expired. Reload this page and try again."), 400

    @bp.errorhandler(ValidationError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @bp.errorhandler(Conflict)
    def conflict(exc):
        return jsonify(error=str(exc)), 409

    @bp.errorhandler(PyMongoError)
    def unavailable(exc):
        current_app.logger.exception("Purchase-order database operation failed")
        if "/api/" in request.path:
            return jsonify(error="Purchase-order storage is unavailable. Your changes have not been confirmed; retry after checking the dashboard."), 503
        return render_template("purchase_orders/error.html"), 503

    @bp.get("/")
    def dashboard():
        return render_template("purchase_orders/dashboard.html", csrf=csrf_token())

    @bp.get("/new")
    def new():
        doc = {"supplier": {}, "date": today(), "currency": "MYR", "items": [], "discount_type": "amount",
               "discount_value": "0", "status": "draft"}
        return render_template("purchase_orders/editor.html", po=doc, csrf=csrf_token(), currencies=app.config["PO_CURRENCIES"])

    @bp.get("/<number>")
    def view(number):
        return render_template("purchase_orders/view.html", po=get_doc(number), csrf=csrf_token())

    @bp.get("/<number>/edit")
    def edit(number):
        doc = get_doc(number)
        return render_template("purchase_orders/editor.html", po=doc, csrf=csrf_token(), currencies=app.config["PO_CURRENCIES"])

    @bp.get("/<number>/print")
    def print_order(number):
        doc = get_doc(number)
        return render_template("purchase_orders/print.html", po=doc, pages=svg_pages(doc))

    @bp.get("/<number>/pdf")
    def export_pdf(number):
        doc = get_doc(number)
        return send_file(BytesIO(pdf_bytes(doc)), mimetype="application/pdf", as_attachment=True,
                         download_name=doc["number"] + ".pdf")

    @bp.get("/api/orders")
    def list_orders():
        query = scope()
        search = request.args.get("q", "").strip()[:160]
        if search:
            rx = re.compile(re.escape(search), re.I)
            query["$or"] = [{k: rx} for k in ("number", "legacy_number", "supplier.name")]
        status = request.args.get("status", "")
        if status:
            if status not in ("draft", "finalised", "cancelled"):
                raise ValidationError("Invalid status filter.")
            query["status"] = status
        order = request.args.get("sort", "newest")
        if order not in ("oldest", "newest"):
            raise ValidationError("Invalid sort order.")
        try:
            page = max(1, min(100000, int(request.args.get("page", "1"))))
        except ValueError:
            raise ValidationError("Invalid page.") from None
        direction = 1 if order == "oldest" else -1
        fields = {k: 1 for k in ("number", "legacy_number", "supplier.name", "date", "total", "currency", "status", "revision", "created_by")}
        rows = list(repo.collection.find(query, fields).sort([("date", direction), ("created_at", direction), ("_id", direction)]).skip((page-1)*25).limit(25))
        return jsonify(orders=rows, total=repo.collection.count_documents(query), page=page)

    @bp.get("/api/orders/<number>")
    def read_order(number):
        return jsonify(get_doc(number))

    @bp.post("/api/orders")
    def create_order():
        doc = repo.create(validated(payload()), session["username"])
        return jsonify(doc), 201

    @bp.put("/api/orders/<number>")
    def update_order(number):
        original = get_doc(number)
        data = payload()
        return jsonify(repo.update(number, revision(data), validated(data, original), session["username"], scope()))

    @bp.delete("/api/orders/<number>")
    def delete_order(number):
        get_doc(number)
        repo.delete(number, revision(payload()), scope())
        return jsonify(deleted=True, number=number)

    @bp.post("/api/orders/<number>/<action>")
    def action_order(number, action):
        original = get_doc(number)
        data = payload()
        if action == "duplicate":
            return jsonify(repo.duplicate(original, session["username"])), 201
        if action not in ("finalise", "cancel"):
            abort(404)
        if action == "finalise":
            validated(original, original)
        return jsonify(repo.transition(number, revision(data), "finalised" if action == "finalise" else "cancelled", session["username"], scope()))

    @bp.post("/api/preview")
    def preview():
        data = payload()
        original = get_doc(data["number"]) if isinstance(data.get("number"), str) else None
        doc = validated(data, original)
        doc.update(number=(original or {}).get("number", "Pending"), status=(original or {}).get("status", "draft"))
        return jsonify(pages=svg_pages(doc), subtotal=doc["subtotal"], discount=doc["discount"], total=doc["total"])

    @bp.get("/api/products")
    def search_products():
        search = request.args.get("q", "").strip()[:120]
        if not search:
            return jsonify([])
        rx = re.compile(re.escape(search), re.I)
        fields = ("uid", "Part_id", "name", "Desc", "readable_id", "Mssid", "size", "Size", "material", "Material", "Matl")
        docs = products.find({"$or": [{k: rx} for k in fields[:6]]}, {k: 1 for k in fields}).limit(20)
        return jsonify([product_snapshot(d) for d in docs])

    @bp.get("/api/prices")
    def price_options():
        uid = request.args.get("uid", "").strip()[:120]
        currency = request.args.get("currency", "MYR").upper()
        if currency not in app.config["PO_CURRENCIES"]:
            raise ValidationError("Select a supported currency.")
        on_date = date_value(request.args.get("date", today()), "Order date", True)
        product = products.find_one({"$or": [{"uid": uid}, {"Part_id": uid}]}) if uid else None
        if not product:
            return jsonify([])
        mapped = product_snapshot(product)
        candidates = list({v for v in (mapped["product_id"], mapped["mssid"]) if v})
        rows = list(pricing.find({"part_id": {"$in": candidates}, "$or": [{"pricecd": "P"}, {"priced": "P"}]}).limit(500))
        return jsonify(purchasing_options(rows, candidates, currency, on_date))

    app.register_blueprint(bp)
