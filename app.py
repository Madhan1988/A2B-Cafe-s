import os
import json
import re
from itertools import combinations
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data", "recipes.json")

# ---- Load data ----
with open(DATA_PATH, "r", encoding="utf-8") as f:
    RECIPES_RAW = json.load(f)

# normalize recipes: lowercase ingredients
RECIPES = {r: [ing.lower() for ing in ings] for r, ings in RECIPES_RAW.items()}

# ---- Substitutions (expand as you like) ----
SUBSTITUTIONS = {
    "butter": ["margarine", "oil"],
    "milk": ["almond milk", "soy milk", "water (with milk powder)"],
    "egg": ["flaxseed (1 tbsp ground + 3 tbsp water)", "chia seeds (same)"],
    "sugar": ["honey", "maple syrup"],
    "paneer": ["tofu"],
    "yogurt": ["curd", "buttermilk"],
    "cream": ["milk + butter"],
    "cheese": ["vegan cheese", "tofu (crumbled)"],
    "rice": ["quinoa (different flavor)"],
    "chicken": ["tofu (veg alternative)", "paneer (vegetarian, different flavor)"],
    "soy sauce": ["tamari", "coconut aminos"],
    "flour": ["almond flour (texture differs)"],
    "tomato": ["tinned tomato", "tomato paste + water"]
}

# ---- Synonyms / canonicalization ----
SYNONYMS = {
    "bell pepper": "capsicum",
    "scallion": "spring onion",
    "cilantro": "coriander",
    "curd": "yogurt",
    "corn flour": "cornstarch",
    "chick pea": "chickpeas",
    "kidney bean": "kidney beans"
}

# helper: normalize single ingredient text
def normalize_ing(ing: str) -> str:
    ing = ing.lower().strip()
    # remove extra spaces and articles
    ing = re.sub(r"\b(a|an|the)\b", "", ing).strip()
    # map synonyms
    if ing in SYNONYMS:
        ing = SYNONYMS[ing]
    return ing

# Build graphs (dicts)
def build_graph(recipes=RECIPES):
    ingredient_to_recipes = {}
    recipe_to_ingredients = {}
    for recipe, ings in recipes.items():
        recipe_to_ingredients[recipe] = set(ings)
        for ing in ings:
            ingredient_to_recipes.setdefault(ing, set()).add(recipe)
    return ingredient_to_recipes, recipe_to_ingredients

ING2REC, REC2ING = build_graph()

# ---- Suggestion functions ----
def parse_user_ingredients(raw_input: str):
    # split on commas, semicolons, newlines
    parts = re.split(r"[,\n;]+", raw_input)
    normalized = [normalize_ing(p) for p in parts if p.strip()]
    return list(dict.fromkeys(normalized))  # preserve order, unique

def suggest_recipes(available_ingredients, min_match=1, sort_by="matched_then_missing"):
    """
    Greedy ranking of recipes based on available ingredients.
    - available_ingredients: list of normalized ingredient strings
    - min_match: only include recipes with at least min_match matching ingredients
    - sort_by: "matched_then_missing" or "ratio" (match ratio)
    Returns list of dicts with metadata.
    """
    avail_set = set(available_ingredients)
    suggestions = []

    for recipe, ings in REC2ING.items():
        matched = ings & avail_set
        missing = ings - avail_set
        if len(matched) >= min_match:
            # prepare substitution suggestions for missing
            subs = {m: SUBSTITUTIONS.get(m, []) for m in missing}
            match_ratio = len(matched) / len(ings)
            suggestions.append({
                "recipe": recipe,
                "matched": sorted(list(matched)),
                "missing": sorted(list(missing)),
                "matched_count": len(matched),
                "missing_count": len(missing),
                "match_ratio": match_ratio,
                "substitutions": subs
            })

    if sort_by == "matched_then_missing":
        suggestions.sort(key=lambda x: (-x["matched_count"], x["missing_count"], -x["match_ratio"]))
    else:
        suggestions.sort(key=lambda x: (-x["match_ratio"], -x["matched_count"], x["missing_count"]))

    return suggestions

# ---- Backtracking: find best combination of recipes ----
def find_best_combo_backtracking(available_ingredients, max_recipes=3):
    """
    Backtracking search to find subset of recipes (size <= max_recipes)
    that maximizes total matched ingredients (sum). This is a combinatorial search.
    For simplicity, ingredients are considered reusable (we don't track counts).
    Returns the best subset and its score.
    """
    suggestions = suggest_recipes(available_ingredients, min_match=1)
    recipes_list = [s["recipe"] for s in suggestions]
    best = {"recipes": [], "score": -1}

    def score_subset(subset):
        # score = sum of matched_count in each recipe; we can prefer sets with diverse ingredients
        total = 0
        covered = set()
        for r in subset:
            ings = REC2ING[r]
            matched = ings & set(available_ingredients)
            # count unique matched ingredients across subset (prefer coverage)
            covered |= matched
            total += len(matched)
        # final score: primary unique coverage, secondary total matched
        return (len(covered), total)

    # try all combinations up to max_recipes
    for k in range(1, min(max_recipes, len(recipes_list)) + 1):
        for comb in combinations(recipes_list, k):
            sc = score_subset(comb)
            # lexicographic compare
            if sc > (best["score"] if isinstance(best["score"], tuple) else (-1, -1)):
                best["recipes"] = list(comb)
                best["score"] = sc

    return best

# ---- Greedy combo: pick recipes greedily by marginal benefit ----
def find_best_combo_greedy(available_ingredients, max_recipes=3):
    avail_set = set(available_ingredients)
    suggestions = suggest_recipes(available_ingredients, min_match=1)
    chosen = []
    covered = set()
    for _ in range(max_recipes):
        best = None
        best_marginal = -1
        for s in suggestions:
            if s["recipe"] in chosen:
                continue
            marginal = len((set(s["matched"]) | covered) - covered)  # new unique ingredients covered
            if marginal > best_marginal:
                best_marginal = marginal
                best = s
        if best is None or best_marginal == 0:
            break
        chosen.append(best["recipe"])
        covered |= set(best["matched"])
    # compute final score
    return {"recipes": chosen, "score": (len(covered), sum(len(REC2ING[r] & avail_set) for r in chosen))}

# ---- Flask routes ----

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        raw = request.form.get("ingredients", "")
        min_match = int(request.form.get("min_match", 1))
        sort_by = request.form.get("sort_by", "matched_then_missing")
        max_recipes = int(request.form.get("max_recipes", 3))
        available = parse_user_ingredients(raw)
        suggestions = suggest_recipes(available, min_match=min_match, sort_by=sort_by)
        best_bt = find_best_combo_backtracking(available, max_recipes=max_recipes)
        best_gr = find_best_combo_greedy(available, max_recipes=max_recipes)
        return render_template("results.html",
                               ingredients=available,
                               suggestions=suggestions,
                               best_bt=best_bt,
                               best_gr=best_gr)
    # GET
    return render_template("index.html")

# Optional JSON API for integrations / frontend
@app.route("/api/suggest", methods=["POST"])
def api_suggest():
    data = request.get_json(force=True)
    raw = data.get("ingredients", "")
    min_match = int(data.get("min_match", 1))
    sort_by = data.get("sort_by", "matched_then_missing")
    max_recipes = int(data.get("max_recipes", 3))
    available = parse_user_ingredients(raw)
    suggestions = suggest_recipes(available, min_match=min_match, sort_by=sort_by)
    best_bt = find_best_combo_backtracking(available, max_recipes=max_recipes)
    best_gr = find_best_combo_greedy(available, max_recipes=max_recipes)
    return jsonify({
        "ingredients": available,
        "suggestions": suggestions,
        "best_backtracking": best_bt,
        "best_greedy": best_gr
    })


if __name__ == "__main__":
    app.run(debug=True)
