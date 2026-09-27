#!/bin/bash
# Установка Claude RC Hub: зависимости, автозапуск при входе в систему.
set -e

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
APP_DIR="$HOME/.claude-rc-hub"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.user.claude-rc-hub"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_BUNDLE="/Applications/Claude RC Hub.app"

echo "→ Проверяю claude..."
if ! command -v claude >/dev/null 2>&1; then
  echo "✗ Команда claude не найдена. Сначала установи Claude Code."
  exit 1
fi

echo "→ Проверяю tmux..."
if ! command -v tmux >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "  tmux не найден, ставлю через Homebrew..."
    brew install tmux
  else
    echo "✗ Нет tmux и нет Homebrew. Поставь Homebrew (https://brew.sh), потом запусти install.sh снова."
    exit 1
  fi
fi

echo "→ Копирую файлы в $APP_DIR ..."
mkdir -p "$APP_DIR"
cp "$SRC_DIR/claude_rc_hub.py" "$SRC_DIR/rc_core.py" "$SRC_DIR/window.py" "$SRC_DIR/ui.html" "$APP_DIR/"

cat > "$APP_DIR/tmux.conf" <<'EOF'
set -g remain-on-exit on
set -g history-limit 10000
set -g mouse on
EOF

echo "→ Ставлю Python-зависимости (rumps, pywebview)..."
if [ ! -x "$APP_DIR/venv/bin/python3" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip rumps pywebview

echo "→ Настраиваю автозапуск..."
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/venv/bin/python3</string>
    <string>$APP_DIR/claude_rc_hub.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>StandardOutPath</key><string>$APP_DIR/stdout.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/stderr.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "→ Собираю $APP_BUNDLE ..."
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
cat > "$APP_BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Claude RC Hub</string>
  <key>CFBundleDisplayName</key><string>Claude RC Hub</string>
  <key>CFBundleIdentifier</key><string>$LABEL.launcher</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
</dict>
</plist>
EOF
# .app открывает окно; заодно будит фоновый хаб (меню ✳︎), если он не запущен
cat > "$APP_BUNDLE/Contents/MacOS/launcher" <<EOF
#!/bin/bash
DOMAIN="gui/\$(id -u)"
if ! launchctl print "\$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootstrap "\$DOMAIN" "$PLIST"
fi
if ! pgrep -f "$APP_DIR/claude_rc_hub.py" >/dev/null; then
  launchctl kickstart "\$DOMAIN/$LABEL"
fi
exec "$APP_DIR/venv/bin/python3" "$APP_DIR/window.py"
EOF
chmod +x "$APP_BUNDLE/Contents/MacOS/launcher"
"$APP_DIR/venv/bin/python3" "$SRC_DIR/make_icon.py" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
touch "$APP_BUNDLE"

echo ""
echo "✓ Готово. Иконка ✳︎ появится в строке меню вверху справа."
echo "  Через ~20 секунд стартуют 5 последних проектов."
echo "  Настройки: $APP_DIR/config.json"
