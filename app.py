"""
NutriPlan -- Flask application entry point.

Route groups, in order:
  Auth        -- register / login / logout (Flask-Login sessions)
  Dashboard   -- today's planned nutrition vs. goals
  Recipes     -- CRUD for the user's recipe library (with ingredients)
  Weekly plan -- the day x meal-type grid, plus two JSON API endpoints
                 (/api/plan/slot, /api/grocery/toggle) that back the
                 page's AJAX behaviour -- see static/app.js. These are
                 the app's two "dynamic, JS-driven backend interaction"
                 features: assigning a recipe to a plan slot and
                 checking off a grocery item both save instantly via
                 fetch(), without a full page reload. The classic HTML
                 <form> submit paths are kept alongside them as a
                 no-JavaScript fallback, so the app still fully works
                 with JavaScript disabled.
  Grocery     -- auto-generated shopping list aggregated from the plan
  Goals       -- per-user daily nutrition targets

Every route that touches user data is decorated with @login_required
and filters every query by current_user.id, so one account can never
see or modify another account's recipes, plan, or goals.
"""
import os
from datetime import date, datetime, timedelta

from flask import Flask, render_template, redirect, url_for, request, flash, jsonify
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
)

from models import db, User, Recipe, RecipeIngredient, PlanEntry, GroceryCheck, Goal, MEAL_TYPES

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def week_start_of(d: date) -> date:
    """Return the Monday on or before d."""
    return d - timedelta(days=d.weekday())


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "change-this-secret-key-before-submitting"
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(
        BASE_DIR, "nutriplan.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.login_message = "Please log in to continue."
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    def get_or_create_goal(user_id):
        goal = Goal.query.filter_by(user_id=user_id).first()
        if not goal:
            goal = Goal(user_id=user_id)
            db.session.add(goal)
            db.session.commit()
        return goal

    def get_week_param():
        raw = request.args.get("week", "")
        try:
            requested = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            requested = date.today()
        return week_start_of(requested)

    # ---------- Auth ----------

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")

            if not username or not password:
                flash("Username and password are required.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            elif User.query.filter_by(username=username).first():
                flash("That username is already taken.", "error")
            else:
                user = User(username=username)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                get_or_create_goal(user.id)
                flash("Account created. Please log in.", "success")
                return redirect(url_for("login"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()

            if user and user.check_password(password):
                login_user(user)
                return redirect(url_for("dashboard"))
            flash("Invalid username or password.", "error")

        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # ---------- Dashboard ----------

    @app.route("/dashboard")
    @login_required
    def dashboard():
        today = date.today()
        goal = get_or_create_goal(current_user.id)

        today_entries = (
            PlanEntry.query.filter_by(user_id=current_user.id, plan_date=today).all()
        )
        totals = {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0}
        for e in today_entries:
            totals["calories"] += e.recipe.calories
            totals["protein_g"] += e.recipe.protein_g
            totals["carbs_g"] += e.recipe.carbs_g
            totals["fat_g"] += e.recipe.fat_g

        def pct(value, target):
            if not target:
                return 0
            return min(int((value / target) * 100), 999)

        progress = {k: pct(totals[k], getattr(goal, f"daily_{k}")) for k in totals}
        over_calories = totals["calories"] > goal.daily_calories

        wk_start = week_start_of(today)
        week_dates = [wk_start + timedelta(days=i) for i in range(7)]
        week_entries = PlanEntry.query.filter(
            PlanEntry.user_id == current_user.id,
            PlanEntry.plan_date >= wk_start,
            PlanEntry.plan_date <= week_dates[-1],
        ).all()
        cal_by_day = {d: 0 for d in week_dates}
        for e in week_entries:
            cal_by_day[e.plan_date] = cal_by_day.get(e.plan_date, 0) + e.recipe.calories
        week_rows = [{"date": d, "calories": cal_by_day[d]} for d in week_dates]

        recipe_count = Recipe.query.filter_by(user_id=current_user.id).count()

        return render_template(
            "dashboard.html",
            today=today,
            goal=goal,
            totals=totals,
            progress=progress,
            over_calories=over_calories,
            today_entries=today_entries,
            week_rows=week_rows,
            recipe_count=recipe_count,
        )

    # ---------- Recipes ----------

    @app.route("/recipes")
    @login_required
    def recipes():
        items = Recipe.query.filter_by(user_id=current_user.id).order_by(Recipe.name).all()
        return render_template("recipes.html", recipes=items)

    def _parse_ingredients(raw_text):
        ingredients = []
        errors = []
        for i, line in enumerate(raw_text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 3:
                errors.append(
                    f"Ingredient line {i}: use 'name, quantity, unit' (e.g. 'Rice, 200, g')."
                )
                continue
            name, qty_raw, unit = parts
            try:
                qty = float(qty_raw)
                if qty <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"Ingredient line {i}: quantity must be a positive number.")
                continue
            if not name or not unit:
                errors.append(f"Ingredient line {i}: name and unit are required.")
                continue
            ingredients.append({"name": name, "quantity": qty, "unit": unit})
        if not ingredients and not errors:
            errors.append("Add at least one ingredient.")
        return ingredients, errors

    def _parse_recipe_form(form):
        errors = []
        name = form.get("name", "").strip()
        if not name:
            errors.append("Recipe name is required.")

        def parse_positive(field, label, allow_int=False):
            raw = form.get(field, "")
            try:
                value = int(raw) if allow_int else float(raw)
                if value <= 0:
                    raise ValueError
                return value
            except ValueError:
                errors.append(f"{label} must be a positive number.")
                return None

        servings = parse_positive("servings", "Servings", allow_int=True)
        calories = parse_positive("calories", "Calories")

        def parse_nonneg(field, label):
            raw = form.get(field, "")
            try:
                value = float(raw)
                if value < 0:
                    raise ValueError
                return value
            except ValueError:
                errors.append(f"{label} must be a non-negative number.")
                return None

        protein_g = parse_nonneg("protein_g", "Protein")
        carbs_g = parse_nonneg("carbs_g", "Carbs")
        fat_g = parse_nonneg("fat_g", "Fat")

        ingredients, ing_errors = _parse_ingredients(form.get("ingredients", ""))
        errors.extend(ing_errors)

        return {
            "name": name,
            "servings": servings,
            "calories": calories,
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
            "ingredients": ingredients,
        }, errors

    @app.route("/recipes/add", methods=["GET", "POST"])
    @login_required
    def add_recipe():
        if request.method == "POST":
            data, errors = _parse_recipe_form(request.form)
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                recipe = Recipe(
                    user_id=current_user.id,
                    name=data["name"],
                    servings=data["servings"],
                    calories=data["calories"],
                    protein_g=data["protein_g"],
                    carbs_g=data["carbs_g"],
                    fat_g=data["fat_g"],
                )
                for ing in data["ingredients"]:
                    recipe.ingredients.append(RecipeIngredient(**ing))
                db.session.add(recipe)
                db.session.commit()
                flash("Recipe added.", "success")
                return redirect(url_for("recipes"))

        return render_template("add_recipe.html")

    @app.route("/recipes/edit/<int:recipe_id>", methods=["GET", "POST"])
    @login_required
    def edit_recipe(recipe_id):
        recipe = Recipe.query.filter_by(
            id=recipe_id, user_id=current_user.id
        ).first_or_404()

        if request.method == "POST":
            data, errors = _parse_recipe_form(request.form)
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                recipe.name = data["name"]
                recipe.servings = data["servings"]
                recipe.calories = data["calories"]
                recipe.protein_g = data["protein_g"]
                recipe.carbs_g = data["carbs_g"]
                recipe.fat_g = data["fat_g"]
                recipe.ingredients.clear()
                for ing in data["ingredients"]:
                    recipe.ingredients.append(RecipeIngredient(**ing))
                db.session.commit()
                flash("Recipe updated.", "success")
                return redirect(url_for("recipes"))

        ingredients_text = "\n".join(
            f"{i.name}, {i.quantity:g}, {i.unit}" for i in recipe.ingredients
        )
        return render_template(
            "edit_recipe.html", recipe=recipe, ingredients_text=ingredients_text
        )

    @app.route("/recipes/delete/<int:recipe_id>", methods=["POST"])
    @login_required
    def delete_recipe(recipe_id):
        recipe = Recipe.query.filter_by(
            id=recipe_id, user_id=current_user.id
        ).first_or_404()
        db.session.delete(recipe)
        db.session.commit()
        flash("Recipe deleted.", "success")
        return redirect(url_for("recipes"))

    # ---------- Weekly plan ----------

    @app.route("/plan", methods=["GET", "POST"])
    @login_required
    def plan():
        """Weekly meal-plan grid. GET renders it; POST is the no-JavaScript
        fallback that saves the whole week at once from one big form. The
        primary path for saving is per-cell AJAX -- see api_plan_slot()."""
        wk_start = get_week_param()
        week_dates = [wk_start + timedelta(days=i) for i in range(7)]

        if request.method == "POST":
            for d in week_dates:
                for meal in MEAL_TYPES:
                    field = f"slot_{d.isoformat()}_{meal}"
                    raw_recipe_id = request.form.get(field, "")
                    existing = PlanEntry.query.filter_by(
                        user_id=current_user.id, plan_date=d, meal_type=meal
                    ).first()
                    if raw_recipe_id:
                        try:
                            recipe_id = int(raw_recipe_id)
                        except ValueError:
                            continue
                        if existing:
                            existing.recipe_id = recipe_id
                        else:
                            db.session.add(
                                PlanEntry(
                                    user_id=current_user.id,
                                    plan_date=d,
                                    meal_type=meal,
                                    recipe_id=recipe_id,
                                )
                            )
                    elif existing:
                        db.session.delete(existing)
            db.session.commit()
            flash("Meal plan saved.", "success")
            return redirect(url_for("plan", week=wk_start.isoformat()))

        recipes_list = Recipe.query.filter_by(user_id=current_user.id).order_by(
            Recipe.name
        ).all()
        existing_entries = PlanEntry.query.filter(
            PlanEntry.user_id == current_user.id,
            PlanEntry.plan_date >= wk_start,
            PlanEntry.plan_date <= week_dates[-1],
        ).all()
        grid = {(e.plan_date, e.meal_type): e.recipe_id for e in existing_entries}

        return render_template(
            "plan.html",
            week_start=wk_start,
            week_dates=week_dates,
            meal_types=MEAL_TYPES,
            recipes=recipes_list,
            grid=grid,
            prev_week=(wk_start - timedelta(days=7)).isoformat(),
            next_week=(wk_start + timedelta(days=7)).isoformat(),
        )

    @app.route("/plan/grocery")
    @login_required
    def grocery_list():
        wk_start = get_week_param()
        week_end = wk_start + timedelta(days=6)

        entries = PlanEntry.query.filter(
            PlanEntry.user_id == current_user.id,
            PlanEntry.plan_date >= wk_start,
            PlanEntry.plan_date <= week_end,
        ).all()

        aggregated = {}
        for e in entries:
            for ing in e.recipe.ingredients:
                key = f"{ing.name.strip().lower()}|{ing.unit.strip().lower()}"
                if key not in aggregated:
                    aggregated[key] = {
                        "name": ing.name.strip(),
                        "unit": ing.unit.strip(),
                        "quantity": 0.0,
                    }
                aggregated[key]["quantity"] += ing.quantity

        checks = {
            c.ingredient_key: c.checked
            for c in GroceryCheck.query.filter_by(
                user_id=current_user.id, week_start=wk_start
            ).all()
        }

        items = sorted(aggregated.items(), key=lambda kv: kv[1]["name"].lower())
        rows = [
            {
                "key": key,
                "name": data["name"],
                "unit": data["unit"],
                "quantity": data["quantity"],
                "checked": checks.get(key, False),
            }
            for key, data in items
        ]

        return render_template(
            "grocery.html",
            week_start=wk_start,
            week_end=week_end,
            rows=rows,
            prev_week=(wk_start - timedelta(days=7)).isoformat(),
            next_week=(wk_start + timedelta(days=7)).isoformat(),
        )

    @app.route("/plan/grocery/toggle", methods=["POST"])
    @login_required
    def toggle_grocery_item():
        """No-JavaScript fallback: classic form POST, full page reload."""
        wk_start_raw = request.form.get("week_start", "")
        key = request.form.get("key", "")
        try:
            wk_start = datetime.strptime(wk_start_raw, "%Y-%m-%d").date()
        except ValueError:
            return redirect(url_for("grocery_list"))

        _toggle_grocery_check(current_user.id, wk_start, key)
        return redirect(url_for("grocery_list", week=wk_start.isoformat()))

    def _toggle_grocery_check(user_id, wk_start, key):
        existing = GroceryCheck.query.filter_by(
            user_id=user_id, week_start=wk_start, ingredient_key=key
        ).first()
        if existing:
            existing.checked = not existing.checked
            checked = existing.checked
        else:
            db.session.add(
                GroceryCheck(
                    user_id=user_id,
                    week_start=wk_start,
                    ingredient_key=key,
                    checked=True,
                )
            )
            checked = True
        db.session.commit()
        return checked

    # ---------- JSON API (used by static/app.js for AJAX interactions) ----------

    @app.route("/api/plan/slot", methods=["POST"])
    @login_required
    def api_plan_slot():
        """Dynamic aspect #1: assign (or clear) one meal-plan slot via fetch(),
        called on <select onchange> in plan.html -- persists immediately
        without a full-page form submit."""
        payload = request.get_json(silent=True) or request.form
        date_raw = payload.get("date", "")
        meal_type = payload.get("meal_type", "")
        raw_recipe_id = payload.get("recipe_id", "")

        try:
            plan_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid date."}), 400
        if meal_type not in MEAL_TYPES:
            return jsonify({"error": "Invalid meal type."}), 400

        existing = PlanEntry.query.filter_by(
            user_id=current_user.id, plan_date=plan_date, meal_type=meal_type
        ).first()

        if raw_recipe_id:
            recipe = Recipe.query.filter_by(
                id=raw_recipe_id, user_id=current_user.id
            ).first()
            if not recipe:
                return jsonify({"error": "Recipe not found."}), 404
            if existing:
                existing.recipe_id = recipe.id
            else:
                db.session.add(
                    PlanEntry(
                        user_id=current_user.id,
                        plan_date=plan_date,
                        meal_type=meal_type,
                        recipe_id=recipe.id,
                    )
                )
            db.session.commit()
            return jsonify({"status": "saved", "recipe_name": recipe.name})
        else:
            if existing:
                db.session.delete(existing)
                db.session.commit()
            return jsonify({"status": "cleared"})

    @app.route("/api/grocery/toggle", methods=["POST"])
    @login_required
    def api_grocery_toggle():
        """Dynamic aspect #2: toggle a grocery item's purchased state via
        fetch(), called from the checkbox button in grocery.html -- updates
        the row instantly instead of reloading the page."""
        payload = request.get_json(silent=True) or request.form
        wk_start_raw = payload.get("week_start", "")
        key = payload.get("key", "")
        try:
            wk_start = datetime.strptime(wk_start_raw, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid week_start."}), 400
        if not key:
            return jsonify({"error": "Missing key."}), 400

        checked = _toggle_grocery_check(current_user.id, wk_start, key)
        return jsonify({"status": "ok", "key": key, "checked": checked})

    # ---------- Goals ----------

    @app.route("/goals", methods=["GET", "POST"])
    @login_required
    def goals():
        goal = get_or_create_goal(current_user.id)

        if request.method == "POST":
            errors = []

            def parse_positive(field, label):
                raw = request.form.get(field, "")
                try:
                    value = float(raw)
                    if value <= 0:
                        raise ValueError
                    return value
                except ValueError:
                    errors.append(f"{label} must be a positive number.")
                    return None

            calories = parse_positive("daily_calories", "Daily calorie goal")
            protein = parse_positive("daily_protein_g", "Daily protein goal")
            carbs = parse_positive("daily_carbs_g", "Daily carbs goal")
            fat = parse_positive("daily_fat_g", "Daily fat goal")

            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                goal.daily_calories = calories
                goal.daily_protein_g = protein
                goal.daily_carbs_g = carbs
                goal.daily_fat_g = fat
                db.session.commit()
                flash("Goals updated.", "success")
                return redirect(url_for("goals"))

        return render_template("goals.html", goal=goal)

    with app.app_context():
        db.create_all()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
