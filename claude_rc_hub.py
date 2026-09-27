#!/usr/bin/env python3
"""
Claude RC Hub — приложение в строке меню macOS.

Показывает все проекты из папки проектов, запускает / останавливает
для каждого отдельную сессию `claude remote-control` (внутри tmux),
при старте автоматически поднимает 5 последних проектов (или закреплённые).
"""
import os
import shlex
import subprocess
import threading
import time

# Прячем иконку из Dock (приложение живёт только в строке меню)
try:
    from AppKit import NSBundle

    NSBundle.mainBundle().infoDictionary()["LSUIElement"] = "1"
except Exception:
    pass

import rumps

from rc_core import (
    CLAUDE,
    CONFIG_PATH,
    LOG_PATH,
    RESTART_BACKOFF,
    RESTART_RESET_AFTER,
    SOCKET,
    TMUX,
    autostart_names,
    is_untrusted,
    kill_sessions,
    list_projects,
    load_config,
    log,
    open_in_terminal,
    save_config,
    session_name,
    session_states,
    start_session,
    stop_session,
    tmux,
)

APP_BUNDLE = "/Applications/Claude RC Hub.app"


# ---------------------------------------------------------------- приложение

class Hub(rumps.App):
    def __init__(self):
        super().__init__("Claude RC", title="✳︎", quit_button=None)
        self.cfg = load_config()
        self.caffeinate = None
        self.signature = None
        self.dirty = True
        self.restarts = {}  # {сессия: {"attempts": n, "next": время следующей попытки}}
        self.connected_since = {}

        if not TMUX or not CLAUDE:
            missing = ", ".join(n for n, v in (("tmux", TMUX), ("claude", CLAUDE)) if not v)
            rumps.alert("Claude RC Hub", f"Не найдено: {missing}. Запусти install.sh ещё раз.")

        self.set_keep_awake(self.cfg.get("keep_awake", True))
        self.refresh(None)
        self.timer = rumps.Timer(self.refresh, 5)
        self.timer.start()

        delay = float(self.cfg.get("autostart_delay_sec", 20))
        threading.Timer(delay, self.autostart).start()

    # ---------- логика

    def autostart_names(self, projects):
        return autostart_names(self.cfg, projects)

    def autostart(self):
        if not TMUX or not CLAUDE:
            return
        cfg = load_config()
        self.cfg = cfg
        projects = list_projects(cfg)
        names = self.autostart_names(projects)
        states = session_states()
        for p in projects:
            if p.name in names and states.get(session_name(p.name)) not in ("running", "pending"):
                start_session(p)
                time.sleep(1)
        self.dirty = True

    def watchdog(self, projects, states):
        """Перезапускает упавшие сессии с нарастающей паузой."""
        now = time.time()
        by_sess = {session_name(p.name): p for p in projects}

        for sess, st in states.items():
            if st == "running":
                self.connected_since.setdefault(sess, now)
                if now - self.connected_since[sess] >= RESTART_RESET_AFTER:
                    self.restarts.pop(sess, None)
            else:
                self.connected_since.pop(sess, None)
        # остановленные вручную сессии исчезают из tmux — забываем о них
        for sess in list(self.restarts):
            if sess not in states:
                del self.restarts[sess]

        if not self.cfg.get("auto_restart", True) or not CLAUDE:
            return False
        started = False
        for sess, st in states.items():
            p = by_sess.get(sess)
            if st != "dead" or p is None or is_untrusted(sess):
                continue
            r = self.restarts.setdefault(sess, {"attempts": 0, "next": None})
            if r["next"] is None:
                delay = RESTART_BACKOFF[min(r["attempts"], len(RESTART_BACKOFF) - 1)]
                r["next"] = now + delay
                self.dirty = True
            elif now >= r["next"]:
                r["attempts"] += 1
                r["next"] = None
                log(f"auto-restart {p.name} (attempt {r['attempts']})")
                start_session(p)
                started = True
        return started

    def set_keep_awake(self, on):
        if on and self.caffeinate is None:
            # -s: не засыпать, пока Мак на зарядке (на батарее спит как обычно),
            # -w: сам завершится, когда закроется это приложение
            self.caffeinate = subprocess.Popen(
                ["caffeinate", "-s", "-w", str(os.getpid())]
            )
        elif not on and self.caffeinate is not None:
            self.caffeinate.terminate()
            self.caffeinate = None

    # ---------- меню

    def refresh(self, _):
        self.cfg = load_config()
        # настройку могли поменять в окне приложения
        if self.cfg.get("keep_awake", True) != (self.caffeinate is not None):
            self.set_keep_awake(self.cfg.get("keep_awake", True))
        projects = list_projects(self.cfg)
        states = session_states()
        if self.watchdog(projects, states):
            states = session_states()
        sig = (
            tuple(p.name for p in projects),
            tuple(sorted(states.items())),
            tuple(self.cfg.get("autostart_pinned", [])),
            self.cfg.get("autostart_count"),
            self.caffeinate is not None,
            self.cfg.get("auto_restart", True),
        )
        if sig != self.signature or self.dirty:
            self.signature = sig
            self.dirty = False
            self.rebuild(projects, states)

    def rebuild(self, projects, states):
        auto = self.autostart_names(projects)
        running = sum(
            1 for p in projects if states.get(session_name(p.name)) == "running"
        )
        self.title = f"✳︎ {running}" if running else "✳︎"

        items = [
            rumps.MenuItem("🪟 Открыть окно", callback=lambda _: subprocess.run(["open", APP_BUNDLE])),
            rumps.MenuItem(f"Запущено: {running} из {len(projects)}  ·  ⭐ = автостарт"),
            None,
        ]

        for p in projects:
            sess = session_name(p.name)
            st = states.get(sess)
            icon = {"running": "🟢", "pending": "🟡", "dead": "🔴"}.get(st, "⚪️")
            star = "  ⭐" if p.name in auto else ""
            item = rumps.MenuItem(f"{icon} {p.name}{star}")

            sub = []
            if st in ("running", "pending"):
                sub.append(rumps.MenuItem("■ Остановить", callback=lambda _, p=p: self.on_stop(p)))
            else:
                sub.append(rumps.MenuItem("▶ Запустить", callback=lambda _, p=p: self.on_start(p)))

            if st:
                sub.append(rumps.MenuItem(
                    "🖥 Показать терминал сессии (QR, ошибки)",
                    callback=lambda _, s=sess: self.on_attach(s),
                ))
            if st == "pending":
                sub.append(rumps.MenuItem("⏳ Ещё не подключился — смотри терминал"))
            if st == "dead":
                if is_untrusted(sess):
                    sub.append(rumps.MenuItem(
                        "⚠️ Папка не доверена — открыть claude и нажать Yes",
                        callback=lambda _, p=p: self.on_trust(p),
                    ))
                else:
                    sub.append(rumps.MenuItem("⚠️ Сессия завершилась — смотри терминал"))
                    r = self.restarts.get(sess)
                    if r and r["next"] and self.cfg.get("auto_restart", True):
                        when = time.strftime("%H:%M:%S", time.localtime(r["next"]))
                        sub.append(rumps.MenuItem(f"🔁 Авто-перезапуск в {when} (попыток было: {r['attempts']})"))

            sub.append(None)
            pin = rumps.MenuItem("Закрепить в автостарте", callback=lambda _, p=p: self.on_pin(p))
            pin.state = 1 if p.name in self.cfg.get("autostart_pinned", []) else 0
            sub.append(pin)
            sub.append(rumps.MenuItem("📂 Открыть в Finder", callback=lambda _, p=p: subprocess.run(["open", str(p)])))

            item.update(sub)
            items.append(item)

        if not projects:
            items.append(rumps.MenuItem(f"Нет проектов в {self.cfg['projects_dir']}"))

        awake = rumps.MenuItem("Не давать Маку засыпать", callback=self.on_toggle_awake)
        awake.state = 1 if self.caffeinate is not None else 0
        restart = rumps.MenuItem("Перезапускать упавшие сессии", callback=self.on_toggle_restart)
        restart.state = 1 if self.cfg.get("auto_restart", True) else 0

        items += [
            None,
            rumps.MenuItem("▶ Запустить все ⭐ (автостарт)", callback=lambda _: threading.Thread(target=self.autostart).start()),
            rumps.MenuItem("■ Остановить все", callback=self.on_stop_all),
            None,
            awake,
            restart,
            rumps.MenuItem("⚙️ Настройки (config.json)", callback=lambda _: subprocess.run(["open", "-e", str(CONFIG_PATH)])),
            rumps.MenuItem("📄 Лог", callback=lambda _: subprocess.run(["open", "-e", str(LOG_PATH)]) if LOG_PATH.exists() else None),
            rumps.MenuItem("Выйти из хаба (сессии продолжат работать)", callback=self.on_quit),
        ]

        self.menu.clear()
        self.menu.update(items)

    # ---------- обработчики

    def on_start(self, p):
        self.restarts.pop(session_name(p.name), None)
        start_session(p)
        self.dirty = True
        self.refresh(None)

    def on_stop(self, p):
        stop_session(p)
        self.dirty = True
        self.refresh(None)

    def on_stop_all(self, _):
        if TMUX:
            kill_sessions(list(session_states()))
            tmux("kill-server")
        log("stop all")
        self.dirty = True
        self.refresh(None)

    def on_attach(self, sess):
        open_in_terminal(f"{shlex.quote(TMUX)} -L {SOCKET} attach -t {shlex.quote(sess)}")

    def on_trust(self, p):
        open_in_terminal(f"cd {shlex.quote(str(p))} && {shlex.quote(CLAUDE)}")

    def on_pin(self, p):
        pinned = list(self.cfg.get("autostart_pinned", []))
        if p.name in pinned:
            pinned.remove(p.name)
        else:
            pinned.append(p.name)
        self.cfg["autostart_pinned"] = pinned
        save_config(self.cfg)
        self.dirty = True
        self.refresh(None)

    def on_toggle_awake(self, _):
        on = self.caffeinate is None
        self.set_keep_awake(on)
        self.cfg["keep_awake"] = on
        save_config(self.cfg)
        self.dirty = True
        self.refresh(None)

    def on_toggle_restart(self, _):
        self.cfg["auto_restart"] = not self.cfg.get("auto_restart", True)
        save_config(self.cfg)
        self.restarts.clear()
        self.dirty = True
        self.refresh(None)

    def on_quit(self, _):
        self.set_keep_awake(False)
        rumps.quit_application()


if __name__ == "__main__":
    log("hub started")
    Hub().run()
