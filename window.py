#!/usr/bin/env python3
"""
Окно Claude RC Hub: список проектов, включение Remote Control, чаты, настройки.

Работает рядом с меню ✳︎ (claude_rc_hub.py): оба управляют одними и теми же
tmux-сессиями и одним config.json, поэтому изменения видны и там, и там.
"""
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import webview

from rc_core import (
    APP_DIR,
    CLAUDE,
    CONFIG_PATH,
    LOG_PATH,
    SOCKET,
    TMUX,
    activity_time,
    autostart_names,
    list_projects,
    load_config,
    log,
    open_in_terminal,
    save_config,
    session_info,
    session_name,
    session_states,
    start_session,
    stop_session,
)

LABEL = "com.user.claude-rc-hub"
APP_BUNDLE = Path("/Applications/Claude RC Hub.app")
UI_PATH = Path(__file__).with_name("ui.html")
SETTINGS = {"keep_awake", "auto_restart", "autostart_count", "projects_dir"}


def hub_running():
    r = subprocess.run(
        ["pgrep", "-f", str(APP_DIR / "claude_rc_hub.py")], capture_output=True
    )
    return r.returncode == 0


class Api:
    def __init__(self):
        self.window = None
        self.busy = set()  # проекты, которые сейчас запускаются / останавливаются
        self.lock = threading.Lock()

    def _project(self, name):
        cfg = load_config()
        for p in list_projects(cfg):
            if p.name == name:
                return p
        raise ValueError(f"нет проекта {name}")

    def _in_background(self, name, fn):
        with self.lock:
            if name in self.busy:
                return
            self.busy.add(name)

        def run():
            try:
                fn()
            finally:
                with self.lock:
                    self.busy.discard(name)

        threading.Thread(target=run, daemon=True).start()

    # ---------- чтение

    def get_state(self):
        cfg = load_config()
        projects = list_projects(cfg)
        states = session_states()
        auto = autostart_names(cfg, projects)
        pinned = set(cfg.get("autostart_pinned", []))
        items = []
        for p in projects:
            sess = session_name(p.name)
            st = states.get(sess, "off")
            info = session_info(sess) if st != "off" else {}
            items.append({
                "name": p.name,
                "status": st,
                "busy": p.name in self.busy,
                "auto": p.name in auto,
                "pinned": p.name in pinned,
                "activity": activity_time(p),
                "chats": info.get("chats", []),
                "chats_count": info.get("chats_count", 0),
                "url": info.get("url"),
                "untrusted": info.get("untrusted", False),
            })
        return {
            "projects": items,
            "settings": {k: cfg.get(k) for k in SETTINGS},
            "any_pinned": bool(pinned & {p.name for p in projects}),
            "hub_running": hub_running(),
            "ready": bool(TMUX and CLAUDE),
            "now": time.time(),
        }

    # ---------- проекты

    def set_enabled(self, name, on):
        p = self._project(name)
        self._in_background(name, (lambda: start_session(p)) if on else (lambda: stop_session(p)))

    def toggle_pin(self, name):
        cfg = load_config()
        pinned = list(cfg.get("autostart_pinned", []))
        if name in pinned:
            pinned.remove(name)
        else:
            pinned.append(name)
        cfg["autostart_pinned"] = pinned
        save_config(cfg)

    def open_terminal(self, name):
        sess = session_name(name)
        open_in_terminal(f"{shlex.quote(TMUX)} -L {SOCKET} attach -t {shlex.quote(sess)}")

    def open_finder(self, name):
        subprocess.run(["open", str(self._project(name))])

    def open_trust(self, name):
        open_in_terminal(f"cd {shlex.quote(str(self._project(name)))} && {shlex.quote(CLAUDE)}")

    def open_url(self, url):
        if url and url.startswith("https://claude.ai/"):
            subprocess.run(["open", url])

    def start_autostart(self):
        cfg = load_config()
        projects = list_projects(cfg)
        names = autostart_names(cfg, projects)
        states = session_states()
        for p in projects:
            if p.name in names and states.get(session_name(p.name)) not in ("running", "pending"):
                self._in_background(p.name, lambda p=p: start_session(p))

    def stop_all(self):
        states = session_states()
        cfg = load_config()
        for p in list_projects(cfg):
            if session_name(p.name) in states:
                self._in_background(p.name, lambda p=p: stop_session(p))
        log("stop all (window)")

    # ---------- настройки и хаб

    def set_setting(self, key, value):
        if key not in SETTINGS:
            raise ValueError(key)
        if key == "autostart_count":
            value = max(0, min(50, int(value)))
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)

    def choose_projects_dir(self):
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if res:
            self.set_setting("projects_dir", res[0])
            return res[0]
        return None

    def open_config(self):
        subprocess.run(["open", "-e", str(CONFIG_PATH)])

    def open_log(self):
        if LOG_PATH.exists():
            subprocess.run(["open", "-e", str(LOG_PATH)])

    def start_hub(self):
        domain = f"gui/{os.getuid()}"
        plist = Path.home() / f"Library/LaunchAgents/{LABEL}.plist"
        if subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True).returncode != 0:
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)])
        subprocess.run(["launchctl", "kickstart", f"{domain}/{LABEL}"])


def brand_app():
    """Имя и иконка в Dock: иначе macOS покажет «Python» с ракетой."""
    try:
        from AppKit import NSApplication, NSBundle, NSImage

        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "Claude RC Hub"
        icon = APP_BUNDLE / "Contents/Resources/AppIcon.icns"
        if icon.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(
                NSImage.alloc().initWithContentsOfFile_(str(icon))
            )
    except Exception as e:
        log(f"window brand error: {e}")


def main():
    brand_app()
    api = Api()
    api.window = webview.create_window(
        "Claude RC Hub",
        UI_PATH.as_uri(),
        js_api=api,
        width=760,
        height=720,
        min_size=(520, 420),
    )
    webview.start()


if __name__ == "__main__":
    main()
