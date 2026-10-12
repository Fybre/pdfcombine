# PyInstaller spec - builds a single-file .exe on Windows and a .app bundle on macOS.
import os
import sys

VERSION = os.environ.get("APP_VERSION", "1.0.0")

# Qt modules the app never uses; leaving them out keeps the bundle small.
EXCLUDES = [
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDBus", "PySide6.QtPrintSupport", "PySide6.QtConcurrent",
    "PySide6.QtXml", "PySide6.QtHelp", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtPdf", "tkinter",
]

a = Analysis(["pdfcombine.py"], excludes=EXCLUDES, datas=[("assets/icon.png", "assets")])
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PDF Combine",
              console=False, upx=False, icon="assets/icon.icns")
    coll = COLLECT(exe, a.binaries, a.datas, name="PDF Combine", upx=False)
    app = BUNDLE(
        coll,
        name="PDF Combine.app",
        icon="assets/icon.icns",
        bundle_identifier="com.pdfcombine.app",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "PDF Document",
                "CFBundleTypeRole": "Viewer",
                "LSItemContentTypes": ["com.adobe.pdf"],
            }],
        },
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="PDFCombine",
              console=False, upx=False, icon="assets/icon.ico")
