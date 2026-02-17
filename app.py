# =================================================================
# LIBRARIES
# =================================================================
import os
import logging
import random
import string
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    current_user,
    logout_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# =================================================================
# CONFIGURATION
# =================================================================
load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Essential Environment Variables
app.secret_key = os.environ.get("SECRET_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
MASTER_INVITE_CODE = os.environ.get("MASTER_INVITE_CODE", "Teams2026$kL9p!")

if not DATABASE_URL:
    raise ValueError("ERROR: DATABASE_URL not found in .env file!")

if not app.secret_key:
    # Fail-safe to ensure sessions work
    app.secret_key = "fallback-key-if-env-fails-but-fix-your-env"

# Flask Session Settings
app.config.update(
    SESSION_COOKIE_NAME="teams_auth_v2",
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # Set to True if using HTTPS
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=1800,
)

# =================================================================
# UTILITIES
# =================================================================


def get_db_connection():
    """Establishes connection to the PostgreSQL database."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def generate_invite_code(length=6):
    """Generates a random uppercase alphanumeric string."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


# =================================================================
# AUTHENTICATION MODELS & HELPERS
# =================================================================
class User(UserMixin):
    def __init__(self, user_id, username, is_admin):
        self.id = str(user_id)
        self.username = username
        # Force Admin status to True if user_id is 1, otherwise use DB value
        self.is_admin = True if self.id == "1" else bool(is_admin)


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Fetch id, username, and is_admin
        cur.execute(
            "SELECT id, username, is_admin FROM users WHERE id = %s", (int(user_id),)
        )
        row = cur.fetchone()
        if row:
            # The User class constructor will handle the super-admin logic for ID 1
            return User(row["id"], row["username"], row["is_admin"])
        return None
    finally:
        conn.close()


@app.before_request
def require_login():
    # List of endpoints that don't require logging in
    # ADD 'register' TO THIS LIST
    allowed_routes = ["login", "register", "static"]

    if not current_user.is_authenticated and request.endpoint not in allowed_routes:
        return redirect(url_for("login"))


@app.route("/favicon.ico")
def favicon():
    return "", 204


# =================================================================
# 1. AUTHENTICATION ROUTES
# =================================================================


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pw_input = request.form.get("password", "").strip()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, password_hash, is_admin FROM users WHERE username = %s",
            (username,),
        )
        db_user = cur.fetchone()

        if db_user and check_password_hash(db_user["password_hash"], pw_input):
            user_obj = User(db_user["id"], db_user["username"], db_user["is_admin"])
            login_user(user_obj)

            cur.execute(
                "SELECT game_id FROM game_members WHERE user_id = %s", (db_user["id"],)
            )
            memberships = cur.fetchall()
            conn.close()

            if len(memberships) == 1:
                # Single Game User: Set the session and go straight to Players
                session["active_game_id"] = memberships[0]["game_id"]
                return redirect(url_for("players_page_render"))
            else:
                # Multi-Game User: Go to Players to let them pick
                return redirect(url_for("players_page_render"))

        # IMPORTANT: Close connection and return something if login fails
        if conn:
            conn.close()
        return "Invalid username or password", 401

    # This handles the GET request (initial page load)
    return render_template("login.html")


@app.route("/")
@login_required
def index():
    # This ensures that entering the base URL "/" sends users to the Players hub
    return redirect(url_for("players_page_render"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        code = request.form.get("code")

        if not username or not password or not code:
            return "Missing fields", 400

        # 1. Determine Identity & Global Role
        is_global_admin = code == MASTER_INVITE_CODE

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Check if user exists
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                return "Username already exists", 400

            # Create User
            hashed_pw = generate_password_hash(password)
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (%s, %s, %s) RETURNING id",
                (username, hashed_pw, is_global_admin),
            )
            user_id = cur.fetchone()["id"]

            # 2. Handle Membership for Non-Global Admins
            if not is_global_admin:
                cur.execute("SELECT id FROM game_names WHERE invite_code = %s", (code,))
                game = cur.fetchone()
                if not game:
                    conn.rollback()
                    return "Invalid Invite Code", 400

                # Add to game_members as 'Read-only'
                cur.execute(
                    "INSERT INTO game_members (user_id, game_id, role) VALUES (%s, %s, 'Read-only')",
                    (user_id, game["id"]),
                )

            conn.commit()
            return redirect(url_for("login"))
        finally:
            conn.close()
    return render_template("login.html")  # Or register.html if you have it


@app.route("/api/join_game", methods=["POST"])
@login_required
def join_game():
    code = request.json.get("code", "").strip().upper()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Find the game with this code
        cur.execute("SELECT id FROM game_names WHERE invite_code = %s", (code,))
        game = cur.fetchone()

        if game:
            # Link user to game (ignore if already linked)
            cur.execute(
                """
                INSERT INTO game_members (user_id, game_id) 
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """,
                (current_user.id, game["id"]),
            )
            conn.commit()
            return jsonify({"ok": True})
        else:
            return jsonify({"error": "Invalid Invite Code"}), 404
    finally:
        conn.close()


@app.route("/api/set_active_game/<int:game_id>", methods=["POST"])
@login_required
def set_active_game(game_id):
    session["active_game_id"] = game_id
    # This line ensures the session is saved so the 'if' statement in base.html sees it
    session.modified = True
    return jsonify({"ok": True})


@app.route("/api/update_member_role", methods=["POST"])
@login_required
def update_member_role():
    data = request.json
    target_username = data.get("username")
    new_role = data.get("role")
    game_id = session.get("active_game_id")

    if not all([target_username, new_role, game_id]):
        return jsonify({"error": "Missing data"}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. AUTH CHECK: Is the CURRENT user an admin for THIS game?
        cur.execute(
            """
            SELECT role FROM game_members 
            WHERE user_id = %s AND game_id = %s
        """,
            (current_user.id, game_id),
        )
        user_row = cur.fetchone()

        if not user_row or user_row["role"] != "Admin":
            return (
                jsonify({"error": "Unauthorized. Only game admins can change roles."}),
                403,
            )

        # 2. Get the ID of the user we are changing
        cur.execute("SELECT id FROM users WHERE username = %s", (target_username,))
        target_user = cur.fetchone()
        if not target_user:
            return jsonify({"error": "Target user not found"}), 404

        # 3. Prevent self-demotion (Optional Safety)
        if target_user["id"] == current_user.id:
            return jsonify({"error": "You cannot change your own role."}), 400

        # 4. Update the role
        cur.execute(
            """
            UPDATE game_members 
            SET role = %s 
            WHERE user_id = %s AND game_id = %s
        """,
            (new_role, target_user["id"], game_id),
        )

        conn.commit()
        return jsonify({"success": True, "new_role": new_role})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/group")
@login_required
def group_page():
    game_id = session.get("active_game_id")
    if not game_id:
        return redirect(url_for("players_page_render"))

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Fetch the actual game name
        cur.execute("SELECT name FROM game_names WHERE id = %s", (game_id,))
        game_row = cur.fetchone()
        game_name = game_row["name"] if game_row else "Unknown Game"

        # 2. Fetch the members list for this game
        cur.execute(
            """
            SELECT u.username, m.role 
            FROM game_members m
            JOIN users u ON m.user_id = u.id
            WHERE m.game_id = %s
            ORDER BY u.username ASC
        """,
            (game_id,),
        )
        members = cur.fetchall()

        # 3. Logic for determining if user can edit roles (Your Admin Bypass)
        if str(current_user.id) == "1":
            is_page_admin = True
        else:
            cur.execute(
                "SELECT role FROM game_members WHERE game_id = %s AND user_id = %s",
                (game_id, current_user.id),
            )
            user_member_data = cur.fetchone()
            is_page_admin = user_member_data and user_member_data["role"] == "Admin"

        return render_template(
            "group.html",
            members=members,
            game_name=game_name,
            is_page_admin=is_page_admin,
        )
    finally:
        conn.close()


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# =================================================================
# 2. PLAYER & GAME SETTINGS
# =================================================================


@app.route("/players", methods=["GET"])
@login_required
def players_page_render():
    # Pass the current session's active game to the template
    return render_template("players.html", active_game_id=session.get("active_game_id"))


@app.route("/api/games")
@login_required
def api_games():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # This already selects invite_code
        cur.execute(
            """
            SELECT g.id, g.name, g.invite_code 
            FROM game_names g
            JOIN game_members m ON g.id = m.game_id
            WHERE m.user_id = %s
            ORDER BY LOWER(g.name)
        """,
            (current_user.id,),
        )
        res = cur.fetchall()
        return jsonify(res)
    finally:
        conn.close()


@app.route("/api/players/<int:game_id>", methods=["GET"])
@login_required
def api_players(game_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.name, COALESCE(rv.rank, 0) as rank
            FROM players p
            LEFT JOIN rankview rv ON LOWER(p.name) = LOWER(rv.player)
            WHERE p.game_id = %s AND p.active = 1
            ORDER BY LOWER(p.name) ASC  -- <--- CHANGE THIS LINE
        """,
            (game_id,),
        )
        res = cur.fetchall()
        return jsonify(res)
    finally:
        conn.close()


@app.route("/api/players", methods=["POST"])
@login_required
def api_players_upsert():
    if not current_user.is_admin:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    players = data.get("players", [])
    game_id = data.get("game_id")
    game_name = data.get("game_name")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Handle New Game Creation with a 10-digit code logic
        if not game_id:
            # Generate a fresh code (e.g., P6S782WL90 style or random)
            new_code = generate_invite_code(10)

            cur.execute(
                """
                INSERT INTO game_names (name, owner_user_id, invite_code) 
                VALUES (%s, %s, %s) RETURNING id
            """,
                (game_name, current_user.id, new_code),
            )
            game_id = cur.fetchone()["id"]

            # Auto-link the creator to the game
            cur.execute(
                """
                INSERT INTO game_members (user_id, game_id, role) 
                VALUES (%s, %s, 'Admin')
            """,
                (current_user.id, game_id),
            )

        # 2. Sync Player List
        # Deactivate all current players for this game
        cur.execute("UPDATE players SET active = 0 WHERE game_id = %s", (game_id,))

        # Reactivate or Insert new names
        for pname in players:
            pname_clean = pname.strip()
            if pname_clean:
                cur.execute(
                    """
                    INSERT INTO players (name, game_id, active) VALUES (%s, %s, 1) 
                    ON CONFLICT (name, game_id) DO UPDATE SET active = 1
                """,
                    (pname_clean, game_id),
                )

        conn.commit()
        response_data = {"id": game_id, "status": "success"}
        if "new_code" in locals():
            response_data["invite_code"] = new_code

        return jsonify(response_data), 200

    except Exception as e:
        conn.rollback()
        logging.error(f"Error in api_players_upsert: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# =================================================================
# 3. TEAM GENERATION & MATCH MANAGEMENT
# =================================================================


@app.route("/teams")
@login_required
def teams_page():
    # 1. Verify the user has selected a game in the session
    game_id = session.get("active_game_id")

    # 2. Safety: If no game is active, send them back to pick one
    if not game_id:
        return redirect(url_for("players_page_render"))

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # 3. Fetch the roster and ranks for the SPECIFIC active game
        cur.execute(
            """
            SELECT p.name as player, COALESCE(rv.rank, 0) as rank
            FROM players p
            LEFT JOIN rankview rv ON LOWER(p.name) = LOWER(rv.player)
            WHERE p.active = 1 AND p.game_id = %s 
            ORDER BY LOWER(p.name)
        """,
            (game_id,),
        )
        players = cur.fetchall()
        # 4. Render the specific teams template
        return render_template("teams.html", players=players)
    finally:
        conn.close()


@app.route("/generate_teams", methods=["POST"])
@login_required
def generate_teams():
    data = request.get_json()
    selected_names = data.get("players", [])
    if not selected_names:
        return jsonify({"error": "No players selected"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.name as player, COALESCE(rv.rank, 0) as rank
        FROM players p
        LEFT JOIN rankview rv ON LOWER(p.name) = LOWER(rv.player)
        WHERE p.name = ANY(%s) AND p.active = 1
    """,
        (selected_names,),
    )

    pool = cur.fetchall()
    conn.close()

    if not pool:
        return jsonify({"error": "No valid players found"}), 400

    half_size = len(pool) // 2

    best_t1, best_t2 = [], []
    min_diff = float("inf")
    best_sum1, best_sum2 = 0, 0

    # Best of 100 to find the most balanced split
    for _ in range(100):
        shuffled_pool = list(pool)
        random.shuffle(shuffled_pool)

        temp_t1 = shuffled_pool[:half_size]
        temp_t2 = shuffled_pool[half_size:]

        sum1 = sum(p["rank"] for p in temp_t1)
        sum2 = sum(p["rank"] for p in temp_t2)
        current_diff = abs(sum1 - sum2)

        if current_diff < min_diff:
            min_diff = current_diff
            best_t1, best_t2 = temp_t1, temp_t2
            best_sum1, best_sum2 = sum1, sum2

        if min_diff <= 1:
            break

    # --- FIX: Sort by Rank Ascending (1, 2, 3...) then Name A-Z ---
    best_t1.sort(key=lambda x: (x.get("rank", 0), x.get("player", "").lower()))
    best_t2.sort(key=lambda x: (x.get("rank", 0), x.get("player", "").lower()))

    return jsonify(
        {
            "team1": best_t1,
            "team2": best_t2,
            "total1": best_sum1,
            "total2": best_sum2,
            "difference": min_diff,
        }
    )


@app.route("/swap_locked_players", methods=["POST"])
@login_required
def swap_locked_players():
    """Manually swaps players between teams in the locked_teams table."""
    data = request.json
    date_str, p1, p2 = data.get("date"), data.get("p1"), data.get("p2")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE locked_teams SET team_name = %s WHERE date = %s AND player_id = %s",
            (p1["team"], date_str, p1["id"]),
        )
        cur.execute(
            "UPDATE locked_teams SET team_name = %s WHERE date = %s AND player_id = %s",
            (p2["team"], date_str, p2["id"]),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/lock_teams", methods=["POST"])
@login_required
def lock_teams():
    if not current_user.is_admin:
        return jsonify({"error": "Admin access required"}), 403

    """Saves the final team composition for a specific date."""
    data = request.get_json()
    game_date, team1, team2 = (
        data.get("date"),
        data.get("team1", []),
        data.get("team2", []),
    )
    if not game_date:
        return jsonify({"error": "Missing date"}), 400
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM locked_teams WHERE date = %s", (game_date,))

        def save(players, team):
            for i, p in enumerate(players):
                cur.execute(
                    "SELECT id, game_id FROM players WHERE name = %s", (p["player"],)
                )
                res = cur.fetchone()
                if res:
                    cur.execute(
                        """INSERT INTO locked_teams (date, game_id, player_id, team_name, slot, locked_by)
                                   VALUES (%s, %s, %s, %s, %s, %s)""",
                        (
                            game_date,
                            res["game_id"],
                            res["id"],
                            team,
                            i,
                            current_user.id,
                        ),
                    )

        save(team1, "Orange")
        save(team2, "Yellow")
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


@app.route("/unlock_teams", methods=["POST"])
@login_required
def unlock_teams():
    if not current_user.is_admin:
        return jsonify({"error": "Admin access required"}), 403

    """Clears team locks for a specific date."""
    data = request.json
    date_str = data.get("date") or data.get("Date")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM locked_teams WHERE date = %s", (date_str,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# =================================================================
# 4. RESULTS & STANDINGS
# =================================================================


@app.route("/get_locked_teams")
@login_required
def get_locked_teams():
    if not current_user.is_admin:
        return jsonify({"error": "Admin access required"}), 403

    """Loads locked players to populate the Results entry form."""
    date_str = request.args.get("date") or request.args.get("Date")
    if not date_str:
        return jsonify({"error": "No date provided"}), 400
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT lt.team_name, p.id, p.id as player_id, p.name FROM locked_teams lt 
                   JOIN players p ON lt.player_id = p.id WHERE lt.date = %s
                   ORDER BY lt.team_name ASC, p.name ASC""",
        (date_str,),
    )
    rows = cur.fetchall()
    conn.close()
    teams = {"Orange": [], "Yellow": []}
    for r in rows:
        teams[r["team_name"]].append(
            {"id": r["id"], "player_id": r["player_id"], "name": r["name"]}
        )
    return jsonify({"success": True, "teams": teams, "Date": date_str})


@app.route("/submit-results", methods=["GET", "POST"])
@login_required
def results_entry_page():
    if not current_user.is_admin:
        return jsonify({"error": "Admin access required"}), 403

    """Submits match results with point validation rules."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        date_str, player_results = data.get("Date") or data.get("date"), data.get(
            "results", []
        )
        total_points = sum(int(r.get("points", 0)) for r in player_results)
        num_players = len(player_results)

        # Validation Logic
        if num_players == 10 and total_points not in [10, 15]:
            return (
                jsonify(
                    {
                        "error": f"10 players must have 10 or 15 total points (Current: {total_points})"
                    }
                ),
                400,
            )
        if num_players == 12 and total_points not in [12, 18]:
            return (
                jsonify(
                    {
                        "error": f"12 players must have 12 or 18 total points (Current: {total_points})"
                    }
                ),
                400,
            )

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            for res in player_results:
                cur.execute(
                    "SELECT game_id FROM players WHERE id = %s", (res["player_id"],)
                )
                game_id = cur.fetchone()["game_id"] if cur.rowcount > 0 else 1
                cur.execute(
                    """INSERT INTO results (match_date, game_id, player_id, points, submitted_by)
                               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (player_id, match_date) 
                               DO UPDATE SET points = EXCLUDED.points, submitted_by = EXCLUDED.submitted_by""",
                    (
                        date_str,
                        game_id,
                        res["player_id"],
                        res["points"],
                        current_user.id,
                    ),
                )
            conn.commit()
            return jsonify({"ok": True})
        finally:
            conn.close()
    return render_template("submit_results.html")


@app.route("/submit-results")
@login_required
def submit_results_page():
    game_id = session.get("active_game_id")
    if not game_id:
        return redirect(url_for("players_page_render"))

    conn = get_db_connection()
    cur = conn.cursor()
    # Ensure rankview filtering logic exists or join with players
    cur.execute(
        "SELECT * FROM rankview WHERE game_id = %s ORDER BY points DESC", (game_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return render_template("results_page.html", rankview=rows)


@app.route("/statistics")
@login_required
def statistics_page():
    """Statistics page with meaningful metrics"""
    game_id = session.get("active_game_id")
    if not game_id:
        return redirect(url_for("players_page_render"))

    conn = get_db_connection()
    cur = conn.cursor()

    # Initialize variables
    total_matches = 0
    active_players = 0
    most_active = {"name": "N/A", "games": 0, "tied_players": []}
    win_rates = []
    recent_matches = []
    longest_game_streak = {"player": "N/A", "streak": 0}
    longest_win_streak = {"player": "N/A", "streak": 0}
    longest_losing_streak = {"player": "N/A", "streak": 0}
    best_pairings = []
    fun_facts = []

    try:
        # 1. TOTAL MATCHES
        cur.execute(
            """
            SELECT COUNT(DISTINCT match_date) as total 
            FROM results 
            WHERE game_id = %s AND points IS NOT NULL
        """,
            (game_id,),
        )
        row = cur.fetchone()
        total_matches = row["total"] if row else 0

        # 2. ACTIVE PLAYERS
        cur.execute(
            """
            SELECT COUNT(DISTINCT player_id) as active 
            FROM results 
            WHERE game_id = %s AND points IS NOT NULL
        """,
            (game_id,),
        )
        row = cur.fetchone()
        active_players = row["active"] if row else 0

        # 3. MOST ACTIVE PLAYER (WITH TIE HANDLING)
        cur.execute(
            """
            SELECT p.name, COUNT(DISTINCT r.match_date) as games 
            FROM results r 
            JOIN players p ON r.player_id = p.id 
            WHERE r.game_id = %s AND r.points IS NOT NULL
            GROUP BY p.id, p.name 
            ORDER BY games DESC
        """,
            (game_id,),
        )
        all_active = cur.fetchall()
        if all_active:
            max_games = all_active[0]["games"]
            tied_players = [p["name"] for p in all_active if p["games"] == max_games]
            most_active = {
                "name": tied_players[0],
                "games": max_games,
                "tied_players": tied_players,
            }

        # 4. WIN RATES (FIXED)
        cur.execute(
            """
            SELECT 
                p.name, 
                COUNT(DISTINCT r.match_date) as games_played,
                SUM(CASE WHEN r.points = 3 THEN 1 ELSE 0 END) as wins
            FROM players p
            JOIN results r ON p.id = r.player_id
            WHERE p.game_id = %s 
                AND p.active = 1 
                AND r.points IS NOT NULL
            GROUP BY p.id, p.name
            HAVING COUNT(DISTINCT r.match_date) > 0
            ORDER BY (CAST(SUM(CASE WHEN r.points = 3 THEN 1 ELSE 0 END) AS FLOAT) / COUNT(DISTINCT r.match_date)) DESC
        """,
            (game_id,),
        )

        for row in cur.fetchall():
            rate = (
                (row["wins"] / row["games_played"] * 100)
                if row["games_played"] > 0
                else 0
            )
            win_rates.append(
                {
                    "name": row["name"],
                    "games_played": row["games_played"],
                    "wins": row["wins"],
                    "win_rate": round(rate, 1),
                }
            )

        # 5. RECENT MATCHES
        cur.execute(
            """
            SELECT match_date, SUM(points) as total_points
            FROM results 
            WHERE game_id = %s AND points IS NOT NULL
            GROUP BY match_date 
            ORDER BY match_date DESC 
            LIMIT 5
        """,
            (game_id,),
        )
        recent_matches = cur.fetchall() or []

        # 7. LONGEST GAME STREAK
        cur.execute(
            """
            SELECT p.name, COUNT(DISTINCT r.match_date) as streak
            FROM players p
            JOIN results r ON p.id = r.player_id
            WHERE p.game_id = %s AND r.points IS NOT NULL
            GROUP BY p.id, p.name
            ORDER BY streak DESC
            LIMIT 1
        """,
            (game_id,),
        )
        row = cur.fetchone()
        if row:
            longest_game_streak = {"player": row["name"], "streak": row["streak"]}

        # 8. LONGEST WIN STREAK
        try:
            cur.execute(
                """
                WITH player_games AS (
                    SELECT 
                        p.id,
                        p.name,
                        r.match_date,
                        CASE WHEN r.points = 3 THEN 1 ELSE 0 END as is_win,
                        ROW_NUMBER() OVER (PARTITION BY p.id ORDER BY r.match_date) as rn
                    FROM players p
                    JOIN results r ON p.id = r.player_id
                    WHERE p.game_id = %s AND r.points IS NOT NULL
                ),
                streaks AS (
                    SELECT 
                        name,
                        SUM(is_win) as streak
                    FROM (
                        SELECT 
                            name,
                            is_win,
                            SUM(CASE WHEN is_win = 0 THEN 1 ELSE 0 END) 
                                OVER (PARTITION BY id ORDER BY rn) as grp
                        FROM player_games
                    ) x
                    WHERE is_win = 1
                    GROUP BY name, grp
                )
                SELECT name, MAX(streak) as streak
                FROM streaks
                GROUP BY name
                ORDER BY streak DESC
                LIMIT 1
            """,
                (game_id,),
            )
            row = cur.fetchone()
            if row and row["streak"]:
                longest_win_streak = {"player": row["name"], "streak": row["streak"]}
        except:
            pass

        # 9. LONGEST LOSING STREAK
        try:
            cur.execute(
                """
                WITH player_games AS (
                    SELECT 
                        p.id,
                        p.name,
                        r.match_date,
                        CASE WHEN r.points = 0 THEN 1 ELSE 0 END as is_loss,
                        ROW_NUMBER() OVER (PARTITION BY p.id ORDER BY r.match_date) as rn
                    FROM players p
                    JOIN results r ON p.id = r.player_id
                    WHERE p.game_id = %s AND r.points IS NOT NULL
                ),
                streaks AS (
                    SELECT 
                        name,
                        SUM(is_loss) as streak
                    FROM (
                        SELECT 
                            name,
                            is_loss,
                            SUM(CASE WHEN is_loss = 0 THEN 1 ELSE 0 END) 
                                OVER (PARTITION BY id ORDER BY rn) as grp
                        FROM player_games
                    ) x
                    WHERE is_loss = 1
                    GROUP BY name, grp
                )
                SELECT name, MAX(streak) as streak
                FROM streaks
                GROUP BY name
                ORDER BY streak DESC
                LIMIT 1
            """,
                (game_id,),
            )
            row = cur.fetchone()
            if row and row["streak"]:
                longest_losing_streak = {"player": row["name"], "streak": row["streak"]}
        except:
            pass

        # 10. BEST TEAM PAIRINGS (IMPLEMENTED)
        try:
            cur.execute(
                """
                WITH team_games AS (
                    SELECT 
                        lt.date,
                        lt.team_name,
                        p.id as player_id,
                        p.name as player_name
                    FROM locked_teams lt
                    JOIN players p ON lt.player_id = p.id
                    WHERE p.game_id = %s
                ),
                team_results AS (
                    SELECT 
                        tg.date,
                        tg.team_name,
                        SUM(r.points) as team_points
                    FROM team_games tg
                    JOIN results r ON r.player_id = tg.player_id AND r.match_date = tg.date
                    WHERE r.points IS NOT NULL
                    GROUP BY tg.date, tg.team_name
                ),
                winning_teams AS (
                    SELECT 
                        tw1.date,
                        tw1.team_name
                    FROM team_results tw1
                    LEFT JOIN team_results tw2 ON tw1.date = tw2.date AND tw1.team_name != tw2.team_name
                    WHERE tw1.team_points > COALESCE(tw2.team_points, 0)
                ),
                pairings AS (
                    SELECT 
                        LEAST(tg1.player_name, tg2.player_name) as player1,
                        GREATEST(tg1.player_name, tg2.player_name) as player2,
                        tg1.date,
                        CASE WHEN wt.date IS NOT NULL THEN 1 ELSE 0 END as won
                    FROM team_games tg1
                    JOIN team_games tg2 ON tg1.date = tg2.date 
                        AND tg1.team_name = tg2.team_name 
                        AND tg1.player_id < tg2.player_id
                    LEFT JOIN winning_teams wt ON tg1.date = wt.date AND tg1.team_name = wt.team_name
                )
                SELECT 
                    player1,
                    player2,
                    COUNT(*) as games_together,
                    SUM(won) as wins_together,
                    ROUND(100.0 * SUM(won) / COUNT(*), 1) as win_rate
                FROM pairings
                GROUP BY player1, player2
                HAVING COUNT(*) >= 3
                ORDER BY win_rate DESC, wins_together DESC
                LIMIT 10
            """,
                (game_id,),
            )
            best_pairings = cur.fetchall() or []
        except Exception as e:
            app.logger.error(f"Pairings query error: {e}")

        # 12. FUN FACTS (IMPROVED)
        if win_rates:
            fun_facts.append(
                {
                    "icon": "🔥",
                    "title": "Top Performer",
                    "description": f"{win_rates[0]['name']} leads with {win_rates[0]['win_rate']:.1f}% win rate!",
                }
            )

        # Handle ties in most active
        if most_active["games"] > 0:
            if len(most_active["tied_players"]) > 1:
                players_str = " & ".join(most_active["tied_players"])
                fun_facts.append(
                    {
                        "icon": "🛡️",
                        "title": "Iron Players",
                        "description": f"{players_str} have each played {most_active['games']} games.",
                    }
                )
            else:
                fun_facts.append(
                    {
                        "icon": "🛡️",
                        "title": "Iron Player",
                        "description": f"{most_active['name']} has played {most_active['games']} games.",
                    }
                )

        # ASSEMBLE STATS (REMOVED avg_points_per_game and team_stats)
        stats = {
            "total_matches": total_matches,
            "active_players": active_players,
            "most_active_player": most_active,
            "win_rates": win_rates,
            "recent_matches": recent_matches,
            "longest_game_streak": longest_game_streak,
            "longest_win_streak": longest_win_streak,
            "longest_losing_streak": longest_losing_streak,
            "best_pairings": best_pairings,
            "fun_facts": fun_facts if fun_facts else None,
        }

        return render_template(
            "statistics.html", stats=stats if total_matches > 0 else None
        )

    except Exception as e:
        app.logger.error(f"Statistics error: {e}")
        import traceback

        traceback.print_exc()
        return render_template("statistics.html", stats=None)

    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    # Get port from environment variable, default to 10000 for Render
    port = int(os.environ.get("PORT", 10000))
    # In production, debug should be False
    app.run(host="0.0.0.0", port=port, debug=False)
