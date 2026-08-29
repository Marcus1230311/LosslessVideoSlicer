from pathlib import Path
import sys, os
from PySide6.QtWidgets import QApplication
from .ui import MainWindow

def run():
    base=Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    app=QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName('无损视频切片器')
    app.setOrganizationName('LosslessSlicer')
    w=MainWindow(base);w.show()
    return app.exec()

if __name__=='__main__':raise SystemExit(run())
