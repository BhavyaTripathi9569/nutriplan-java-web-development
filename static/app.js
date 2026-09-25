/**
 * NutriPlan front-end JavaScript.
 *
 * This file implements the app's two dynamic, backend-interacting
 * features required by the assignment brief:
 *
 *   1. Meal-plan autosave (plan.html): selecting a recipe in any grid
 *      cell POSTs to /api/plan/slot immediately and shows a small
 *      inline "Saved" confirmation, instead of requiring the user to
 *      click a page-wide submit button.
 *
 *   2. Grocery checkbox toggle (grocery.html): clicking an item's
 *      checkbox POSTs to /api/grocery/toggle and updates that row's
 *      styling instantly, instead of reloading the whole page.
 *
 * Both features degrade gracefully: if JavaScript is disabled, the
 * <form> elements in the templates still submit normally to the
 * classic (non-AJAX) Flask routes.
 */

function initPlanAutosave() {
  const selects = document.querySelectorAll("[data-plan-slot]");
  if (!selects.length) return;

  selects.forEach((select) => {
    select.addEventListener("change", async () => {
      const date = select.dataset.date;
      const mealType = select.dataset.mealType;
      const recipeId = select.value;
      const indicator = select.parentElement.querySelector(".save-indicator");

      if (indicator) {
        indicator.textContent = "Saving…";
        indicator.className = "save-indicator saving";
      }

      try {
        const res = await fetch("/api/plan/slot", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ date: date, meal_type: mealType, recipe_id: recipeId }),
        });
        if (!res.ok) throw new Error("Request failed");
        await res.json();
        if (indicator) {
          indicator.textContent = "Saved ✓";
          indicator.className = "save-indicator saved";
          setTimeout(() => {
            indicator.textContent = "";
            indicator.className = "save-indicator";
          }, 1500);
        }
      } catch (err) {
        if (indicator) {
          indicator.textContent = "Error — try Save button below";
          indicator.className = "save-indicator error";
        }
      }
    });
  });
}

function initGroceryToggle() {
  const buttons = document.querySelectorAll("[data-grocery-key]");
  if (!buttons.length) return;

  buttons.forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      const key = button.dataset.groceryKey;
      const weekStart = button.dataset.weekStart;
      const row = button.closest("tr");

      button.disabled = true;
      try {
        const res = await fetch("/api/grocery/toggle", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ week_start: weekStart, key: key }),
        });
        if (!res.ok) throw new Error("Request failed");
        const data = await res.json();
        button.textContent = data.checked ? "✓" : "";
        if (row) row.classList.toggle("checked-row", data.checked);
      } catch (err) {
        // Leave the row untouched and let the user retry; the form
        // fallback (page reload) still works if fetch is unavailable.
      } finally {
        button.disabled = false;
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initPlanAutosave();
  initGroceryToggle();
});
