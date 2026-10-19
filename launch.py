"""Prepare project-local settings and TMP dependencies, then launch TypeRoom."""
from pathlib import Path
import subprocess
import sys

def prepare_settings(root: Path) -> Path:
    """Create first-run settings without overwriting an existing local directory."""
    destination = root / "settings.json"
    if destination.exists():
        return destination
    template = root / "settings.example.json"
    source = template.read_text(encoding="utf-8")
    (root / "fonts").mkdir(exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as output:
            output.write(source)
    except FileExistsError:
        pass
    return destination


def main():
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / ".deps"))
    prepare_settings(root)
    try:
        import UnityPy
        import PIL
    except ImportError:
        print("正在准备 TMP 预览依赖（仅首次需要联网，安装在本项目 .deps 文件夹）…", flush=True)
        result = subprocess.run([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade",
            "--target", str(root / ".deps"), "-r", str(root / "requirements.txt"),
        ])
        if result.returncode:
            print("依赖安装失败。可联网后运行 setup.cmd，再重新启动。")
            return 1
    from server import main as run_server
    return run_server()


if __name__ == "__main__":
    raise SystemExit(main())
