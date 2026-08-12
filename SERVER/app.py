import json
import math
import os
import random
import statistics

from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# Only one game can be active at a time.
current_game = None

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LANGUAGES = {
    "en": {"label": "English", "file": "QuestionsAndActions_eng.json"},
    "de": {"label": "Deutsch", "file": "QuestionsAndAnswers_ger.json"},
}

COLOR_SCHEMES = {
    "red": {"label": "Red"},
    "blue": {"label": "Blue"},
    "green": {"label": "Green"},
    "pop": {"label": "Pop (colorful)"},
}


@app.context_processor
def inject_theme():
    return {"theme": current_game["color_scheme"] if current_game else "red"}

QUESTION_POOLS = {}
for lang_code, lang_info in LANGUAGES.items():
    with open(os.path.join(DATA_DIR, lang_info["file"]), encoding="utf-8") as f:
        QUESTION_POOLS[lang_code] = json.load(f)


def init_game_state(game):
    question_pool = QUESTION_POOLS[game["language"]]
    num_rounds = min(game["num_questions"], len(question_pool))
    questions = random.sample(question_pool, num_rounds)
    game["game_state"] = {
        "questions": questions,
        "rounds": [{"answers": {}, "predictions": {}} for _ in range(num_rounds)],
        "scores": {name: 0 for name in game["players"]},
    }


def ensure_game_state(game):
    if game.get("game_state") is None and len(game["players"]) >= game["num_players"]:
        init_game_state(game)
    return game.get("game_state")


def player_step(game, player_name, round_index):
    gs = game["game_state"]
    if round_index >= len(gs["questions"]):
        return "finished", round_index

    round_data = gs["rounds"][round_index]
    num_players = game["num_players"]

    if player_name not in round_data["answers"]:
        return "play", round_index
    if len(round_data["answers"]) < num_players:
        return "wait", round_index
    return "reveal", round_index


def guess_score(round_data, player_name):
    predictions = round_data["predictions"][player_name]
    return sum(
        max(0, 10 - abs(predicted - round_data["answers"][other]))
        for other, predicted in predictions.items()
    )


def own_score_breakdown(game, round_data, player_name):
    guesses_about_me = [
        round_data["predictions"][other][player_name]
        for other in game["players"]
        if other != player_name
    ]
    my_answer = round_data["answers"][player_name]

    if not guesses_about_me:
        return {"mean": None, "std": None, "my_answer": my_answer, "own_score": 0}

    mean = statistics.mean(guesses_about_me)
    std = max(statistics.pstdev(guesses_about_me), 1)
    z = (my_answer - mean) / std
    own_score = 10 * math.exp(-0.5 * z ** 2)

    return {
        "mean": mean,
        "std": std,
        "my_answer": my_answer,
        "own_score": own_score,
    }


def value_to_pct(value):
    return max(0, min(100, (value + 10) / 20 * 100))


def ratio_to_pct(value, max_value):
    if max_value <= 0:
        return 0
    return max(0, min(100, value / max_value * 100))


def award_round_scores(game, round_data):
    scores = game["game_state"]["scores"]
    for player_name in game["players"]:
        own = own_score_breakdown(game, round_data, player_name)
        scores[player_name] += round(own["own_score"]) + guess_score(round_data, player_name)


@app.route("/")
def index():
    return render_template("index.html", game_exists=current_game is not None)


@app.route("/manage")
def manage_game():
    return render_template(
        "manage.html",
        can_create=current_game is None,
        can_remove=current_game is not None,
    )


@app.route("/manage/remove", methods=["POST"])
def remove_game():
    global current_game
    if current_game is not None:
        current_game = None
    return redirect(url_for("manage_game"))


@app.route("/create", methods=["GET", "POST"])
def create_game():
    global current_game

    if current_game is not None:
        return redirect(url_for("manage_game"))

    if request.method == "GET":
        return render_template(
            "create.html",
            languages=LANGUAGES,
            language="de",
            num_players=3,
            num_questions=10,
            color_schemes=COLOR_SCHEMES,
            color_scheme="red",
        )

    password = request.form.get("password", "").strip()
    num_players = request.form.get("num_players", "").strip()
    num_questions = request.form.get("num_questions", "").strip()
    language = request.form.get("language", "").strip()
    color_scheme = request.form.get("color_scheme", "").strip()

    errors = []
    if not num_players.isdigit() or int(num_players) < 1:
        errors.append("Number of players must be a positive number.")
    if not num_questions.isdigit() or int(num_questions) < 1:
        errors.append("Number of questions must be a positive number.")
    if language not in LANGUAGES:
        errors.append("Please choose a valid language.")
    if color_scheme not in COLOR_SCHEMES:
        errors.append("Please choose a valid color scheme.")

    if errors:
        return render_template(
            "create.html",
            errors=errors,
            password=password,
            num_players=num_players,
            num_questions=num_questions,
            language=language,
            languages=LANGUAGES,
            color_scheme=color_scheme,
            color_schemes=COLOR_SCHEMES,
        )

    current_game = {
        "password": password,
        "num_players": int(num_players),
        "num_questions": int(num_questions),
        "language": language,
        "color_scheme": color_scheme,
        "players": [],
        "started": False,
    }

    return redirect(url_for("index"))


@app.route("/join", methods=["GET", "POST"])
def join_game():
    if request.method == "GET":
        if current_game is not None and not current_game["password"]:
            return redirect(url_for("welcome"))
        return render_template("join.html")

    password = request.form.get("password", "").strip()

    if current_game is None or password == "" or current_game["password"] != password:
        return render_template("join.html", error="No game found for that password.")

    return redirect(url_for("welcome"))


@app.route("/welcome", methods=["GET", "POST"])
def welcome():
    if current_game is None:
        return redirect(url_for("index"))

    player_name = session.get("player_name")
    if player_name and player_name in current_game["players"]:
        return redirect(url_for("waiting_room"))

    if request.method == "GET":
        return render_template("welcome.html", game=current_game)

    name = request.form.get("name", "").strip()

    if not name:
        return render_template("welcome.html", game=current_game, error="Please enter your name.")
    if name in current_game["players"]:
        return render_template("welcome.html", game=current_game, error="That name is already taken.")
    if len(current_game["players"]) >= current_game["num_players"]:
        return render_template("welcome.html", game=current_game, error="This game is already full.")

    current_game["players"].append(name)
    session["player_name"] = name
    session["round_index"] = 0

    return redirect(url_for("waiting_room"))


@app.route("/waiting")
def waiting_room():
    if current_game is None:
        return redirect(url_for("index"))

    player_name = session.get("player_name")
    if not player_name or player_name not in current_game["players"]:
        return redirect(url_for("welcome"))

    return render_template("waiting.html", game=current_game)


@app.route("/waiting/status")
def waiting_status():
    if current_game is None:
        return jsonify({"active": False})

    return jsonify(
        {
            "active": True,
            "players": current_game["players"],
            "num_players": current_game["num_players"],
            "ready": len(current_game["players"]) >= current_game["num_players"],
        }
    )


@app.route("/play")
def play():
    if current_game is None:
        return redirect(url_for("index"))

    player_name = session.get("player_name")
    if not player_name or player_name not in current_game["players"]:
        return redirect(url_for("welcome"))

    if len(current_game["players"]) < current_game["num_players"]:
        return redirect(url_for("waiting_room"))

    gs = ensure_game_state(current_game)
    round_index = session.get("round_index", 0)
    step, round_index = player_step(current_game, player_name, round_index)

    if step == "finished":
        standings = sorted(gs["scores"].items(), key=lambda kv: -kv[1])
        return render_template("scoreboard.html", game=current_game, standings=standings)

    question = gs["questions"][round_index]
    round_data = gs["rounds"][round_index]
    other_players = [p for p in current_game["players"] if p != player_name]

    context = {
        "game": current_game,
        "step": step,
        "question": question,
        "round_number": round_index + 1,
        "total_rounds": len(gs["questions"]),
        "other_players": other_players,
        "is_last_round": round_index + 1 >= len(gs["questions"]),
    }

    if step == "reveal":
        my_predictions = round_data["predictions"][player_name]
        reveal_rows = []
        for other in other_players:
            actual = round_data["answers"][other]
            predicted = my_predictions[other]
            points = max(0, 10 - abs(predicted - actual))
            reveal_rows.append({
                "name": other,
                "actual": actual,
                "predicted": predicted,
                "points": points,
                "actual_pct": value_to_pct(actual),
                "predicted_pct": value_to_pct(predicted),
                "line_left_pct": value_to_pct(min(actual, predicted)),
                "line_width_pct": value_to_pct(max(actual, predicted)) - value_to_pct(min(actual, predicted)),
            })

        guess_total = guess_score(round_data, player_name)
        own = own_score_breakdown(current_game, round_data, player_name)
        own_points = round(own["own_score"])

        context["reveal_rows"] = reveal_rows
        context["guess_total"] = guess_total
        context["own_answer"] = own["my_answer"]
        context["own_answer_pct"] = value_to_pct(own["my_answer"])
        context["own_mean"] = round(own["mean"], 1) if own["mean"] is not None else None
        context["own_std"] = round(own["std"], 1) if own["std"] is not None else None
        if own["mean"] is not None:
            band_low = own["mean"] - own["std"]
            band_high = own["mean"] + own["std"]
            context["own_band_left_pct"] = value_to_pct(band_low)
            context["own_band_width_pct"] = value_to_pct(band_high) - value_to_pct(band_low)
        context["own_points"] = own_points
        context["round_points"] = guess_total + own_points
        context["standings"] = sorted(gs["scores"].items(), key=lambda kv: -kv[1])

        num_players = current_game["num_players"]
        max_guess_per_round = 10 * (num_players - 1)
        scatter_points = []
        for p in current_game["players"]:
            if p == player_name:
                p_own_points, p_guess_points = own_points, guess_total
            else:
                p_own_points = round(own_score_breakdown(current_game, round_data, p)["own_score"])
                p_guess_points = guess_score(round_data, p)
            scatter_points.append({
                "name": p,
                "is_you": p == player_name,
                "x_pct": ratio_to_pct(p_own_points, 10),
                "y_pct": ratio_to_pct(p_guess_points, max_guess_per_round),
            })
        context["scatter_points"] = scatter_points

    return render_template("play.html", **context)


@app.route("/play/submit", methods=["POST"])
def play_submit():
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return redirect(url_for("index"))

    gs = ensure_game_state(current_game)
    round_index = session.get("round_index", 0)
    if gs is None or round_index >= len(gs["questions"]):
        return redirect(url_for("play"))

    round_data = gs["rounds"][round_index]
    if player_name not in round_data["answers"]:
        try:
            value = int(request.form.get("value", "0"))
        except ValueError:
            value = 0
        round_data["answers"][player_name] = max(-10, min(10, value))

        predictions = {}
        for other in current_game["players"]:
            if other == player_name:
                continue
            try:
                predicted = int(request.form.get(f"prediction_{other}", "0"))
            except ValueError:
                predicted = 0
            predictions[other] = max(-10, min(10, predicted))
        round_data["predictions"][player_name] = predictions

        if len(round_data["answers"]) == current_game["num_players"]:
            award_round_scores(current_game, round_data)

    return redirect(url_for("play"))


@app.route("/play/continue", methods=["POST"])
def play_continue():
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return redirect(url_for("index"))

    session["round_index"] = session.get("round_index", 0) + 1

    return redirect(url_for("play"))


@app.route("/play/status")
def play_status():
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return jsonify({"active": False})

    gs = ensure_game_state(current_game)
    if gs is None:
        return jsonify({"active": True, "step": "waiting", "round_index": -1})

    round_index = session.get("round_index", 0)
    step, round_index = player_step(current_game, player_name, round_index)
    return jsonify({"active": True, "step": step, "round_index": round_index})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
