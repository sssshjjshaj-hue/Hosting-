import os, json, uuid, zipfile, shutil, time, threading, subprocess, sys
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import psutil

app = Flask(__name__)
app.config['SECRET_KEY'] = 'nirob-secret-2025'
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_DIR = Path(__file__).parent
PROJECTS_DIR = BASE_DIR / 'projects'
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)

def load_json(path, default):
    if not path.exists():
        with open(path, 'w') as f: json.dump(default, f)
    with open(path) as f: return json.load(f)

def save_json(path, data):
    with open(path, 'w') as f: json.dump(data, f, indent=2)

users = load_json(DATA_DIR/'users.json', {"nirob": "hosting"})
projects = load_json(DATA_DIR/'projects.json', {})
active = {}

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = request.form['username']
        p = request.form['password']
        if u in users and users[u] == p:
            session['user'] = u
            return redirect('/')
        return render_template('login.html', error='Incorrect username or password')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/login')

@app.route('/')
def dashboard():
    if 'user' not in session: return redirect('/login')
    user = session['user']
    user_projects = {k:v for k,v in projects.items() if v['owner'] == user}
    running = sum(1 for p in user_projects.values() if p.get('status')=='running')
    stopped = len(user_projects) - running
    return render_template('dashboard.html', projects=user_projects,
                           total=len(user_projects), running=running, stopped=stopped)

@app.route('/create', methods=['POST'])
def create_project():
    if 'user' not in session: return redirect('/login')
    name = request.form.get('name','New Project').strip()
    pid = str(uuid.uuid4())[:8]
    (PROJECTS_DIR / pid).mkdir(exist_ok=True)
    projects[pid] = {
        'name': name, 'owner': session['user'],
        'created': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'status': 'stopped'
    }
    save_json(DATA_DIR/'projects.json', projects)
    return redirect(url_for('project', pid=pid))

@app.route('/delete/<pid>')
def delete_project(pid):
    if 'user' not in session: return redirect('/login')
    if pid not in projects or projects[pid]['owner'] != session['user']:
        return "Permission denied", 403
    if pid in active:
        stop_process(pid)
    shutil.rmtree(PROJECTS_DIR / pid, ignore_errors=True)
    del projects[pid]
    save_json(DATA_DIR/'projects.json', projects)
    return redirect('/')

@app.route('/project/<pid>')
def project(pid):
    if 'user' not in session: return redirect('/login')
    if pid not in projects or projects[pid]['owner'] != session['user']:
        return "Not Found", 404
    proj = projects[pid]
    files = os.listdir(PROJECTS_DIR / pid)
    files = sorted([f for f in files if not f.startswith('.')])
    return render_template('project.html', pid=pid, proj=proj, files=files)

@app.route('/upload/<pid>', methods=['POST'])
def upload(pid):
    if 'user' not in session or pid not in projects or projects[pid]['owner'] != session['user']:
        return "Unauthorized", 403
    file = request.files['file']
    if file:
        file.save(PROJECTS_DIR / pid / file.filename)
        if file.filename.endswith('.zip'):
            with zipfile.ZipFile(PROJECTS_DIR / pid / file.filename, 'r') as z:
                z.extractall(PROJECTS_DIR / pid)
    return redirect(url_for('project', pid=pid))

@app.route('/delete_file/<pid>/<filename>')
def delete_file(pid, filename):
    if 'user' not in session or pid not in projects or projects[pid]['owner'] != session['user']:
        return "Unauthorized", 403
    try: os.remove(PROJECTS_DIR / pid / filename)
    except: pass
    return redirect(url_for('project', pid=pid))

def stop_process(pid):
    if pid in active:
        try:
            active[pid].terminate()
            active[pid].wait(timeout=5)
        except:
            active[pid].kill()
        del active[pid]
    projects[pid]['status'] = 'stopped'
    save_json(DATA_DIR/'projects.json', projects)
    socketio.emit('process_stopped', {'pid': pid}, room=f'proj_{pid}')

@socketio.on('join')
def handle_join(data):
    pid = data['pid']
    if 'user' in session:
        join_room(f'proj_{pid}')
        if pid in projects:
            emit('status_update', {'status': projects[pid]['status']})
            emit('metrics', {'cpu': '0', 'ram': '0', 'uptime': '00:00:00'})

@socketio.on('run_file')
def handle_run_file(data):
    pid = data['pid']
    filename = data['filename']
    if 'user' not in session or pid not in projects or projects[pid]['owner'] != session['user']:
        emit('log', {'msg': 'Unauthorized\n'})
        return
    if pid in active:
        emit('log', {'msg': '❌ A process is already running!\n'})
        return
    filepath = PROJECTS_DIR / pid / filename
    if not filepath.exists():
        emit('log', {'msg': '❌ File not found\n'})
        return
    try:
        proc = subprocess.Popen(
            [sys.executable, str(filepath)],
            cwd=PROJECTS_DIR / pid,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True
        )
        active[pid] = proc
        projects[pid]['status'] = 'running'
        save_json(DATA_DIR/'projects.json', projects)
        emit('status_update', {'status': 'running'})
        emit('log', {'msg': f'▶ Running {filename}\n'})
        def reader():
            start = time.time()
            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    emit('log', {'msg': line})
                try:
                    p = psutil.Process(proc.pid)
                    cpu = p.cpu_percent(0.1) / psutil.cpu_count()
                    mem = p.memory_info().rss / 1024**2
                    uptime = time.strftime('%H:%M:%S', time.gmtime(time.time()-start))
                    emit('metrics', {'cpu': f'{cpu:.1f}', 'ram': f'{mem:.1f}', 'uptime': uptime})
                except: pass
            stop_process(pid)
        threading.Thread(target=reader, daemon=True).start()
    except Exception as e:
        emit('log', {'msg': f'❌ Error: {str(e)}\n'})

@socketio.on('stop_process')
def handle_stop(data):
    pid = data['pid']
    if 'user' not in session or pid not in projects or projects[pid]['owner'] != session['user']:
        return
    stop_process(pid)
    emit('log', {'msg': '🛑 Process stopped\n'})

@socketio.on('install_requirements')
def handle_install_req(data):
    pid = data['pid']
    if 'user' not in session or pid not in projects or projects[pid]['owner'] != session['user']:
        emit('log', {'msg': 'Unauthorized\n'})
        return
    req_file = PROJECTS_DIR / pid / 'requirements.txt'
    if not req_file.exists():
        emit('log', {'msg': '❌ requirements.txt not found\n'})
        return
    emit('log', {'msg': '📦 Installing dependencies...\n'})
    try:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'pip', 'install', '-r', str(req_file)],
            cwd=PROJECTS_DIR / pid,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True
        )
        def read_pip():
            for line in proc.stdout:
                emit('log', {'msg': line})
            proc.wait()
            if proc.returncode == 0:
                emit('log', {'msg': '✅ Installation successful\n'})
            else:
                emit('log', {'msg': '⚠️ Installation failed\n'})
        threading.Thread(target=read_pip, daemon=True).start()
    except Exception as e:
        emit('log', {'msg': f'❌ {str(e)}\n'})

@socketio.on('pip_install')
def handle_pip_install(data):
    pid = data['pid']
    package = data['package'].strip()
    if not package: return
    if 'user' not in session or pid not in projects or projects[pid]['owner'] != session['user']:
        emit('log', {'msg': 'Unauthorized\n'})
        return
    emit('log', {'msg': f'🔧 pip install {package} ...\n'})
    try:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'pip', 'install', package],
            cwd=PROJECTS_DIR / pid,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True
        )
        def read_pip():
            for line in proc.stdout:
                emit('log', {'msg': line})
            proc.wait()
            if proc.returncode == 0:
                emit('log', {'msg': f'✅ {package} installed\n'})
            else:
                emit('log', {'msg': '⚠️ Failed\n'})
        threading.Thread(target=read_pip, daemon=True).start()
    except Exception as e:
        emit('log', {'msg': str(e)})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)