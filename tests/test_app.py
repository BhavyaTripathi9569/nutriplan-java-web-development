"""
Automated test suite for NutriPlan.

This is the "test data and test cases" deliverable referenced in the
Phase 2 slides: every scenario here runs as a real HTTP request against
the Flask app (via its test client) against a throwaway temp database,
so it can be re-run at any time with:

    pip install -r requirements.txt
    pip install pytest
    pytest tests/ -v

Coverage: account creation/auth, recipe CRUD, the weekly meal-plan grid
(both the classic form save and the AJAX /api/plan/slot endpoint), the
grocery-list aggregation logic (including the AJAX /api/grocery/toggle
endpoint), cascading deletes, and access control on protected routes.
"""
import datetime
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module
from models import Recipe, PlanEntry, GroceryCheck, Goal


@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    app_module.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + db_path
    app_module.app.config["TESTING"] = True

    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()

    with app_module.app.test_client() as c:
        yield c

    os.remove(db_path)


def register_and_login(client, username="bhavya", password="pw12345"):
    client.post(
        "/register",
        data={"username": username, "password": password, "confirm_password": password},
        follow_redirects=True,
    )
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=True
    )


# ---------------------------------------------------------------- auth ----

def test_register_and_login(client):
    resp = register_and_login(client)
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data


def test_duplicate_username_rejected(client):
    register_and_login(client)
    client.get("/logout")
    resp = client.post(
        "/register",
        data={"username": "bhavya", "password": "x", "confirm_password": "x"},
        follow_redirects=True,
    )
    assert b"already taken" in resp.data


def test_default_goal_autocreated(client):
    register_and_login(client)
    with app_module.app.app_context():
        goal = Goal.query.first()
        assert goal is not None
        assert goal.daily_calories == 2000


def test_protected_route_requires_login(client):
    resp = client.get("/dashboard", follow_redirects=True)
    assert b"Log in" in resp.data


# ------------------------------------------------------------- recipes ----

def add_recipe(client, name="Chicken Rice Bowl", ingredients="Chicken breast, 300, g\nRice, 200, g"):
    return client.post(
        "/recipes/add",
        data={
            "name": name,
            "servings": "2",
            "calories": "600",
            "protein_g": "40",
            "carbs_g": "60",
            "fat_g": "15",
            "ingredients": ingredients,
        },
        follow_redirects=True,
    )


def test_add_recipe(client):
    register_and_login(client)
    resp = add_recipe(client)
    assert b"Chicken Rice Bowl" in resp.data


def test_edit_recipe(client):
    register_and_login(client)
    add_recipe(client)
    with app_module.app.app_context():
        r = Recipe.query.filter_by(name="Chicken Rice Bowl").first()
    resp = client.post(
        f"/recipes/edit/{r.id}",
        data={
            "name": "Chicken Rice Bowl (updated)",
            "servings": "2",
            "calories": "620",
            "protein_g": "42",
            "carbs_g": "60",
            "fat_g": "16",
            "ingredients": "Chicken breast, 320, g\nRice, 200, g",
        },
        follow_redirects=True,
    )
    assert b"Chicken Rice Bowl (updated)" in resp.data


def test_delete_recipe_cascades_to_plan(client):
    register_and_login(client)
    add_recipe(client)
    with app_module.app.app_context():
        r = Recipe.query.filter_by(name="Chicken Rice Bowl").first()
        rid = r.id

    today = datetime.date.today().isoformat()
    client.post(
        "/api/plan/slot",
        json={"date": today, "meal_type": "Breakfast", "recipe_id": str(rid)},
    )
    client.post(f"/recipes/delete/{rid}", follow_redirects=True)

    with app_module.app.app_context():
        assert PlanEntry.query.filter_by(recipe_id=rid).first() is None


# ------------------------------------------------- weekly plan (AJAX) -----

def test_api_plan_slot_assigns_and_clears(client):
    register_and_login(client)
    add_recipe(client)
    with app_module.app.app_context():
        rid = Recipe.query.filter_by(name="Chicken Rice Bowl").first().id

    today = datetime.date.today().isoformat()

    resp = client.post(
        "/api/plan/slot",
        json={"date": today, "meal_type": "Lunch", "recipe_id": str(rid)},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "saved"

    with app_module.app.app_context():
        entry = PlanEntry.query.filter_by(meal_type="Lunch").first()
        assert entry is not None and entry.recipe_id == rid

    # Clearing the slot (empty recipe_id) should delete the entry.
    resp = client.post(
        "/api/plan/slot", json={"date": today, "meal_type": "Lunch", "recipe_id": ""}
    )
    assert resp.get_json()["status"] == "cleared"
    with app_module.app.app_context():
        assert PlanEntry.query.filter_by(meal_type="Lunch").first() is None


def test_plan_form_fallback_saves_whole_week(client):
    register_and_login(client)
    add_recipe(client)
    with app_module.app.app_context():
        rid = Recipe.query.filter_by(name="Chicken Rice Bowl").first().id

    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    resp = client.post(
        f"/plan?week={monday.isoformat()}",
        data={f"slot_{monday.isoformat()}_Breakfast": str(rid)},
        follow_redirects=True,
    )
    assert b"Meal plan saved" in resp.data


# --------------------------------------------------- grocery list (AJAX) --

def test_grocery_list_aggregates_ingredients(client):
    register_and_login(client)
    add_recipe(client, "Chicken Rice Bowl", "Rice, 200, g\nSoy sauce, 2, tbsp")
    add_recipe(client, "Fried Rice", "Rice, 150, g\nSoy sauce, 1, tbsp")
    with app_module.app.app_context():
        r1 = Recipe.query.filter_by(name="Chicken Rice Bowl").first().id
        r2 = Recipe.query.filter_by(name="Fried Rice").first().id

    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    client.post("/api/plan/slot", json={"date": monday.isoformat(), "meal_type": "Breakfast", "recipe_id": str(r1)})
    client.post("/api/plan/slot", json={"date": monday.isoformat(), "meal_type": "Lunch", "recipe_id": str(r2)})

    resp = client.get(f"/plan/grocery?week={monday.isoformat()}")
    assert b"350.0" in resp.data  # 200 + 150 g rice
    assert b"3.0" in resp.data    # 2 + 1 tbsp soy sauce


def test_api_grocery_toggle_persists(client):
    register_and_login(client)
    add_recipe(client, "Chicken Rice Bowl", "Rice, 200, g")
    with app_module.app.app_context():
        rid = Recipe.query.filter_by(name="Chicken Rice Bowl").first().id

    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    client.post("/api/plan/slot", json={"date": monday.isoformat(), "meal_type": "Breakfast", "recipe_id": str(rid)})

    resp = client.post(
        "/api/grocery/toggle", json={"week_start": monday.isoformat(), "key": "rice|g"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["checked"] is True

    with app_module.app.app_context():
        gc = GroceryCheck.query.filter_by(ingredient_key="rice|g").first()
        assert gc is not None and gc.checked is True


# ------------------------------------------------------------------ misc --

def test_update_goals(client):
    register_and_login(client)
    resp = client.post(
        "/goals",
        data={
            "daily_calories": "1900",
            "daily_protein_g": "110",
            "daily_carbs_g": "220",
            "daily_fat_g": "65",
        },
        follow_redirects=True,
    )
    assert b"1900" in resp.data


def test_logout(client):
    register_and_login(client)
    resp = client.get("/logout", follow_redirects=True)
    assert b"Log in" in resp.data
