# -*- mode: python ; coding: utf-8 -*-
#
# Chromium (~/Library/Caches/ms-playwright) is NOT bundled here as `datas` -
# PyInstaller's own binary analysis tries to re-sign the nested Chromium.app
# bundle inside it and fails outright (SystemError: Failed to process
# binary ... "Google Chrome for Testing"). Instead, after running
# `pyinstaller --noconfirm "Live Tools.spec"`, bundle it manually:
#
#   rm -rf "dist/Live Tools.app/Contents/Frameworks/ms-playwright/.links"  # if present from a previous run
#   cp -R ~/Library/Caches/ms-playwright "dist/Live Tools.app/Contents/Frameworks/ms-playwright"
#   rm -rf "dist/Live Tools.app/Contents/Frameworks/ms-playwright/.links"  # plain text, breaks --deep codesign
#   ln -s "../Frameworks/ms-playwright" "dist/Live Tools.app/Contents/Resources/ms-playwright"
#   codesign --force --deep -s - "dist/Live Tools.app"
#
# It MUST go in Contents/Frameworks, not Contents/Resources - that's the
# directory sys._MEIPASS actually resolves to at runtime (confirmed
# empirically: PyInstaller's own `binaries=` entries below land for real in
# Frameworks, with Resources holding only a symlink back to them). Putting it
# in Resources instead shipped silently broken in v1.1.0/v1.2.0: Playwright
# would fail with "Executable doesn't exist at .../Frameworks/ms-playwright/
# .../chrome-headless-shell" only once an actual live event was watched,
# since nothing else bundled this way is exercised by the other features.
#
# Zip/release asset naming: "Live.Tools.v<APP_VERSION>.MacOS.zip" (matches
# the Windows workflow's "Live.Tools.v<version>.Windows.zip"), e.g.:
#   cd dist && ditto -c -k --sequesterRsrc --keepParent \
#     "Live Tools.app" "Live.Tools.v1.3.0.MacOS.zip"
#
# GitHub release title: "Live Tools v<APP_VERSION>" (tag stays "v<APP_VERSION>"),
# e.g. tag "v1.3.0" with title "Live Tools v1.3.0".
#
# App name was renamed from "eBay Live Tools" to "Live Tools" starting with
# the first release built after this change - the Application Support and
# default Downloads folder names deliberately did NOT change (app_paths.py /
# app.py's _default_downloads_dir()), so upgrading users keep their existing
# settings/queue state and default downloads location.


a = Analysis(
    ['desktop_launcher.py'],
    pathex=[],
    binaries=[
        ('/tmp/ebay-tools-build/ffmpeg', '.'),
        ('/tmp/ebay-tools-build/yt-dlp', '.'),
        ('/tmp/ebay-tools-build/cloudflared', '.'),
    ],
    datas=[('static', 'static'), ('overlay/template.html', 'overlay')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Live Tools',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['/tmp/ebay-tools-icon/AppIcon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Live Tools',
)
app = BUNDLE(
    coll,
    name='Live Tools.app',
    icon='/tmp/ebay-tools-icon/AppIcon.icns',
    bundle_identifier=None,
)
