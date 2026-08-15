import json
import os
import random
import statistics
import time

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# Only one game can be active at a time.
current_game = None

# Set while someone is on the create-game form, so a second player who clicks
# "Create Game" at the same moment gets bounced back instead of racing them.
creating_game_since = None
CREATE_LOCK_TIMEOUT_SECONDS = 120

FINISH_TIMER_SECONDS = 15
JOIN_TIMEOUT_SECONDS = 30

NUM_PLAYERS_OPTIONS = [2, 3, 4, 5, 6, 7, 8]
NUM_QUESTIONS_OPTIONS = [3, 5, 8, 10, 13, 15, 20]
ANSWER_TIME_OPTIONS = [5, 10, 15, 20, 25, 30, 40, 50, 60]
NUM_CHOICES_OPTIONS = [3, 5]

DEFAULT_NUM_PLAYERS = 3
DEFAULT_NUM_QUESTIONS = 5
DEFAULT_ANSWER_TIME = 15
DEFAULT_NUM_CHOICES = 5

# Points awarded for a guess, keyed by its distance from the actual answer.
# Slider values are one of -10/-5/0/5/10 (or just -10/0/10 for a 3-choice game),
# so distances are always a multiple of 5 between 0 and 20.
DISTANCE_POINTS = {0: 10, 5: 5, 10: 3, 15: 1, 20: 0}

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
LANGUAGES = {
    "de": {"label": "Deutsch", "file": "QuestionsAndAnswers_ger.json"},
    "en": {"label": "English", "file": "QuestionsAndActions_eng.json"},
}
DEFAULT_LANGUAGE = "de"

COLOR_SCHEMES = {
    "red": {"label": "Red"},
    "blue": {"label": "Blue"},
    "green": {"label": "Green"},
    "pop": {"label": "Pop (colorful)"},
}
DEFAULT_COLOR_SCHEME = "pop"


def default_settings():
    return {
        "num_players": DEFAULT_NUM_PLAYERS,
        "num_questions": DEFAULT_NUM_QUESTIONS,
        "answer_time": DEFAULT_ANSWER_TIME,
        "num_choices": DEFAULT_NUM_CHOICES,
        "color_scheme": DEFAULT_COLOR_SCHEME,
    }


# Remembers the last-used create-game settings so starting another round is quick.
last_settings = default_settings()


def is_creating():
    return (
        creating_game_since is not None
        and (time.time() - creating_game_since) < CREATE_LOCK_TIMEOUT_SECONDS
    )


@app.before_request
def expire_stale_game():
    global current_game
    if (
        current_game is not None
        and len(current_game["players"]) < current_game["num_players"]
        and time.time() >= current_game["created_at"] + JOIN_TIMEOUT_SECONDS
    ):
        current_game = None


@app.context_processor
def inject_theme():
    return {"theme": current_game["color_scheme"] if current_game else "red"}

QUESTION_POOLS = {}
for lang_code, lang_info in LANGUAGES.items():
    with open(os.path.join(DATA_DIR, lang_info["file"]), encoding="utf-8") as f:
        QUESTION_POOLS[lang_code] = json.load(f)

with open(os.path.join(DATA_DIR, "InGameStrings.json"), encoding="utf-8") as f:
    IN_GAME_STRINGS = {entry["id"]: entry for entry in json.load(f)}


def tr(string_id, lang, **kwargs):
    entry = IN_GAME_STRINGS.get(string_id)
    if entry is None:
        return string_id
    text = entry["text_ger"] if lang == "de" else entry["text_eng"]
    return text.format(**kwargs) if kwargs else text


def current_player_language():
    fallback = session.get("ui_language", "de")
    if current_game is not None:
        player_name = session.get("player_name")
        if player_name:
            return current_game["player_languages"].get(player_name, fallback)
    return fallback


@app.context_processor
def inject_translator():
    lang = current_player_language()
    return {
        "t": lambda string_id, **kwargs: tr(string_id, lang, **kwargs),
        "t_lang": tr,
        "language": lang,
        "logo_filename": "Logo_ger.jpg" if lang == "de" else "Logo_eng.jpg",
    }


def init_game_state(game):
    reference_pool = QUESTION_POOLS[DEFAULT_LANGUAGE]
    long_indices = [i for i, q in enumerate(reference_pool) if q["type"] == "long"]
    short_indices = [i for i, q in enumerate(reference_pool) if q["type"] == "short"]
    random.shuffle(long_indices)
    random.shuffle(short_indices)

    num_rounds = min(game["num_questions"], len(long_indices) + len(short_indices))

    question_indices = []
    long_pos = short_pos = 0
    want_long = True
    while len(question_indices) < num_rounds:
        if want_long and long_pos < len(long_indices):
            question_indices.append(long_indices[long_pos])
            long_pos += 1
        elif not want_long and short_pos < len(short_indices):
            question_indices.append(short_indices[short_pos])
            short_pos += 1
        elif long_pos < len(long_indices):
            question_indices.append(long_indices[long_pos])
            long_pos += 1
        elif short_pos < len(short_indices):
            question_indices.append(short_indices[short_pos])
            short_pos += 1
        else:
            break
        want_long = not want_long

    game["game_state"] = {
        "question_indices": question_indices,
        "rounds": [{"answers": {}, "predictions": {}, "continued": set()} for _ in range(len(question_indices))],
        "scores": {name: 0 for name in game["players"]},
    }


def ensure_game_state(game):
    if game.get("game_state") is None and len(game["players"]) >= game["num_players"]:
        init_game_state(game)
    return game.get("game_state")


def player_step(game, player_name, round_index):
    gs = game["game_state"]
    num_players = game["num_players"]

    while round_index < len(gs["question_indices"]):
        round_data = gs["rounds"][round_index]
        is_last_round = round_index + 1 >= len(gs["question_indices"])

        if player_name not in round_data["answers"]:
            return "play", round_index
        if len(round_data["answers"]) < num_players:
            return "wait", round_index
        if player_name not in round_data["continued"]:
            return "reveal", round_index
        if not is_last_round and len(round_data["continued"]) < num_players:
            return "wait_next", round_index

        round_index += 1

    return "finished", round_index


def guess_score(round_data, player_name):
    predictions = round_data["predictions"][player_name]
    points = [
        DISTANCE_POINTS.get(abs(predicted - round_data["answers"][other]), 0)
        for other, predicted in predictions.items()
    ]
    return statistics.mean(points) if points else 0


def own_score_breakdown(game, round_data, player_name):
    my_answer = round_data["answers"][player_name]
    guesses = [
        (other, round_data["predictions"][other][player_name])
        for other in game["players"]
        if other != player_name
    ]

    if not guesses:
        return {"my_answer": my_answer, "own_score": 0, "guesses": []}

    points = [DISTANCE_POINTS.get(abs(my_answer - guess), 0) for _, guess in guesses]
    own_score = statistics.mean(points)

    return {"my_answer": my_answer, "own_score": own_score, "guesses": guesses}


def value_to_pct(value):
    return max(0, min(100, (value + 10) / 20 * 100))


def waiting_on_set(round_data, step):
    if step == "wait":
        return round_data["answers"]
    if step == "wait_next":
        return round_data["continued"]
    return None


def award_round_scores(game, round_data):
    scores = game["game_state"]["scores"]
    two_player_game = game["num_players"] == 2
    for player_name in game["players"]:
        guess_points = round(guess_score(round_data, player_name))
        if two_player_game:
            scores[player_name] += guess_points
        else:
            own = own_score_breakdown(game, round_data, player_name)
            scores[player_name] += round(own["own_score"]) + guess_points


@app.route("/")
def index():
    return render_template(
        "index.html",
        game_exists=current_game is not None,
        creating=current_game is None and is_creating(),
        timer_start=current_game["created_at"] if current_game else None,
        timer_duration=JOIN_TIMEOUT_SECONDS,
    )


@app.route("/set_language", methods=["POST"])
def set_language():
    language = request.form.get("language", "").strip()
    if language in LANGUAGES:
        session["ui_language"] = language
    return jsonify({"ok": True})


@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/remove")
def remove_game():
    global current_game
    current_game = None
    return redirect(url_for("index"))


@app.route("/secret_remove")
def secret_remove():
    return render_template("secret_remove.html")


@app.route("/create", methods=["GET", "POST"])
def create_game():
    global current_game, last_settings, creating_game_since

    if current_game is not None:
        return redirect(url_for("index"))

    if request.method == "GET":
        if request.args.get("reset"):
            last_settings = default_settings()
        if not session.get("is_creator") and is_creating():
            return redirect(url_for("index"))
        creating_game_since = time.time()
        session["is_creator"] = True
        return render_template(
            "create.html",
            num_players_options=NUM_PLAYERS_OPTIONS,
            num_players=last_settings["num_players"],
            num_questions_options=NUM_QUESTIONS_OPTIONS,
            num_questions=last_settings["num_questions"],
            answer_time_options=ANSWER_TIME_OPTIONS,
            answer_time=last_settings["answer_time"],
            num_choices_options=NUM_CHOICES_OPTIONS,
            num_choices=last_settings["num_choices"],
            color_schemes=COLOR_SCHEMES,
            color_scheme=last_settings["color_scheme"],
        )

    if not session.get("is_creator") and is_creating():
        return redirect(url_for("index"))

    num_players = request.form.get("num_players", "").strip()
    num_questions = request.form.get("num_questions", "").strip()
    answer_time = request.form.get("answer_time", "").strip()
    num_choices = request.form.get("num_choices", "").strip()
    color_scheme = request.form.get("color_scheme", "").strip()

    errors = []
    if not num_players.isdigit() or int(num_players) not in NUM_PLAYERS_OPTIONS:
        errors.append("Please choose a valid number of players.")
    if not num_questions.isdigit() or int(num_questions) not in NUM_QUESTIONS_OPTIONS:
        errors.append("Please choose a valid number of questions.")
    if not answer_time.isdigit() or int(answer_time) not in ANSWER_TIME_OPTIONS:
        errors.append("Please choose a valid time to answer.")
    if not num_choices.isdigit() or int(num_choices) not in NUM_CHOICES_OPTIONS:
        errors.append("Please choose a valid number of choices.")
    if color_scheme not in COLOR_SCHEMES:
        errors.append("Please choose a valid color scheme.")

    if errors:
        return render_template(
            "create.html",
            errors=errors,
            num_players_options=NUM_PLAYERS_OPTIONS,
            num_players=num_players,
            num_questions_options=NUM_QUESTIONS_OPTIONS,
            num_questions=num_questions,
            answer_time_options=ANSWER_TIME_OPTIONS,
            answer_time=answer_time,
            num_choices_options=NUM_CHOICES_OPTIONS,
            num_choices=num_choices,
            color_scheme=color_scheme,
            color_schemes=COLOR_SCHEMES,
        )

    last_settings = {
        "num_players": int(num_players),
        "num_questions": int(num_questions),
        "answer_time": int(answer_time),
        "num_choices": int(num_choices),
        "color_scheme": color_scheme,
    }

    current_game = {
        "password": "",
        "num_players": int(num_players),
        "num_questions": int(num_questions),
        "answer_time": int(answer_time),
        "num_choices": int(num_choices),
        "color_scheme": color_scheme,
        "players": [],
        "player_languages": {},
        "started": False,
        "finished_players": set(),
        "finish_timer_start": None,
        "created_at": time.time(),
    }
    creating_game_since = None
    session.pop("is_creator", None)

    return redirect(url_for("index"))


@app.route("/create/cancel")
def cancel_create():
    global creating_game_since
    if session.get("is_creator"):
        creating_game_since = None
        session.pop("is_creator", None)
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
    language = session.get("ui_language", "de")

    if not name:
        return render_template("welcome.html", game=current_game, error=tr("error_enter_name", language))
    if name in current_game["players"]:
        return render_template("welcome.html", game=current_game, error=tr("error_name_taken", language))
    if len(current_game["players"]) >= current_game["num_players"]:
        return render_template("welcome.html", game=current_game, error=tr("error_game_full", language))

    current_game["players"].append(name)
    current_game["player_languages"][name] = language
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

    return render_template(
        "waiting.html",
        game=current_game,
        timer_start=current_game["created_at"],
        timer_duration=JOIN_TIMEOUT_SECONDS,
    )


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
            "timer_start": current_game["created_at"],
            "timer_duration": JOIN_TIMEOUT_SECONDS,
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
    session["round_index"] = round_index

    if step == "finished":
        if player_name in current_game["finished_players"]:
            return redirect(url_for("finish_wait"))

        if current_game.get("finish_timer_start") is None:
            current_game["finish_timer_start"] = time.time()

        standings = sorted(gs["scores"].items(), key=lambda kv: -kv[1])
        return render_template(
            "scoreboard.html",
            game=current_game,
            standings=standings,
            timer_start=current_game["finish_timer_start"],
            timer_duration=FINISH_TIMER_SECONDS,
        )

    player_language = current_game["player_languages"].get(player_name, DEFAULT_LANGUAGE)
    question = QUESTION_POOLS[player_language][gs["question_indices"][round_index]]
    round_data = gs["rounds"][round_index]
    other_players = [p for p in current_game["players"] if p != player_name]

    context = {
        "game": current_game,
        "step": step,
        "question": question,
        "round_number": round_index + 1,
        "total_rounds": len(gs["question_indices"]),
        "other_players": other_players,
        "is_last_round": round_index + 1 >= len(gs["question_indices"]),
        "timer_start": round_data.get("timer_start"),
        "timer_duration": current_game["answer_time"],
        "slider_step": 20 // (current_game["num_choices"] - 1),
    }

    done_set = waiting_on_set(round_data, step)
    if done_set is not None:
        context["player_statuses"] = [
            {"name": p, "done": p in done_set} for p in current_game["players"]
        ]
        context["done_count"] = len(done_set)

    if step == "reveal":
        two_player_game = current_game["num_players"] == 2

        my_predictions = round_data["predictions"][player_name]
        reveal_rows = [
            {
                "name": other,
                "actual_pct": value_to_pct(round_data["answers"][other]),
                "predicted_pct": value_to_pct(my_predictions[other]),
            }
            for other in other_players
        ]

        guess_total = round(guess_score(round_data, player_name))

        context["reveal_rows"] = reveal_rows
        context["guess_total"] = guess_total
        context["two_player_game"] = two_player_game

        if two_player_game:
            context["round_points"] = guess_total
        else:
            own = own_score_breakdown(current_game, round_data, player_name)
            own_points = round(own["own_score"])
            my_answer = own["my_answer"]

            own_rows = [
                {"name": other, "guess": guess, "guess_pct": value_to_pct(guess)}
                for other, guess in own["guesses"]
            ]

            context["own_answer"] = my_answer
            context["own_answer_pct"] = value_to_pct(my_answer)
            context["own_rows"] = own_rows
            context["own_points"] = own_points
            context["round_points"] = guess_total + own_points

        context["standings"] = sorted(gs["scores"].items(), key=lambda kv: -kv[1])

    return render_template("play.html", **context)


@app.route("/play/submit", methods=["POST"])
def play_submit():
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return redirect(url_for("index"))

    gs = ensure_game_state(current_game)
    round_index = session.get("round_index", 0)
    if gs is None or round_index >= len(gs["question_indices"]):
        return redirect(url_for("play"))

    round_data = gs["rounds"][round_index]
    if player_name not in round_data["answers"]:
        if not round_data["answers"]:
            round_data["timer_start"] = time.time()

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

    gs = ensure_game_state(current_game)
    round_index = session.get("round_index", 0)
    if gs is not None and round_index < len(gs["question_indices"]):
        gs["rounds"][round_index]["continued"].add(player_name)

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

    timer_start = None
    done_count = None
    if round_index < len(gs["question_indices"]):
        round_data = gs["rounds"][round_index]
        timer_start = round_data.get("timer_start")
        done_set = waiting_on_set(round_data, step)
        if done_set is not None:
            done_count = len(done_set)

    return jsonify({
        "active": True,
        "step": step,
        "round_index": round_index,
        "timer_start": timer_start,
        "timer_duration": current_game["answer_time"],
        "done_count": done_count,
    })


@app.route("/finish", methods=["POST"])
def finish_game():
    global current_game
    player_name = session.get("player_name")
    if current_game is None or not player_name or player_name not in current_game["players"]:
        return redirect(url_for("index"))

    current_game["finished_players"].add(player_name)

    if len(current_game["finished_players"]) >= current_game["num_players"]:
        current_game = None
        return redirect(url_for("index"))

    return redirect(url_for("finish_wait"))


@app.route("/finish/wait")
def finish_wait():
    if current_game is None:
        return redirect(url_for("index"))

    player_name = session.get("player_name")
    if not player_name or player_name not in current_game["players"]:
        return redirect(url_for("index"))

    if player_name not in current_game["finished_players"]:
        return redirect(url_for("play"))

    done_set = current_game["finished_players"]
    player_statuses = [{"name": p, "done": p in done_set} for p in current_game["players"]]

    return render_template(
        "finish_wait.html",
        game=current_game,
        player_statuses=player_statuses,
        done_count=len(done_set),
        timer_start=current_game.get("finish_timer_start"),
        timer_duration=FINISH_TIMER_SECONDS,
    )


@app.route("/finish/status")
def finish_status():
    if current_game is None:
        return jsonify({"active": False})

    return jsonify({
        "active": True,
        "done_count": len(current_game["finished_players"]),
        "timer_start": current_game.get("finish_timer_start"),
        "timer_duration": FINISH_TIMER_SECONDS,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
