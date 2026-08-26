"""core-banking-lite: a deliberately "legacy" back-office stand-in app."""
import time
import uuid
from datetime import datetime, timedelta

from flask import Flask, request, redirect, url_for, session, render_template

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"

SESSION_TIMEOUT_SECONDS = 45
VALID_USERS = {"operator1": "correcthorse"}

MEMBERS = {
    "10001": {"id": "10001", "name": "Alice Nguyen", "savings_balance": 4210.55},
    "10002": {"id": "10002", "name": "Ben Torres", "savings_balance": 128.10},
    "10003": {"id": "10003", "name": "Chidi Okafor", "savings_balance": 998877.02},
}
SUB_ACCOUNTS = {}
_slow_load_armed = {"member_search": False}


def require_login():
    if not session.get("user"):
        return False
    last = session.get("last_active")
    if last is None:
        return False
    if datetime.utcnow() - datetime.fromisoformat(last) > timedelta(seconds=SESSION_TIMEOUT_SECONDS):
        session.clear()
        return False
    session["last_active"] = datetime.utcnow().isoformat()
    return True


@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    expired = request.args.get("expired")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if VALID_USERS.get(username) == password:
            session.clear()
            session["user"] = username
            session["last_active"] = datetime.utcnow().isoformat()
            return redirect(url_for("member_search"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, expired=expired)


@app.route("/members/search", methods=["GET", "POST"])
def member_search():
    if not require_login():
        return redirect(url_for("login", expired=1))

    if request.args.get("member_id") and not _slow_load_armed["member_search"]:
        _slow_load_armed["member_search"] = True
        time.sleep(2.5)  # one-time simulated slow load

    member_id = None
    result = None
    not_found = False
    if request.method == "POST":
        member_id = request.form.get("member_id", "").strip()
    elif request.args.get("member_id"):
        member_id = request.args.get("member_id").strip()

    if member_id:
        result = MEMBERS.get(member_id)
        not_found = result is None

    return render_template("member_search.html", member_id=member_id, result=result, not_found=not_found)


@app.route("/members/<member_id>")
def member_detail(member_id):
    if not require_login():
        return redirect(url_for("login", expired=1))
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("member_not_found.html", member_id=member_id), 404
    sub_accounts = SUB_ACCOUNTS.get(member_id, [])
    return render_template("member_detail.html", member=member, sub_accounts=sub_accounts)


@app.route("/members/<member_id>/sub-account/new", methods=["GET", "POST"])
def new_sub_account(member_id):
    if not require_login():
        return redirect(url_for("login", expired=1))
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("member_not_found.html", member_id=member_id), 404

    error = None
    if request.method == "POST":
        acct_type = request.form.get("acct_type", "")
        deposit_raw = request.form.get("initial_deposit", "").strip()
        try:
            deposit = float(deposit_raw)
        except ValueError:
            deposit = -1
        if deposit < 25:
            error = "Initial deposit must be at least $25.00."
        else:
            session["pending_sub_account"] = {"member_id": member_id, "acct_type": acct_type, "deposit": deposit}
            return redirect(url_for("confirm_sub_account", member_id=member_id))

    return render_template("new_sub_account.html", member=member, error=error)


@app.route("/members/<member_id>/sub-account/confirm", methods=["GET", "POST"])
def confirm_sub_account(member_id):
    if not require_login():
        return redirect(url_for("login", expired=1))
    pending = session.get("pending_sub_account")
    if not pending or pending["member_id"] != member_id:
        return redirect(url_for("member_detail", member_id=member_id))

    if request.method == "POST":
        new_id = "SA-" + uuid.uuid4().hex[:8].upper()
        SUB_ACCOUNTS.setdefault(member_id, []).append(
            {"id": new_id, "type": pending["acct_type"], "deposit": pending["deposit"]}
        )
        session.pop("pending_sub_account", None)
        return render_template("sub_account_success.html", member_id=member_id, new_id=new_id, acct=pending)

    return render_template("confirm_sub_account.html", member_id=member_id, pending=pending)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(port=5001, debug=False)