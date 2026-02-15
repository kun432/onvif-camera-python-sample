# Repository Guidelines

## Project Structure & Module Organization
This repository is a small Python workspace for ONVIF camera control and capture scripts.

- `capture_pic.py`: interactive PTZ control plus still image capture.
- `capture_mov.py`: interactive PTZ control plus video recording.
- `deviceinfo.py`: quick device metadata check.
- `ptz.py`: Pan/Tilt interactive control script for movement checks and calibration (zoom not supported yet).
- `captures/`: output directory for generated images and videos.
- `pyproject.toml` and `uv.lock`: dependency and environment lock files.

Keep new runtime code at the repository root unless a clear module split is introduced. If scripts grow, move shared logic into a package directory such as `onvif_camera/`.

## Build, Test, and Development Commands
Use `uv` for environment and command execution.

Recommended execution order for first-time setup and validation:

1. `uv sync`: install dependencies from `pyproject.toml` and `uv.lock`.
2. `uv run deviceinfo.py`: confirm ONVIF connectivity, credentials, and camera metadata.
3. `uv run ptz.py`: validate Pan/Tilt movement before testing capture flows (zoom not supported yet).
4. `uv run capture_pic.py`: validate still capture.
5. `uv run capture_mov.py`: validate RTSP recording flow.

`ffmpeg` is required by capture fallbacks and recording paths. Install it on your OS before running capture scripts.

## Coding Style & Naming Conventions
Target Python 3.12+ and follow PEP 8.

- Use 4-space indentation and `snake_case` for functions/variables.
- Keep async flows explicit: prefer small `async def` helpers over long monolithic blocks.
- Keep environment variable names uppercase (for example `ONVIF_HOST`, `ONVIF_USER`, `CAPTURE_DIR`).
- Add brief comments only where camera-specific behavior is non-obvious.

## Testing Guidelines
There is no committed automated test suite yet.

- Add new tests under `tests/` using `pytest` with names like `test_capture_pic.py`.
- Focus first on pure helpers (parsing, range handling, file naming) before hardware-dependent paths.
- Run tests with `uv run pytest` after adding `pytest` to project dependencies.

## Commit & Pull Request Guidelines
This repository currently has no commit history, so follow a simple, consistent format.

- Use conventional-style subjects, for example: `feat: add snapshot URI timeout handling`.
- Keep commits focused and runnable.
- In pull requests, include: purpose, changed files, how to test, and any required `.env` keys.
- For behavior changes in capture flow, include sample logs or terminal output.

## Security & Configuration Tips
Store camera credentials only in `.env` (never in source files). Do not commit generated media from `captures/` unless explicitly needed for debugging.
