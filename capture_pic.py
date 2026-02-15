# main.py
import asyncio
import curses
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
import onvif
from dotenv import load_dotenv


# -----------------------
# helpers
# -----------------------
async def maybe_await(x):
    return await x if asyncio.iscoroutine(x) else x


def safe_get(obj, path: str, default=None):
    cur = obj
    for key in path.split("."):
        if cur is None:
            return default
        cur = getattr(cur, key, None)
    return default if cur is None else cur


def pick_ptz_profile(profiles):
    for p in profiles:
        if getattr(p, "PTZConfiguration", None):
            return p
    return profiles[0]


def ui_line(stdscr, row: int, text: str):
    h, w = stdscr.getmaxyx()
    stdscr.addstr(row, 0, " " * (w - 1))
    stdscr.addstr(row, 0, text[: w - 1])


# -----------------------
# ONVIF operations
# -----------------------
async def get_pos(ptz, token) -> Optional[Tuple[float, float]]:
    st = await ptz.GetStatus({"ProfileToken": token})
    x = safe_get(st, "Position.PanTilt.x", None)
    y = safe_get(st, "Position.PanTilt.y", None)
    if x is None or y is None:
        return None
    return float(x), float(y)


async def goto_home(ptz, token) -> bool:
    try:
        req = ptz.create_type("GotoHomePosition")
        req.ProfileToken = token
        await ptz.GotoHomePosition(req)
        return True
    except Exception:
        return False


async def relative_move(ptz, token, dx: float, dy: float):
    # dictで“全部埋めて”送る（Tapoで安定しやすい）
    await ptz.RelativeMove(
        {
            "ProfileToken": token,
            "Translation": {"PanTilt": {"x": dx, "y": dy}},
        }
    )


async def get_ranges(ptz, cfg_token) -> Tuple[float, float, float, float]:
    req = ptz.create_type("GetConfigurationOptions")
    req.ConfigurationToken = cfg_token
    opts = await ptz.GetConfigurationOptions(req)
    spaces = getattr(opts, "Spaces", None)

    abs_space = getattr(spaces, "AbsolutePanTiltPositionSpace", None) if spaces else None
    if abs_space:
        xr = abs_space[0].XRange
        yr = abs_space[0].YRange
        pan_min = float(getattr(xr, "Min", -1.0))
        pan_max = float(getattr(xr, "Max", 1.0))
        tilt_min = float(getattr(yr, "Min", -1.0))
        tilt_max = float(getattr(yr, "Max", 1.0))
        return pan_min, pan_max, tilt_min, tilt_max

    return -1.0, 1.0, -1.0, 1.0


# -----------------------
# tilt auto calibration
# -----------------------
async def nudge_off_tilt_limit(ptz, token, tilt_min, tilt_max, settle: float):
    p = await get_pos(ptz, token)
    if p is None:
        return
    _, y = p
    eps = 0.01

    # 端に張り付いてると判定が外れやすいので、軽く中へ寄せる
    try:
        if y >= tilt_max - eps:
            await relative_move(ptz, token, 0.0, -0.12)
            await asyncio.sleep(settle)
        elif y <= tilt_min + eps:
            await relative_move(ptz, token, 0.0, +0.12)
            await asyncio.sleep(settle)
    except Exception:
        pass


async def decide_tilt_up_sign_by_limit(
    ptz, token, tilt_max: float, probe: float = 0.12, settle: float = 0.25
) -> int:
    """
    「UPキー＝tilt を tilt_max 側へ近づける」と定義して、
    dy=+probe / dy=-probe のどっちが tilt_max に近づくかで決める。

    return:
      +1 -> UPキーで dy=+step
      -1 -> UPキーで dy=-step
    """
    p0 = await get_pos(ptz, token)
    if p0 is None:
        return +1
    _, y0 = p0

    async def try_dy(dy):
        try:
            await relative_move(ptz, token, 0.0, dy)
            await asyncio.sleep(settle)
            p = await get_pos(ptz, token)
            return None if p is None else float(p[1])
        except Exception:
            return None

    y_plus = await try_dy(+probe)
    await try_dy(-probe)  # だいたい戻す
    y_minus = await try_dy(-probe)
    await try_dy(+probe)  # だいたい戻す

    if y_plus is None or y_minus is None:
        return +1

    # 変化が全くないなら、どっちでも同じ → +1
    if abs(y_plus - y0) < 1e-3 and abs(y_minus - y0) < 1e-3:
        return +1

    dist_plus = abs(tilt_max - y_plus)
    dist_minus = abs(tilt_max - y_minus)
    return +1 if dist_plus <= dist_minus else -1


# -----------------------
# Photo capture (ONVIF -> RTSP fallback)
# -----------------------
async def fetch_bytes(url: str, username: str, password: str, timeout_sec: float = 6.0) -> bytes:
    auth = aiohttp.BasicAuth(username, password)
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


def get_rtsp_url(host: str, username: str, password: str) -> str:
    # だめなら .env の STREAM_URL で上書きできる
    return f"rtsp://{username}:{password}@{host}:554/stream1"


async def capture_via_rtsp_ffmpeg(rtsp_url: str, timeout_sec: float = 10.0) -> bytes:
    """
    ffmpegでRTSPから1フレームをJPEGで取り出してbytesで返す
    """
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = [
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-y",
            "-i",
            rtsp_url,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-f",
            "image2",
            tmp_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("ffmpeg timeout while capturing frame")

        if proc.returncode != 0:
            err = (await proc.stderr.read()).decode("utf-8", errors="ignore")
            err = err.strip().replace("\n", " ")
            raise RuntimeError(f"ffmpeg failed (code={proc.returncode}): {err[:400]}")

        return Path(tmp_path).read_bytes()

    finally:
        Path(tmp_path).unlink(missing_ok=True)


async def capture_photo(cam, media, profile_token: str, username: str, password: str, host: str) -> bytes:
    """
    1) cam.get_snapshot が使えれば試す
    2) GetSnapshotUri が取れればHTTPで取る（Faultなら無視）
    3) どっちもだめならRTSP(ffmpeg)で取る（最終手段）
    """
    # 1) helper
    try:
        get_snapshot = getattr(cam, "get_snapshot", None)
        if callable(get_snapshot):
            data = await get_snapshot(profile_token)
            if data:
                return data
    except Exception:
        pass

    # 2) SnapshotUri -> HTTP
    try:
        snap = await media.GetSnapshotUri({"ProfileToken": profile_token})
        uri = getattr(snap, "Uri", None) or getattr(snap, "URI", None)
        if uri:
            return await fetch_bytes(uri, username, password)
    except Exception:
        pass

    # 3) RTSP fallback
    rtsp_url = os.environ.get("STREAM_URL") or get_rtsp_url(host, username, password)
    return await capture_via_rtsp_ffmpeg(rtsp_url, timeout_sec=10.0)


def save_jpeg_bytes(image_bytes: bytes, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"capture_{ts}.jpg"
    path.write_bytes(image_bytes)
    return path


# -----------------------
# main
# -----------------------
async def async_main(stdscr):
    load_dotenv()

    host = os.environ["ONVIF_HOST"]
    port = int(os.environ.get("ONVIF_PORT", "80"))
    user = os.environ["ONVIF_USER"]
    password = os.environ["ONVIF_PASSWORD"]

    mount_mode = os.environ.get("MOUNT_MODE", "desk").strip().lower()

    # 体感調整（.envで上書き可）
    step = float(os.environ.get("PTZ_STEP", "0.10"))
    margin = float(os.environ.get("PTZ_MARGIN", "0.02"))
    settle = float(os.environ.get("PTZ_SETTLE_SEC", "0.12"))
    probe = float(os.environ.get("PTZ_PROBE", "0.12"))
    capture_dir = Path(os.environ.get("CAPTURE_DIR", "./captures"))

    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.keypad(True)

    wsdl_dir = f"{os.path.dirname(onvif.__file__)}/wsdl/"
    cam = onvif.ONVIFCamera(host, port, user, password, wsdl_dir=wsdl_dir)

    try:
        ui_line(stdscr, 0, "connecting ...")
        stdscr.refresh()

        await cam.update_xaddrs()

        media = await maybe_await(cam.create_media_service())
        profiles = await media.GetProfiles()
        profile = pick_ptz_profile(profiles)
        token = profile.token

        ptz = await maybe_await(cam.create_ptz_service())

        pan_min, pan_max, tilt_min, tilt_max = await get_ranges(ptz, profile.PTZConfiguration.token)

        # panは左右OK前提
        pan_sign = 1.0
        if mount_mode == "ceiling":
            pan_sign *= -1.0

        # tiltの符号を自動決定（UP＝tilt_max側へ）
        ui_line(stdscr, 0, "calibrating tilt ...")
        ui_line(stdscr, 2, f"range pan[{pan_min:.2f},{pan_max:.2f}] tilt[{tilt_min:.2f},{tilt_max:.2f}]")
        stdscr.refresh()

        await nudge_off_tilt_limit(ptz, token, tilt_min, tilt_max, settle=settle)
        tilt_up_sign = await decide_tilt_up_sign_by_limit(ptz, token, tilt_max, probe=probe, settle=max(0.20, settle))
        if mount_mode == "ceiling":
            tilt_up_sign *= -1

        pos = await get_pos(ptz, token)

        stdscr.clear()
        ui_line(stdscr, 0, "PTZ keyboard control (RelativeMove + photo)")
        ui_line(stdscr, 2, "Arrow / WASD : move")
        ui_line(stdscr, 4, "h            : go home (if supported)")
        ui_line(stdscr, 5, "i            : invert tilt (UP/DOWN swap)")
        ui_line(stdscr, 6, "p            : capture photo")
        ui_line(stdscr, 7, "q            : quit")
        ui_line(stdscr, 9, f"step={step} margin={margin} settle={settle} mount={mount_mode}")
        ui_line(stdscr, 10, f"range pan[{pan_min:.2f},{pan_max:.2f}] tilt[{tilt_min:.2f},{tilt_max:.2f}]")
        ui_line(stdscr, 11, f"tilt_up_sign={tilt_up_sign:+d}")
        stdscr.refresh()

        while True:
            if pos is not None:
                x, y = pos
                ui_line(stdscr, 13, f"pos pan={x:+.3f} tilt={y:+.3f}")
            else:
                ui_line(stdscr, 13, "pos (not available)")
            stdscr.refresh()

            key = stdscr.getch()

            if key in (ord("q"), ord("Q")):
                break

            if key in (ord("h"), ord("H")):
                ok = await goto_home(ptz, token)
                ui_line(stdscr, 15, "home: ok" if ok else "home: not supported / failed")
                stdscr.refresh()
                await asyncio.sleep(0.4)
                pos = await get_pos(ptz, token)
                continue

            if key in (ord("i"), ord("I")):
                tilt_up_sign *= -1
                ui_line(stdscr, 11, f"tilt_up_sign={tilt_up_sign:+d}")
                ui_line(stdscr, 15, "tilt inverted")
                stdscr.refresh()
                continue

            if key in (ord("p"), ord("P")):
                ui_line(stdscr, 15, "capturing ...")
                stdscr.refresh()
                try:
                    img = await capture_photo(cam, media, token, user, password, host)
                    path = save_jpeg_bytes(img, capture_dir)
                    ui_line(stdscr, 15, f"saved: {path}")
                except Exception as e:
                    ui_line(stdscr, 15, f"capture failed: {type(e).__name__}: {e}")
                stdscr.refresh()
                continue

            dx = 0.0
            dy = 0.0
            msg = ""

            if pos is not None:
                x, y = pos
                pan_lo = pan_min + margin
                pan_hi = pan_max - margin
                tilt_lo = tilt_min + margin
                tilt_hi = tilt_max - margin
            else:
                pan_lo, pan_hi, tilt_lo, tilt_hi = pan_min, pan_max, tilt_min, tilt_max

            if key in (curses.KEY_RIGHT, ord("d"), ord("D")):
                if pos is not None and x >= pan_hi:
                    msg = "blocked: right limit"
                else:
                    dx = +step * pan_sign

            elif key in (curses.KEY_LEFT, ord("a"), ord("A")):
                if pos is not None and x <= pan_lo:
                    msg = "blocked: left limit"
                else:
                    dx = -step * pan_sign

            elif key in (curses.KEY_UP, ord("w"), ord("W")):
                # UP = tilt_max 側へ
                if pos is not None:
                    if tilt_up_sign > 0 and y >= tilt_hi:
                        msg = "blocked: up limit"
                    elif tilt_up_sign < 0 and y <= tilt_lo:
                        msg = "blocked: up limit"
                    else:
                        dy = +step * tilt_up_sign
                else:
                    dy = +step * tilt_up_sign

            elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
                down_sign = -tilt_up_sign
                if pos is not None:
                    if down_sign > 0 and y >= tilt_hi:
                        msg = "blocked: down limit"
                    elif down_sign < 0 and y <= tilt_lo:
                        msg = "blocked: down limit"
                    else:
                        dy = +step * down_sign
                else:
                    dy = +step * down_sign

            if dx != 0.0 or dy != 0.0:
                try:
                    await relative_move(ptz, token, dx, dy)
                    await asyncio.sleep(settle)
                except Exception as e:
                    msg = f"error: {type(e).__name__}"

            ui_line(stdscr, 15, msg)
            stdscr.refresh()
            pos = await get_pos(ptz, token)

    except Exception as e:
        stdscr.clear()
        ui_line(stdscr, 0, "ERROR")
        ui_line(stdscr, 2, f"{type(e).__name__}: {e}")
        ui_line(stdscr, 4, "press any key to exit")
        stdscr.refresh()
        stdscr.getch()
        raise
    finally:
        try:
            await cam.close()
        except Exception:
            pass


def main():
    curses.wrapper(lambda stdscr: asyncio.run(async_main(stdscr)))


if __name__ == "__main__":
    main()
