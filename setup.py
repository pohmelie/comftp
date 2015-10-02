from cx_Freeze import setup, Executable


options = {
    "build_exe": {
        "includes": ["aioftp", "docopt", "serial"],
        "create_shared_zip": False,
        "packages": ["aioftp", "docopt", "serial"],
    }
}

executables = [
    Executable(
        script="comftp.py",
        # base="Win32GUI",
        targetName="comftp.exe",
        compress=True,
        copyDependentFiles=True,
        appendScriptToExe=True,
        appendScriptToLibrary=False,
        icon="comftp.ico"
    )
]

setup(
    name="comftp",
    version="0.0.1",
    description="",
    options=options,
    executables=executables,
)
