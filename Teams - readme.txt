================================================================================
                       TEAM & LEAGUE MANAGEMENT SYSTEM
================================================================================

DESCRIPTION
-----------
A comprehensive Flask-based web application designed to manage match days. 
The system handles player registration, automated team generation for 
10 or 12-player games, and maintains a real-time league leaderboard.

CORE WORKFLOW
-------------
1. PLAYERS   : Manage the master list of players.
2. TEAMS     : Select available players for a specific date, generate 
               balanced "Orange" and "Yellow" teams, and lock the rosters.
3. STANDINGS : The Unified Dashboard. Load the locked teams for the day, 
               input match points (0, 1, or 3), and view the instantly 
               updated league table and player rankings (MP, Pts, Rank).

TECHNICAL STACK
---------------
- Backend   : Python / Flask
- Database  : PostgreSQL (using psycopg2 and RealDictCursor)
- Frontend  : Jinja2 Templates, Vanilla JavaScript, CSS3 (CSS Variables)
- Auth      : Flask-Login with session persistence

INSTALLATION & SETUP
--------------------
1. INSTALL DEPENDENCIES:
   pip install flask psycopg2-binary flask-login werkzeug

2. DATABASE CONFIGURATION:
   Ensure DATABASE_URL is set in your environment variables:
   Example: postgresql://user:password@localhost:5432/Teams

3. RUN THE APPLICATION:
   python app.py
   Access via http://127.0.0.1:5000

USER INTERFACE FEATURES
-----------------------
- Sticky Navigation: Always accessible at the top of the page.
- Active Wayfinding: Current page is highlighted in the menu.
- Defensive Layout: Tables are optimized for readability across different 
  screen sizes with auto-refreshing data hooks.

DEVELOPMENT NOTES
-----------------
- The 'Results' submission logic is now integrated directly into the 
  Standings page for a streamlined "Match Day" experience.
- Validation: Points submission is restricted to valid totals (e.g., 
  12 or 18 points total for a 12-player game).

================================================================================m