import os
import secrets
import sqlite3
import re
from collections import Counter
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   session, flash, g, abort)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")

GOALS = ("Weight Loss", "Weight Gain", "Maintain Weight")
DIETS = ("Veg", "Non-Veg")
GENDERS = ("Male", "Female", "Other")
ACTIVITY = {"Sedentary": 1.2, "Light": 1.375, "Moderate": 1.55, "Active": 1.725}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------
# Database
# ---------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        age INTEGER,
        gender TEXT,
        height REAL,
        weight REAL,
        goal TEXT,
        email TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL
    )
    """)
    # Upgrade older databases
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "diet" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN diet TEXT DEFAULT 'Non-Veg'")
    if "activity" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN activity TEXT DEFAULT 'Moderate'")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS weight_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        weight REAL NOT NULL,
        logged_at TEXT NOT NULL
    )
    """)
    # Give existing users a starting weight entry
    conn.execute("""
    INSERT INTO weight_logs(user_id, weight, logged_at)
    SELECT id, weight, ? FROM users
    WHERE weight IS NOT NULL
      AND id NOT IN (SELECT DISTINCT user_id FROM weight_logs)
    """, (now(),))
    conn.execute("CREATE TABLE IF NOT EXISTS water_logs(user_id INTEGER, day TEXT, glasses INTEGER DEFAULT 0, PRIMARY KEY(user_id, day))")
    conn.execute("CREATE TABLE IF NOT EXISTS meal_checks(user_id INTEGER, day TEXT, slot TEXT, PRIMARY KEY(user_id, day, slot))")
    conn.commit()
    conn.close()


init_db()


# ---------------------------
# Security: CSRF + login
# ---------------------------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.")
            return redirect("/login")
        return view(*args, **kwargs)
    return wrapped


def is_hashed(value):
    return value.startswith(("scrypt:", "pbkdf2:"))


@app.errorhandler(400)
def bad_request(e):
    return render_template(
        "error.html", code=400,
        message="That request could not be verified. Go back, refresh the page and try again."), 400


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="That page does not exist."), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Something went wrong on our side."), 500


# ---------------------------
# Health calculations
# ---------------------------
def calc_bmi(height_cm, weight_kg):
    height_m = height_cm / 100
    bmi = round(weight_kg / (height_m * height_m), 2)
    if bmi < 18.5:
        status = "Underweight"
    elif bmi < 25:
        status = "Normal Weight"
    elif bmi < 30:
        status = "Overweight"
    else:
        status = "Obese"
    return bmi, status


def calorie_target(u):
    """Mifflin-St Jeor estimate. Returns (bmr, maintenance, daily target)."""
    sex = (u["gender"] or "").lower()
    s = 5 if sex.startswith("m") else -161 if sex.startswith("f") else -78
    bmr = 10 * u["weight"] + 6.25 * u["height"] - 5 * u["age"] + s
    tdee = bmr * ACTIVITY.get(u["activity"], 1.55)
    adjust = {"Weight Loss": -500, "Weight Gain": 400}.get(u["goal"], 0)
    target = max(1200, int(round(tdee + adjust, -1)))
    return round(bmr), round(tdee), target


def goal_note(goal, status):
    if status == "Underweight" and goal == "Weight Loss":
        return ("Your BMI is in the underweight range, so losing weight may not be healthy. "
                "Consider talking to a doctor or dietitian.")
    if status in ("Overweight", "Obese") and goal == "Weight Gain":
        return ("Your BMI is above the normal range, so gaining weight may not be the best goal. "
                "Consider talking to a doctor or dietitian.")
    return None


SPLIT = {"Weight Loss": (35, 35, 30), "Weight Gain": (25, 50, 25), "Maintain Weight": (25, 50, 25)}


def macros(target, goal):
    p, c, f = SPLIT.get(goal, SPLIT["Maintain Weight"])
    return {"p": p, "c": c, "f": f, "pg": round(target * p / 400),
            "cg": round(target * c / 400), "fg": round(target * f / 900)}


def water_goal(weight):
    return max(6, round(weight * 35 / 250))  # 35 ml per kg, 250 ml glasses


def healthy_range(height_cm):
    m = height_cm / 100
    return round(18.5 * m * m, 1), round(24.9 * m * m, 1)


def today_str():
    return date.today().isoformat()


def get_water(uid):
    r = get_db().execute("SELECT glasses FROM water_logs WHERE user_id=? AND day=?",
                         (uid, today_str())).fetchone()
    return r["glasses"] if r else 0


def get_checked(uid):
    rows = get_db().execute("SELECT slot FROM meal_checks WHERE user_id=? AND day=?",
                            (uid, today_str())).fetchall()
    return {r["slot"] for r in rows}


def get_streak(uid):
    days = {r["day"] for r in get_db().execute(
        "SELECT DISTINCT day FROM meal_checks WHERE user_id=?", (uid,))}
    d = date.today()
    if d.isoformat() not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


def grocery_list(plan):
    items = Counter()
    for row in plan:
        for meal in row[1:]:
            for part in re.sub(r"^[^A-Za-z0-9]+", "", meal).split("+"):
                name = re.sub(r"^\d+\s*", "", part.strip())
                if name:
                    items[name] += 1
    return sorted(items.items(), key=lambda x: (-x[1], x[0]))


# ---------------------------
# Meal data
# ---------------------------
# (day, breakfast, lunch, dinner) - vegetarian base plans
WEEKLY_VEG = {
    "Weight Loss": [
        ("Monday", "🥣 Oats + Apple", "🍚 Brown Rice + Dal + Salad", "🥗 Veg Soup + 1 Chapati"),
        ("Tuesday", "🍎 Fruit Bowl + Green Tea", "🍛 Quinoa + Veg Curry", "🥬 Sprouts Salad"),
        ("Wednesday", "🥪 Veg Sandwich", "🍛 Dal + 2 Chapati + Salad", "🧀 Grilled Paneer + Veg"),
        ("Thursday", "🥣 Poha", "🍚 Brown Rice + Sambar", "🍅 Tomato Soup"),
        ("Friday", "🥞 Moong Dal Chilla", "🍛 Chapati + Mixed Veg Curry", "🥒 Cucumber Salad + Curd"),
        ("Saturday", "🥣 Idli + Sambar", "🍚 Veg Pulao + Raita", "🥦 Stir-fried Vegetables"),
        ("Sunday", "🍎 Fruits + Nuts", "🍲 Millet Khichdi", "🥛 Warm Milk + Soup"),
    ],
    "Weight Gain": [
        ("Monday", "🥛 Milk + Banana + Nuts", "🍚 Rice + Dal + Ghee + Veg", "🧀 Paneer + Chapati"),
        ("Tuesday", "🥜 Peanut Butter Toast + Milk", "🍛 Rajma + Rice", "🫓 Aloo Paratha + Curd"),
        ("Wednesday", "🥪 Cheese Sandwich + Banana Shake", "🍚 Veg Biryani + Raita", "🌯 Paneer Wrap"),
        ("Thursday", "🥞 Dosa + Chutney", "🍛 Chole + Rice", "🍛 Dal + Chapati + Ghee"),
        ("Friday", "🥞 Besan Chilla + Milk", "🧀 Paneer Curry + Chapati", "🍲 Khichdi + Ghee"),
        ("Saturday", "🥣 Idli Sambar + Banana", "🍚 Veg Pulao + Paneer", "🫓 Stuffed Paratha"),
        ("Sunday", "🥞 Pancakes + Honey", "🍚 Veg Biryani", "🥛 Milk + Dates + Nuts"),
    ],
    "Maintain Weight": [
        ("Monday", "🥣 Oats + Fruit", "🍚 Rice + Dal + Vegetables", "🥗 Salad + Chapati"),
        ("Tuesday", "🍎 Fruits + Milk", "🍛 Dal Rice", "🥬 Mixed Veg + Chapati"),
        ("Wednesday", "🥪 Sandwich + Milk", "🍛 Rajma + Rice", "🍲 Veg Soup + Chapati"),
        ("Thursday", "🥛 Milk + Banana", "🍚 Rice + Sambar", "🧀 Paneer + Chapati"),
        ("Friday", "🥣 Upma", "🍛 Chapati + Curry", "🥗 Salad + Curd Rice"),
        ("Saturday", "🥣 Idli", "🍚 Veg Pulao", "🥬 Vegetables + Chapati"),
        ("Sunday", "🍎 Fruits + Nuts", "🍚 Veg Biryani", "🥛 Milk + Khichdi"),
    ],
}

# Days that change for Non-Veg users
NONVEG_OVERRIDES = {
    "Weight Loss": {
        "Wednesday": ("Wednesday", "🍳 Boiled Eggs + Toast", "🍗 Grilled Chicken + Salad", "🥗 Grilled Vegetables"),
        "Friday": ("Friday", "🍳 Egg White Omelette", "🐟 Grilled Fish + Veg", "🥒 Cucumber Salad + Curd"),
        "Saturday": ("Saturday", "🥣 Idli + Sambar", "🍗 Chicken Curry + 1 Chapati", "🥦 Stir-fried Vegetables"),
    },
    "Weight Gain": {
        "Monday": ("Monday", "🥛 Milk + Banana + Eggs", "🍗 Chicken + Rice + Vegetables", "🧀 Paneer + Chapati"),
        "Wednesday": ("Wednesday", "🍳 Omelette + Toast", "🍗 Chicken Biryani", "🌯 Chicken Wrap"),
        "Friday": ("Friday", "🍳 Egg Bhurji + Toast", "🐟 Fish Curry + Rice", "🍗 Chicken Curry + Chapati"),
        "Saturday": ("Saturday", "🥣 Idli Sambar + Boiled Eggs", "🍗 Chicken + Rice", "🫓 Stuffed Paratha"),
    },
    "Maintain Weight": {
        "Wednesday": ("Wednesday", "🍳 Eggs + Toast", "🍗 Chicken Rice", "🍲 Veg Soup + Chapati"),
        "Friday": ("Friday", "🍳 Eggs", "🍛 Chapati + Curry", "🥗 Salad + Curd Rice"),
        "Saturday": ("Saturday", "🥣 Idli", "🍗 Chicken + Rice", "🥬 Vegetables + Chapati"),
    },
}


def get_plan(goal, diet):
    goal = goal if goal in WEEKLY_VEG else "Maintain Weight"
    plan = {row[0]: row for row in WEEKLY_VEG[goal]}
    if diet != "Veg":
        plan.update(NONVEG_OVERRIDES.get(goal, {}))
    return [plan[d] for d in DAYS]


# ---------------------------
# Form helpers
# ---------------------------
def parse_profile(form):
    try:
        data = {
            "name": form["name"].strip(),
            "age": int(form["age"]),
            "gender": form["gender"],
            "height": float(form["height"]),
            "weight": float(form["weight"]),
            "goal": form["goal"],
            "diet": form["diet"],
            "activity": form["activity"],
        }
    except (ValueError, KeyError):
        return None, "Please fill in all fields with valid values."

    if not data["name"]:
        return None, "Please enter your name."
    if not (1 <= data["age"] <= 120 and 50 <= data["height"] <= 250
            and 20 <= data["weight"] <= 400):
        return None, "Age, height or weight looks out of range."
    if (data["goal"] not in GOALS or data["diet"] not in DIETS
            or data["activity"] not in ACTIVITY or data["gender"] not in GENDERS):
        return None, "Please choose a valid option in every dropdown."
    return data, None


def current_user():
    return get_db().execute(
        "SELECT * FROM users WHERE id=?", (session["user_id"],)
    ).fetchone()


def log_weight(user_id, weight):
    db = get_db()
    db.execute("INSERT INTO weight_logs(user_id, weight, logged_at) VALUES(?,?,?)",
               (user_id, weight, now()))


def build_chart(logs):
    """logs: rows oldest -> newest. Returns data for an inline SVG line chart."""
    if len(logs) < 2:
        return None
    W, H, PX, PY = 600, 220, 60, 30
    weights = [l["weight"] for l in logs]
    lo, hi = min(weights), max(weights)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    n = len(logs)
    points = []
    for i, l in enumerate(logs):
        x = PX + i * (W - 2 * PX) / (n - 1)
        y = H - PY - (l["weight"] - lo) * (H - 2 * PY) / (hi - lo)
        points.append((round(x, 1), round(y, 1), l["weight"], l["logged_at"][:10]))
    return {
        "w": W, "h": H, "py": PY,
        "points": points,
        "poly": " ".join(f"{x},{y}" for x, y, _, _ in points),
        "lo": round(lo, 1), "hi": round(hi, 1),
    }


# ---------------------------
# Pages
# ---------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        data, error = parse_profile(request.form)
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not error and not email:
            error = "Please enter your email."
        if not error and len(password) < 6:
            error = "Password must be at least 6 characters."

        db = get_db()
        if not error and db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            error = "An account with this email already exists."

        if error:
            flash(error)
            return render_template("register.html", data=request.form)

        cur = db.execute(
            """INSERT INTO users(name,age,gender,height,weight,goal,diet,activity,email,password)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (data["name"], data["age"], data["gender"], data["height"], data["weight"],
             data["goal"], data["diet"], data["activity"], email,
             generate_password_hash(password)),
        )
        log_weight(cur.lastrowid, data["weight"])
        db.commit()
        flash("Registration successful! Please log in.")
        return redirect("/login")

    return render_template("register.html", data={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

        valid = False
        if user:
            stored = user["password"]
            if is_hashed(stored):
                valid = check_password_hash(stored, password)
            elif stored == password:
                # Old plain-text account: accept once, then upgrade to a hash
                valid = True
                db.execute("UPDATE users SET password=? WHERE id=?",
                           (generate_password_hash(password), user["id"]))
                db.commit()

        if valid:
            session.clear()
            session["user_id"] = user["id"]
            return redirect("/dashboard")

        flash("Invalid email or password.")
        return redirect("/login")

    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    if u is None:
        session.clear()
        return redirect("/login")

    bmi, status = calc_bmi(u["height"], u["weight"])
    bmr, maintenance, target = calorie_target(u)
    return render_template(
        "dashboard.html", u=u, bmi=bmi, status=status,
        bmr=bmr, maintenance=maintenance, target=target,
        note=goal_note(u["goal"], status), m=macros(target, u["goal"]),
        water=get_water(u["id"]), wgoal=water_goal(u["weight"]),
        streak=get_streak(u["id"]), done=len(get_checked(u["id"])),
        gauge=max(0, min(100, (bmi - 15) / 25 * 100)))


@app.route("/bmi", methods=["GET", "POST"])
@login_required
def bmi():
    u = current_user()
    bmi_value, status = None, ""
    if request.method == "POST":
        try:
            height = float(request.form["height"])
            weight = float(request.form["weight"])
            if height <= 0 or weight <= 0:
                raise ValueError
            bmi_value, status = calc_bmi(height, weight)
        except (ValueError, KeyError):
            flash("Please enter valid positive numbers.")
    lo, hi = healthy_range(u["height"])
    shown = bmi_value or calc_bmi(u["height"], u["weight"])[0]
    return render_template("bmi.html", bmi=bmi_value, status=status, u=u, lo=lo, hi=hi,
                           gauge=max(0, min(100, (shown - 15) / 25 * 100)))


@app.route("/meal_plan")
@login_required
def meal_plan():
    u = current_user()
    if u is None:
        session.clear()
        return redirect("/login")

    plan = get_plan(u["goal"], u["diet"])
    today = datetime.now().strftime("%A")
    row = next(r for r in plan if r[0] == today)
    target = calorie_target(u)[2]
    return render_template("meal_plan.html", goal=u["goal"], diet=u["diet"] or "Non-Veg",
                           today=today, meals={"breakfast": row[1], "lunch": row[2], "dinner": row[3]},
                           target=target, checked=get_checked(u["id"]),
                           water=get_water(u["id"]), wgoal=water_goal(u["weight"]),
                           m=macros(target, u["goal"]))


@app.route("/weekly_plan")
@login_required
def weekly_plan():
    u = current_user()
    if u is None:
        session.clear()
        return redirect("/login")

    return render_template("weekly_plan.html", plan=get_plan(u["goal"], u["diet"]),
                           goal=u["goal"], diet=u["diet"] or "Non-Veg",
                           today=datetime.now().strftime("%A"))


@app.route("/grocery")
@login_required
def grocery():
    u = current_user()
    plan = get_plan(u["goal"], u["diet"])
    return render_template("grocery.html", items=grocery_list(plan), goal=u["goal"],
                           diet=u["diet"] or "Non-Veg")


@app.route("/water", methods=["POST"])
@login_required
def water():
    delta = 1 if request.form.get("delta") == "1" else -1
    db = get_db()
    new = max(0, min(20, get_water(session["user_id"]) + delta))
    db.execute("INSERT INTO water_logs(user_id, day, glasses) VALUES(?,?,?) "
               "ON CONFLICT(user_id, day) DO UPDATE SET glasses=excluded.glasses",
               (session["user_id"], today_str(), new))
    db.commit()
    return redirect("/meal_plan" if request.form.get("next") == "meals" else "/dashboard")


@app.route("/check", methods=["POST"])
@login_required
def check():
    slot = request.form.get("slot")
    if slot in ("breakfast", "lunch", "dinner"):
        db, uid = get_db(), session["user_id"]
        if slot in get_checked(uid):
            db.execute("DELETE FROM meal_checks WHERE user_id=? AND day=? AND slot=?",
                       (uid, today_str(), slot))
        else:
            db.execute("INSERT INTO meal_checks(user_id, day, slot) VALUES(?,?,?)",
                       (uid, today_str(), slot))
        db.commit()
    return redirect("/meal_plan")


@app.route("/progress", methods=["GET", "POST"])
@login_required
def progress():
    db = get_db()
    uid = session["user_id"]

    if request.method == "POST":
        try:
            weight = float(request.form["weight"])
            if not 20 <= weight <= 400:
                raise ValueError
        except (ValueError, KeyError):
            flash("Enter a weight between 20 and 400 kg.")
            return redirect("/progress")
        log_weight(uid, weight)
        db.execute("UPDATE users SET weight=? WHERE id=?", (weight, uid))
        db.commit()
        flash("Weight saved.")
        return redirect("/progress")

    recent = db.execute(
        "SELECT weight, logged_at FROM weight_logs WHERE user_id=? "
        "ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
    chart = build_chart(list(reversed(recent)))
    change = None
    if len(recent) >= 2:
        change = round(recent[0]["weight"] - recent[-1]["weight"], 1)
    return render_template("progress.html", logs=recent, chart=chart,
                           change=change, u=current_user())


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db = get_db()
    uid = session["user_id"]
    u = current_user()
    if u is None:
        session.clear()
        return redirect("/login")

    if request.method == "POST":
        data, error = parse_profile(request.form)
        if error:
            flash(error)
            return render_template("profile.html", data=request.form)

        db.execute(
            """UPDATE users SET name=?, age=?, gender=?, height=?, weight=?,
               goal=?, diet=?, activity=? WHERE id=?""",
            (data["name"], data["age"], data["gender"], data["height"], data["weight"],
             data["goal"], data["diet"], data["activity"], uid))
        if data["weight"] != u["weight"]:
            log_weight(uid, data["weight"])
        db.commit()
        flash("Profile updated. Your meal plans now match your new details.")
        return redirect("/profile")

    return render_template("profile.html", data=dict(u))


@app.route("/change_password", methods=["POST"])
@login_required
def change_password():
    u = current_user()
    current = request.form.get("current", "")
    new = request.form.get("new", "")
    confirm = request.form.get("confirm", "")

    if not is_hashed(u["password"]) or not check_password_hash(u["password"], current):
        flash("Current password is incorrect.")
    elif len(new) < 6:
        flash("New password must be at least 6 characters.")
    elif new != confirm:
        flash("New passwords do not match.")
    else:
        db = get_db()
        db.execute("UPDATE users SET password=? WHERE id=?",
                   (generate_password_hash(new), u["id"]))
        db.commit()
        flash("Password changed.")
    return redirect("/profile")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
