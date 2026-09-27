#!/bin/bash
# Удаление Claude RC Hub: останавливает все сессии и убирает автозапуск.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
LABEL="com.user.claude-rc-hub"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
# мягко (Ctrl-C), чтобы claude отписался от сервера, потом добиваем
for s in $(tmux -L claude-rc ls -F "#{session_name}" 2>/dev/null); do tmux -L claude-rc send-keys -t "$s" C-c; done
sleep 3
tmux -L claude-rc kill-server 2>/dev/null || true
rm -rf "$HOME/.claude-rc-hub"
rm -rf "/Applications/Claude RC Hub.app"
echo "✓ Claude RC Hub удалён, все его сессии остановлены."
