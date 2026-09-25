"""
SQLAlchemy models for NutriPlan.

Six tables, all scoped to a single owning User so that every query in
app.py can filter by user_id and keep each account's data fully private:

    User             -- an account (auth via Flask-Login + hashed password)
    Recipe           -- a reusable recipe: name, servings, per-serving macros
    RecipeIngredient -- one ingredient line belonging to a Recipe
    PlanEntry        -- "this Recipe is assigned to this User's calendar
                         slot" (one day x one meal type)
    GroceryCheck     -- whether one aggregated grocery-list ingredient has
                         been purchased, for one User + one week
    Goal             -- a User's daily nutrition targets (one row per user)

Every relationship that hangs off a User or a Recipe uses
cascade="all, delete-orphan", so deleting a user or a recipe cleanly
removes everything that depended on it instead of leaving orphaned rows
or hitting a NOT NULL constraint (see the Phase 3 write-up for the bug
this caught: deleting a recipe that was already on the weekly plan).
"""

from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

MEAL_TYPES = ["Breakfast", "Lunch", "Dinner", "Snack"]


class User(db.Model, UserMixin):
    """An account. UserMixin supplies the is_authenticated/get_id() methods
    Flask-Login needs; the password itself is never stored in plain text."""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # cascade="all, delete-orphan": deleting a user removes their recipes,
    # plan entries, and goal row too, instead of leaving them behind.
    recipes = db.relationship(
        "Recipe", backref="user", lazy=True, cascade="all, delete-orphan"
    )
    plan_entries = db.relationship(
        "PlanEntry", backref="user", lazy=True, cascade="all, delete-orphan"
    )
    goal = db.relationship(
        "Goal", backref="user", uselist=False, cascade="all, delete-orphan"
    )

    def set_password(self, raw_password: str) -> None:
        """Hash and store a new password (Werkzeug's salted hash, not
        reversible) -- called on register and nowhere else stores raw text."""
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        """Verify a login attempt against the stored hash."""
        return check_password_hash(self.password_hash, raw_password)


class Recipe(db.Model):
    """One reusable recipe in a user's library. Calories/macros are stored
    per serving; the meal-plan and dashboard multiply by servings actually
    planned where relevant. Ingredients live in a separate table (below) so
    a recipe can have any number of them."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    servings = db.Column(db.Integer, nullable=False, default=1)
    # Macros are per serving.
    calories = db.Column(db.Float, nullable=False)
    protein_g = db.Column(db.Float, nullable=False, default=0)
    carbs_g = db.Column(db.Float, nullable=False, default=0)
    fat_g = db.Column(db.Float, nullable=False, default=0)

    # order_by keeps ingredients in the order they were entered, so the
    # "Ingredients (whole recipe, one per line)" textarea round-trips
    # predictably on the edit-recipe page.
    ingredients = db.relationship(
        "RecipeIngredient",
        backref="recipe",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="RecipeIngredient.id",
    )
    # Deleting a recipe that's already assigned to the weekly plan removes
    # those plan entries too, rather than leaving a plan slot pointing at a
    # recipe that no longer exists (see module docstring).
    plan_entries = db.relationship(
        "PlanEntry", backref="recipe", lazy=True, cascade="all, delete-orphan"
    )


class RecipeIngredient(db.Model):
    """One ingredient line ("name, quantity, unit") belonging to a single
    Recipe. This is the raw material the grocery-list aggregation reads:
    every RecipeIngredient for every Recipe planned in a given week is
    summed by (name, unit) into the grocery list."""

    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("recipe.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(20), nullable=False)


class PlanEntry(db.Model):
    """"This Recipe is assigned to this User, on this date, for this meal
    type" -- one row per filled slot in the weekly meal-plan grid. The
    unique constraint enforces at most one recipe per user/date/meal-type
    combination, matching the one-dropdown-per-cell UI."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recipe_id = db.Column(db.Integer, db.ForeignKey("recipe.id"), nullable=False)
    plan_date = db.Column(db.Date, nullable=False)
    meal_type = db.Column(db.String(20), nullable=False)  # one of MEAL_TYPES

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "plan_date", "meal_type", name="uq_user_date_meal"
        ),
    )


class GroceryCheck(db.Model):
    """Whether one ingredient on the auto-generated grocery list has been
    purchased, for one user and one week. The grocery list itself isn't
    stored -- it's computed on the fly from PlanEntry + RecipeIngredient --
    so this table only needs to remember the checkbox state per
    (user, week, ingredient) so it survives a page reload."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    week_start = db.Column(db.Date, nullable=False)  # the Monday of that week
    ingredient_key = db.Column(db.String(160), nullable=False)  # "name|unit", lowercased
    checked = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "week_start", "ingredient_key", name="uq_user_week_ingredient"
        ),
    )


class Goal(db.Model):
    """A user's daily nutrition targets -- one row per user (created with
    sensible defaults automatically on registration), shown against
    today's planned totals on the dashboard."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True
    )
    daily_calories = db.Column(db.Float, nullable=False, default=2000)
    daily_protein_g = db.Column(db.Float, nullable=False, default=100)
    daily_carbs_g = db.Column(db.Float, nullable=False, default=250)
    daily_fat_g = db.Column(db.Float, nullable=False, default=70)
