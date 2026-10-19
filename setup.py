from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class CleanWebBuildPy(build_py):
    """Prevent hashed Web assets from leaking across incremental wheel builds."""

    def run(self) -> None:
        web_target = Path(self.build_lib) / "voice_input" / "web_dist"
        if web_target.exists():
            shutil.rmtree(web_target)
        super().run()
        # A wheel has no repository-level assets directory. Keep the small,
        # runtime resources next to the installed Python package.
        source = Path(__file__).resolve().parent
        bundle = Path(self.build_lib) / "voice_input" / "bundled"
        for relative in ("assets/icons", "assets/lexicons"):
            target = bundle / relative
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source / relative, target)


setup(cmdclass={"build_py": CleanWebBuildPy})
