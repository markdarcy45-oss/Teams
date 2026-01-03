# =================================================================
# LIBRARIES & CONFIGURATION
# =================================================================
import os
import logging
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, current_user, logout_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

app.secret_key = "temporary-dev-key-123"
app.config.update(
    SESSION_COOKIE_NAME='teams_auth_v2',
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=1800 
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:MDlogin%238213@localhost:5432/Teams"
)

# ---------- Database Utility ----------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# =================================================================
# AUTHENTICATION MODELS & HELPERS
# =================================================================
class User(UserMixin):
    def __init__(self, user_id, username):
        self.id = str(user_id)
        self.username = username

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    if user_id is None: return None
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, username FROM users WHERE id = %s", (int(user_id),))
        row = cur.fetchone()
        return User(row['id'], row['username']) if row else None
    finally:
        conn.close()

@app.before_request
def handle_session_logic():
    if request.path.startswith('/static/') or request.endpoint == 'favicon': 
        return
    
    session.permanent = True
    AUTH_EXEMPT = {"login", "logout", "standings_leaderboard_page"}
    endpoint = (request.endpoint or "").split(".")[-1]
    
    if endpoint not in AUTH_EXEMPT and not current_user.is_authenticated:
        if request.is_json or request.path.startswith('/api/'):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("login"))

@app.route('/favicon.ico')
def favicon(): return '', 204

# =================================================================
# 1. AUTHENTICATION ROUTES
# =================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        pw_input = request.form.get('password', '').strip()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
        db_user = cur.fetchone()
        conn.close()
        if db_user and check_password_hash(db_user['password_hash'], pw_input):
            login_user(User(db_user['id'], db_user['username']))
            return redirect(url_for('teams_page'))
        return "Invalid username or password", 401
    return render_template('login.html')

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# =================================================================
# 2. PLAYER & GAME SETTINGS
# =================================================================

@app.route("/players", methods=["GET"])
@login_required
def players_page_render():
    return render_template("players.html")

@app.route("/api/games")
@login_required
def api_games():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM game_names WHERE owner_user_id=%s ORDER BY LOWER(name)", (current_user.id,))
        res = cur.fetchall()
        return jsonify(res)
    finally:
        conn.close()

@app.route("/api/players", methods=["GET"])
@login_required
def api_players():
    """Returns active players for a specific game."""
    game_id = request.args.get("game_id")
    if not game_id: return jsonify([])
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.name, COALESCE(rv.rank, 0) as rank
        FROM players p
        LEFT JOIN rankview rv ON LOWER(p.name) = LOWER(rv.player)
        WHERE p.game_id = %s AND p.active = 1
        ORDER BY rank ASC, LOWER(p.name)
    """, (game_id,))
    res = cur.fetchall()
    conn.close()
    return jsonify(res)

@app.route("/players", methods=["POST"])
@login_required
def api_players_upsert():
    """Syncs the player list: deactivates missing players, reactivates provided ones."""
    data = request.json or {}
    players, game_id, name = data.get("players", []), data.get("game_id"), data.get("name")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if not game_id:
            cur.execute("INSERT INTO game_names (name, owner_user_id) VALUES (%s,%s) RETURNING id", (name, current_user.id))
            game_id = cur.fetchone()["id"]
        cur.execute("UPDATE players SET active = 0 WHERE game_id = %s", (game_id,))
        for pname in players:
            pname_clean = pname.strip()
            if pname_clean:
                cur.execute("""
                    INSERT INTO players (name, game_id, active) VALUES (%s, %s, 1) 
                    ON CONFLICT (name, game_id) DO UPDATE SET active = 1
                """, (pname_clean, game_id))
        conn.commit()
        return jsonify({"id": game_id, "name": name}), 200
    finally:
        conn.close()

# =================================================================
# 3. TEAM GENERATION & MATCH MANAGEMENT
# =================================================================

@app.route("/")
@login_required
def teams_page():
    """Home page: Displays selection of active players for team generation."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.name as player, COALESCE(rv.rank, 0) as rank
        FROM players p
        LEFT JOIN rankview rv ON LOWER(p.name) = LOWER(rv.player)
        WHERE p.active = 1 ORDER BY LOWER(p.name)
    """)
    players = cur.fetchall()
    conn.close()
    return render_template("teams.html", players=players)

@app.route("/generate_teams", methods=["POST"])
@login_required
def generate_teams():
    """Balances selected players into two teams based on their rank."""
    data = request.get_json()
    selected_names = data.get("players", [])
    if not selected_names: return jsonify({"error": "No players selected"}), 400
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.name as player, COALESCE(rv.rank, 0) as rank
        FROM players p
        LEFT JOIN rankview rv ON LOWER(p.name) = LOWER(rv.player)
        WHERE p.name = ANY(%s) AND p.active = 1
    """, (selected_names,))
    pool = cur.fetchall()
    conn.close()
    sorted_pool = sorted(pool, key=lambda x: x['rank'], reverse=True)
    t1, t2 = [], []
    tot1, tot2 = 0, 0
    for p in sorted_pool:
        if tot1 <= tot2: t1.append(p); tot1 += p['rank']
        else: t2.append(p); tot2 += p['rank']
    return jsonify({"team1": t1, "team2": t2, "total1": tot1, "total2": tot2, "difference": abs(tot1-tot2)})

@app.route("/swap_locked_players", methods=["POST"])
@login_required
def swap_locked_players():
    """Manually swaps players between teams in the locked_teams table."""
    data = request.json
    date_str, p1, p2 = data.get("date"), data.get("p1"), data.get("p2")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE locked_teams SET team_name = %s WHERE date = %s AND player_id = %s", (p1['team'], date_str, p1['id']))
        cur.execute("UPDATE locked_teams SET team_name = %s WHERE date = %s AND player_id = %s", (p2['team'], date_str, p2['id']))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()

@app.route("/lock_teams", methods=["POST"])
@login_required
def lock_teams():
    """Saves the final team composition for a specific date."""
    data = request.get_json()
    game_date, team1, team2 = data.get("date"), data.get("team1", []), data.get("team2", [])
    if not game_date: return jsonify({"error": "Missing date"}), 400
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM locked_teams WHERE date = %s", (game_date,))
        def save(players, team):
            for i, p in enumerate(players):
                cur.execute("SELECT id, game_id FROM players WHERE name = %s", (p['player'],))
                res = cur.fetchone()
                if res:
                    cur.execute("""INSERT INTO locked_teams (date, game_id, player_id, team_name, slot, locked_by)
                                   VALUES (%s, %s, %s, %s, %s, %s)""", (game_date, res['game_id'], res['id'], team, i, current_user.id))
        save(team1, "Orange"); save(team2, "Yellow")
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.route("/unlock_teams", methods=["POST"])
@login_required
def unlock_teams():
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
@app.route("/api/get_locked_teams")
@login_required
def get_locked_teams():
    """Loads locked players to populate the Results entry form."""
    date_str = request.args.get("date") or request.args.get("Date")
    if not date_str: return jsonify({"error": "No date provided"}), 400
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""SELECT lt.team_name, p.id, p.id as player_id, p.name FROM locked_teams lt 
                   JOIN players p ON lt.player_id = p.id WHERE lt.date = %s
                   ORDER BY lt.team_name ASC, p.name ASC""", (date_str,))
    rows = cur.fetchall()
    conn.close()
    teams = {"Orange": [], "Yellow": []}
    for r in rows: teams[r['team_name']].append({"id": r['id'], "player_id": r['player_id'], "name": r['name']})
    return jsonify({"success": True, "teams": teams, "Date": date_str})

@app.route("/results", methods=["GET", "POST"])
@login_required
def results_entry_page():
    """Submits match results with point validation rules."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        date_str, player_results = data.get("Date") or data.get("date"), data.get("results", [])
        total_points = sum(int(r.get('points', 0)) for r in player_results)
        num_players = len(player_results)
        
        # Validation Logic
        if num_players == 10 and total_points not in [10, 15]:
            return jsonify({"error": f"10 players must have 10 or 15 total points (Current: {total_points})"}), 400
        if num_players == 12 and total_points not in [12, 18]:
            return jsonify({"error": f"12 players must have 12 or 18 total points (Current: {total_points})"}), 400

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            for res in player_results:
                cur.execute("SELECT game_id FROM players WHERE id = %s", (res['player_id'],))
                game_id = cur.fetchone()['game_id'] if cur.rowcount > 0 else 1
                cur.execute("""INSERT INTO results (match_date, game_id, player_id, points, submitted_by)
                               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (player_id, match_date) 
                               DO UPDATE SET points = EXCLUDED.points, submitted_by = EXCLUDED.submitted_by""",
                            (date_str, game_id, res['player_id'], res['points'], current_user.id))
            conn.commit()
            return jsonify({"ok": True})
        finally:
            conn.close()
    return render_template("submit_results.html")

@app.route("/standings")
@login_required
def standings_leaderboard_page():
    """Displays the current leaderboard."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rankview ORDER BY points DESC")
    rows = cur.fetchall()
    conn.close()
    return render_template("results_page.html", rankview=rows)

if __name__ == "__main__":
    app.run(debug=True)
