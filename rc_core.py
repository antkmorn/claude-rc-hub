"""
Общая логика Klod remoteHub: проекты, конфиг, сессии `claude remote-control`
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
# «This folder is already served…»: сервер ещё помнит прошлый процесс (обычно
# после грубого выключения Мака) и отпускает папку минуты через две
ALREADY_SERVED_DELAY = 120
# подключённые сессии перечитываются не чаще, чем раз в столько секунд
CONNECTED_RECHECK = 60
# список проектов перечитывается с диска не чаще, чем раз в столько секунд
PROJECTS_CACHE_TTL = 60
LOG_MAX_BYTES = 1_000_000

TMUX = shutil.which("tmux")
CLAUDE = shutil.which("claude")


# ---------------------------------------------------------------- утилиты

def log(msg):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        if LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            # оставляем свежую половину, обрезая по границе строки
            data = LOG_PATH.read_bytes()[-LOG_MAX_BYTES // 2:]
            LOG_PATH.write_bytes(data[data.find(b"\n") + 1:])
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


_projects_cache = {"key": None, "at": 0.0, "projects": [], "activity": {}}


def list_projects(cfg, fresh=False):
    """Проекты, свежие сверху. С диска читаются раз в PROJECTS_CACHE_TTL секунд."""
    root = Path(cfg["projects_dir"]).expanduser()
    c = _projects_cache
    if not fresh and c["key"] == str(root) and time.time() - c["at"] < PROJECTS_CACHE_TTL:
        return list(c["projects"])
    if not root.is_dir():
        projects, activity = [], {}
    else:
        projects = [
            p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
        ]
        activity = {p.name: activity_time(p) for p in projects}
        projects.sort(key=lambda p: activity[p.name], reverse=True)
    c.update(key=str(root), at=time.time(), projects=projects, activity=activity)
    return list(projects)


def project_activity(name):
    """Время последнего изменения проекта из кеша list_projects."""
    return _projects_cache["activity"].get(name, 0)


def panes():
    """{имя_сессии: (мёртв ли процесс, pid)} — один вызов tmux."""
    if not TMUX:
        return {}
    r = tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}\t#{pane_pid}")
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out[parts[0]] = (parts[1].strip() == "1", parts[2].strip())
    return out


def is_connected(text):
    # «Ready» — подключён без чатов, «Connected» — с чатами
    return bool(re.search(r"\b(Ready|Connected)\b", text))


# {сессия: (pid, когда последний раз видели «подключён»)}
_connected_cache = {}


def session_states(texts=None):
    """{имя_сессии: 'running' | 'pending' | 'dead'}

    Терминал читается только у живых сессий, которые ещё не подключились или
    не перепроверялись дольше CONNECTED_RECHECK. Если передан словарь texts,
    в него кладётся прочитанный текст терминала, и читаются все живые сессии.
    """
    now = time.time()
    states = {}
    current = panes()
    for name, (dead, pid) in current.items():
        if dead:
            states[name] = "dead"
            if texts is not None:
                texts[name] = pane_text(name)
            continue
        cached = _connected_cache.get(name)
        if texts is None and cached and cached[0] == pid and now - cached[1] < CONNECTED_RECHECK:
            states[name] = "running"
            continue
        text = pane_text(name)
        if texts is not None:
            texts[name] = text
        if is_connected(text):
            states[name] = "running"
            _connected_cache[name] = (pid, now)
        else:
            states[name] = "pending"
            _connected_cache.pop(name, None)
    for name in list(_connected_cache):
        if name not in current:
            del _connected_cache[name]
    return states


def pane_text(sess):
    # -J склеивает строки, перенесённые по ширине окна
    r = tmux("capture-pane", "-p", "-J", "-t", sess, "-S", "-300")
    return r.stdout if r.returncode == 0 else ""


def is_untrusted(sess, text=None):
    return "not trusted" in (pane_text(sess) if text is None else text).lower()


def death_reason(text):
    """Последняя осмысленная строка упавшей сессии — для лога."""
    lines = [
        l.strip() for l in text.splitlines()
        if l.strip() and not l.startswith("Pane is dead") and not l.startswith("[bridge]")
    ]
    return lines[-1][:300] if lines else "(терминал пуст)"


def is_already_served(text):
    return "already served" in text


def kill_sessions(sessions, timeout=5):
    """Останавливает сессии мягко: Ctrl-C, чтобы claude успел отписаться от
    сервера. Если убить сразу, папка ещё несколько минут считается занятой
    («This folder is already served…») и новый запуск в ней падает."""
    def alive_of(names):
        current = panes()
        return [s for s in names if s in current and not current[s][0]]

    alive = alive_of(sessions)
    for s in alive:
        tmux("send-keys", "-t", s, "C-c")
    deadline = time.time() + timeout
    while alive and time.time() < deadline:
        time.sleep(0.2)
        alive = alive_of(alive)
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


def session_info(sess, text=None):
    """Что видно в терминале сессии: чаты, ссылка на claude.ai, недоверенная папка."""
    if text is None:
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
