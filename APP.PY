from flask import Flask, request, jsonify
import sqlite3
import json
import os
import time

app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        from flask import Response
        r = Response()
        r.headers['Access-Control-Allow-Origin'] = '*'
        r.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return r, 204

DB_PATH = os.path.join(os.path.dirname(__file__), 'ens_graph.db')

# ─── DB ───────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            ens_name     TEXT PRIMARY KEY,
            avatar_url   TEXT,
            eth_address  TEXT,
            content_hash TEXT,
            fields_json  TEXT,
            cached_at    INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS edges (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name_a     TEXT NOT NULL,
            name_b     TEXT NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(name_a, name_b)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def index():
    with open(__file__, 'r') as f:
        contents = f.read()
    return f'<pre>{contents}</pre>'

@app.route('/db')
def view_db():
    conn = get_db()
    profiles = [dict(r) for r in conn.execute('SELECT ens_name, eth_address, cached_at FROM profiles').fetchall()]
    edges = [dict(r) for r in conn.execute('SELECT * FROM edges').fetchall()]
    conn.close()
    return jsonify({'profiles': profiles, 'edges': edges})

CACHE_TTL = 60 * 60 * 24  # 24 hours

def normalize_pair(a, b):
    return (min(a, b), max(a, b))

# ─── PROFILES ─────────────────────────────────────────────────────────────────

@app.route('/api/profile/<ens_name>', methods=['GET'])
def get_profile(ens_name):
    """Return cached profile if it exists and is fresh, else 404."""
    ens_name = ens_name.strip().lower()
    conn = get_db()
    row = conn.execute('SELECT * FROM profiles WHERE ens_name = ?', (ens_name,)).fetchone()
    conn.close()

    if not row:
        return jsonify({'cached': False}), 404

    age = time.time() - (row['cached_at'] or 0)
    if age > CACHE_TTL:
        return jsonify({'cached': False, 'stale': True}), 404

    return jsonify({
        'cached': True,
        'ens_name': row['ens_name'],
        'avatar_url': row['avatar_url'],
        'eth_address': row['eth_address'],
        'content_hash': row['content_hash'],
        'fields': json.loads(row['fields_json'] or '{}'),
        'cached_at': row['cached_at']
    })


@app.route('/api/profile', methods=['POST'])
def save_profile():
    """JS calls this after fetching from Ethereum to cache the profile."""
    data = request.get_json()
    if not data or not data.get('ens_name'):
        return jsonify({'error': 'Missing ens_name'}), 400

    ens_name = data['ens_name'].strip().lower()
    avatar_url   = data.get('avatar_url') or None
    eth_address  = data.get('eth_address') or None
    content_hash = data.get('content_hash') or None
    fields       = data.get('fields') or {}

    conn = get_db()
    conn.execute('''
        INSERT INTO profiles (ens_name, avatar_url, eth_address, content_hash, fields_json, cached_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ens_name) DO UPDATE SET
            avatar_url   = excluded.avatar_url,
            eth_address  = excluded.eth_address,
            content_hash = excluded.content_hash,
            fields_json  = excluded.fields_json,
            cached_at    = excluded.cached_at
    ''', (ens_name, avatar_url, eth_address, content_hash, json.dumps(fields), int(time.time())))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'ens_name': ens_name})


@app.route('/api/profiles', methods=['GET'])
def list_profiles():
    """List all cached profile names."""
    conn = get_db()
    rows = conn.execute('SELECT ens_name, avatar_url, cached_at FROM profiles ORDER BY ens_name').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/profile/<ens_name>', methods=['DELETE'])
def delete_profile(ens_name):
    """Bust the cache for a profile so it gets re-fetched."""
    ens_name = ens_name.strip().lower()
    conn = get_db()
    conn.execute('DELETE FROM profiles WHERE ens_name = ?', (ens_name,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ─── EDGES ────────────────────────────────────────────────────────────────────

@app.route('/api/edges', methods=['GET'])
def get_edges():
    """Return all edges, optionally filtered by ?name=vitalik.eth"""
    name = request.args.get('name', '').strip().lower() or None
    conn = get_db()
    if name:
        rows = conn.execute(
            'SELECT * FROM edges WHERE name_a = ? OR name_b = ? ORDER BY created_at DESC',
            (name, name)
        ).fetchall()
    else:
        rows = conn.execute('SELECT * FROM edges ORDER BY created_at DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/edges', methods=['POST'])
def add_edge():
    """Add a friendship edge between two ENS names."""
    data = request.get_json()
    a = (data.get('name_a') or '').strip().lower()
    b = (data.get('name_b') or '').strip().lower()

    if not a or not b:
        return jsonify({'error': 'Need name_a and name_b'}), 400
    if not a.endswith('.eth') or not b.endswith('.eth'):
        return jsonify({'error': 'Both names must end in .eth'}), 400
    if a == b:
        return jsonify({'error': 'Cannot connect a name to itself'}), 400

    a, b = normalize_pair(a, b)
    conn = get_db()
    try:
        conn.execute('INSERT INTO edges (name_a, name_b) VALUES (?, ?)', (a, b))
        conn.commit()
        row = conn.execute('SELECT * FROM edges WHERE name_a=? AND name_b=?', (a, b)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'edge': dict(row)}), 201
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Edge already exists'}), 409


@app.route('/api/edges/<int:edge_id>', methods=['DELETE'])
def delete_edge(edge_id):
    """Delete an edge by ID."""
    conn = get_db()
    result = conn.execute('DELETE FROM edges WHERE id = ?', (edge_id,))
    conn.commit()
    conn.close()
    if result.rowcount == 0:
        return jsonify({'error': 'Edge not found'}), 404
    return jsonify({'ok': True, 'deleted_id': edge_id})


# ─── HEALTH ───────────────────────────────────────────────────────────────────

@app.route('/api/health', methods=['GET'])
def health():
    conn = get_db()
    profile_count = conn.execute('SELECT COUNT(*) FROM profiles').fetchone()[0]
    edge_count    = conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'profiles': profile_count, 'edges': edge_count})



