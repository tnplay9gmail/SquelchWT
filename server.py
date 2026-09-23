"""SquelchWT Windows entry point."""
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QIcon
import engine
from ui.window import MainWindow


def main():
    engine.acquire_instance_lock()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName('SquelchWT')
    app.setWindowIcon(QIcon(str(engine.ASSET_DIR / 'radio.ico')))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
