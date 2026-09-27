"""
Общая логика Claude RC Hub: проекты, конфиг, сессии `claude remote-control`
в отдельном tmux-сервере. Используется меню ✳︎ (claude_rc_hub.py) и окном (window.py).
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path.home()
APP_DIR = HOME / ".claude-rc-hub"
CONFIG_PATH = APP_DIR / "config.json"
TMUX_CONF = APP_DIR / "tmux.conf"
LOG_PATH = APP_DIR / "hub.log"
SOCKET = "claude-rc"  # отдельный tmux-сервер, не трогает твой обычный tmux
PREFIX = "rc-"

EXTRA_PATHS = [
    str(HOME / ".local/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
]
os.environ["PATH"] = ":".join(EXTRA_PATHS + [os.environ.get("PATH", "")])

DEFAULT_CONFIG = {
    "projects_dir": str(HOME / "PycharmProjects"),
    "autostart_count": 5,
    "autostart_pinned": [],
    "keep_awake": True,
    "autostart_delay_sec": 20,
    "auto_restart": True,
}

# паузы перед повторными попытками перезапуска упавшей сессии, сек
RESTART_BACKOFF = [10, 30, 60, 120, 300]
# сколько сессия должна проработать подключённой, чтобы счётчик попыток сбросился
RESTART_RESET_AFTER = 120

TMUX = shutil.which("tmux")
CLAUDE = shutil.which("claude")


# ---------------------------------------------------------------- утилиты

def log(msg):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            log(f"config read error: {e}")
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def tmux(*args):
    cmd = [TMUX, "-L", SOCKET, "-f", str(TMUX_CONF), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def session_name(project_name):
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", project_name)[:30]
    digest = hashlib.md5(project_name.encode("utf-8")).hexdigest()[:6]
    return f"{PREFIX}{slug}-{digest}"


def activity_time(p: Path):
    """Время последнего изменения проекта (сама папка + файлы верхнего уровня)."""
    try:
        latest = p.stat().st_mtime
    except OSError:
        return 0
    try:
        for child in p.iterdir():
            if child.name == ".DS_Store":
                continue
            try:
                latest = max(latest, child.stat().st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return latest


def list_projects(cfg):
    root = Path(cfg["projects_dir"]).expanduser()
    if not root.is_dir():
        return []
    projects = [
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    ]
    projects.sort(key=activity_time, reverse=True)
    return projects


def session_states():
    """{имя_сессии: 'running' | 'pending' | 'dead'}"""
    if not TMUX:
        return {}
    r = tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}")
    if r.returncode != 0:
        return {}
    states = {}
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        name, dead = line.split("\t", 1)
        states[name] = "dead" if dead.strip() == "1" else "running"
    # живой процесс ещё не значит, что Remote Control подключился
    for name, st in states.items():
        # «Ready» — подключён без сессий, «Connected» — с сессиями
        if st == "running" and not re.search(r"\b(Ready|Connected)\b", pane_text(name)):
            states[name] = "pending"
    return states


def pane_text(sess):
    # -J склеивает строки, перенесённые по ширине окна
    r = tmux("capture-pane", "-p", "-J", "-t", sess, "-S", "-300")
    return r.stdout if r.returncode == 0 else ""


def is_untrusted(sess):
    return "not trusted" in pane_text(sess).lower()


def kill_sessions(sessions, timeout=5):
    """Останавливает сессии мягко: Ctrl-C, чтобы claude успел отписаться от
    сервера. Если убить сразу, папка ещё несколько минут считается занятой
    («This folder is already served…») и новый запуск в ней падает."""
    states = session_states()
    alive = [s for s in sessions if states.get(s) in ("running", "pending")]
    for s in alive:
        tmux("send-keys", "-t", s, "C-c")
    deadline = time.time() + timeout
    while alive and time.time() < deadline:
        time.sleep(0.2)
        states = session_states()
        alive = [s for s in alive if states.get(s) in ("running", "pending")]
    for s in sessions:
        tmux("kill-session", "-t", s)


def start_session(project: Path):
    sess = session_name(project.name)
    kill_sessions([sess])
    # --spawn явно: иначе в новом проекте claude спрашивает режим и висит;
    # --no-create-session-in-dir: только соединение, сессии создаются с телефона
    cmd = f"{shlex.quote(CLAUDE)} remote-control --spawn same-dir --no-create-session-in-dir"
    r = tmux(
        "new-session", "-d",
        "-s", sess,
        "-c", str(project),
        "-x", "200", "-y", "50",
        "-e", f"PATH={os.environ['PATH']}",
        cmd,
    )
    log(f"start {project.name} -> {sess}: rc={r.returncode} {r.stderr.strip()}")


def stop_session(project: Path):
    kill_sessions([session_name(project.name)])
    log(f"stop {project.name}")


def open_in_terminal(shell_cmd):
    escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Terminal"\n'
        "activate\n"
        f'do script "{escaped}"\n'
        "end tell"
    )
    subprocess.run(["osascript", "-e", script])


def autostart_names(cfg, projects):
    """Закреплённые проекты, а если их нет — N последних по активности."""
    existing = {p.name for p in projects}
    pinned = [n for n in cfg.get("autostart_pinned", []) if n in existing]
    if pinned:
        return set(pinned)
    count = int(cfg.get("autostart_count", 5))
    return {p.name for p in projects[:count]}


def session_info(sess):
    """Что видно в терминале сессии: чаты, ссылка на claude.ai, недоверенная папка."""
    text = pane_text(sess)
    lines = text.splitlines()
    info = {"chats": [], "chats_count": 0, "url": None, "untrusted": "not trusted" in text.lower()}
    cap_idx = None
    for i, line in enumerate(lines):
        m = re.search(r"Capacity:\s*(\d+)/\d+", line)
        if m:
            cap_idx, info["chats_count"] = i, int(m.group(1))
        u = re.search(r"https://claude\.ai/code\S*", line)
        if u:
            info["url"] = u.group(0)
    # имена чатов идут строками с отступом сразу под «Capacity: …»
    if cap_idx is not None:
        for line in lines[cap_idx + 1:]:
            if not line.startswith(" ") or not line.strip():
                break
            info["chats"].append(line.strip())
    return info
