import json
import os
import random

from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# Only one game can be active at a time.
current_game = None

QUESTIONS_PATH = os.path.join(os.path.dirname(__file__), "data", "QuestionsAndActions.json")
with open(QUESTIONS_PATH, encoding="utf-8") as f:
    QUESTION_POOL = json.load(f)


def init_game_state(game):
    num_rounds = min(game["num_questions"], len(QUESTION_POOL))
    questions = random.sample(QUESTION_POOL, num_rounds)
    game["game_state"] = {
        "questions": questions,
        "round_index": 0,
        "rounds": [{"answers": {}, "predictions": {}, "continued": []} for _ in range(num_rounds)],
        "scores": {name: 0 for name in game["players"]},
        "finished": False,
    }


def ensure_game_state(game):
    if game.get("game_state") is None and len(game["players"]) >= game["num_players"]:
        init_game_state(game)
    return game.get("game_state")


def player_step(game, player_name):
    gs = game["game_state"]
    if gs["finished"]:
        return "finished", gs["round_index"]

    round_index = gs["round_index"]
    round_data = gs["rounds"][round_index]
    num_players = game["num_players"]

    if player_name not in round_data["answers"]:
        return "answer", round_index
    if player_name not in round_data["predictions"]:
        return "predict", round_index
    if len(round_data["predictions"]) < num_players:
        return "wait_reveal", round_index
    if player_name not in round_data["continued"]:
        return "reveal", round_index
    return "wait_next", round_index


def award_round_scores(game, round_data):
    scores = game["game_state"]["scores"]
    for predictor, predictions in round_data["predictions"].items():
        round_total = 0
        for other, predicted in predictions.items():
            actual = round_data["answers"][other]
            round_total += 10 - abs(predicted - actual)
        scores[predictor] += round_total


@app.route("/")
def index():
    return render_template("index.html")


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
        return render_template("create.html")

    name = request.form.get("name", "").strip()
    password = request.form.get("password", "").strip()
    num_players = request.form.get("num_players", "").strip()
    num_questions = request.form.get("num_questions", "").strip()

    errors = []
    if not name:
        errors.append("Name of game is required.")
    if not num_players.isdigit() or int(num_players) < 1:
        errors.append("Number of players must be a positive number.")
    if not num_questions.isdigit() or int(num_questions) < 1:
        errors.append("Number of questions must be a positive number.")

    if errors:
        return render_template(
            "create.html",
            errors=errors,
            name=name,
            password=password,
            num_players=num_players,
            num_questions=num_questions,
        )

    current_game = {
        "name": name,
        "password": password,
        "num_players": int(num_players),
        "num_questions": int(num_questions),
        "players": [],
        "started": False,
    }

    return redirect(url_for("index"))


@app.route("/join", methods=["GET", "POST"])
def join_game():
    if request.method == "GET":
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
    step, round_index = player_step(current_game, player_name)

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
    }

    if step == "reveal":
        my_predictions = round_data["predictions"][player_name]
        reveal_rows = []
        round_points = 0
        for other in other_players:
            actual = round_data["answers"][other]
            predicted = my_predictions[other]
            points = 10 - abs(predicted - actual)
            round_points += points
            reveal_rows.append({"name": other, "actual": actual, "predicted": predicted, "points": points})

        context["reveal_rows"] = reveal_rows
        context["round_points"] = round_points
        context["total_score"] = gs["scores"][player_name]
        context["standings"] = sorted(gs["scores"].items(), key=lambda kv: -kv[1])

    return render_template("play.html", **context)


@app.route("/play/answer", methods=["POST"])
def play_answer():
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return redirect(url_for("index"))

    gs = ensure_game_state(current_game)
    if gs is None or gs["finished"]:
        return redirect(url_for("play"))

    round_data = gs["rounds"][gs["round_index"]]
    if player_name not in round_data["answers"]:
        try:
            value = int(request.form.get("value", "0"))
        except ValueError:
            value = 0
        round_data["answers"][player_name] = max(-10, min(10, value))

    return redirect(url_for("play"))


@app.route("/play/predict", methods=["POST"])
def play_predict():
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return redirect(url_for("index"))

    gs = ensure_game_state(current_game)
    if gs is None or gs["finished"]:
        return redirect(url_for("play"))

    round_data = gs["rounds"][gs["round_index"]]
    if player_name not in round_data["predictions"]:
        predictions = {}
        for other in current_game["players"]:
            if other == player_name:
                continue
            try:
                value = int(request.form.get(f"prediction_{other}", "0"))
            except ValueError:
                value = 0
            predictions[other] = max(-10, min(10, value))
        round_data["predictions"][player_name] = predictions

        if len(round_data["predictions"]) == current_game["num_players"]:
            award_round_scores(current_game, round_data)

    return redirect(url_for("play"))


@app.route("/play/continue", methods=["POST"])
def play_continue():
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return redirect(url_for("index"))

    gs = ensure_game_state(current_game)
    if gs is None or gs["finished"]:
        return redirect(url_for("play"))

    round_data = gs["rounds"][gs["round_index"]]
    if player_name not in round_data["continued"]:
        round_data["continued"].append(player_name)

    if len(round_data["continued"]) == current_game["num_players"]:
        if gs["round_index"] + 1 >= len(gs["questions"]):
            gs["finished"] = True
        else:
            gs["round_index"] += 1

    return redirect(url_for("play"))


@app.route("/play/status")
def play_status():
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return jsonify({"active": False})

    gs = ensure_game_state(current_game)
    if gs is None:
        return jsonify({"active": True, "step": "waiting", "round_index": -1})

    step, round_index = player_step(current_game, player_name)
    return jsonify({"active": True, "step": step, "round_index": round_index})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
