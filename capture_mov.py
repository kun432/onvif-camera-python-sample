# main.py
import asyncio
import curses
import os
import shutil
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


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


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
    await try_dy(-probe)
    y_minus = await try_dy(-probe)
    await try_dy(+probe)

    if y_plus is None or y_minus is None:
        return +1
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
    return f"rtsp://{username}:{password}@{host}:554/stream1"


async def capture_via_rtsp_ffmpeg(rtsp_url: str, timeout_sec: float = 10.0) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport", "tcp",
            "-y",
            "-i", rtsp_url,
            "-frames:v", "1",
            "-q:v", "2",
            "-f", "image2",
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


def save_bytes(data: bytes, out_dir: Path, prefix: str, ext: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{prefix}_{now_ts()}.{ext}"
    path.write_bytes(data)
    return path


# -----------------------
# Video recording (RTSP + ffmpeg)
# -----------------------
def video_out_path(out_dir: Path, ext: str = "mkv") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"record_{now_ts()}.{ext}"


async def start_recording(rtsp_url: str, out_path: Path):
    """
    途中停止でも壊れにくいように mkv で -c copy（再エンコード無し）
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-map", "0",
        "-c", "copy",
        "-f", "matroska",
        "-y",
        str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    return proc


async def stop_recording(proc) -> str:
    """
    ffmpeg に 'q' を送って終了させる（これが一番壊れにくい）
    だめなら terminate -> kill
    """
    try:
        if proc.stdin:
            proc.stdin.write(b"q\n")
            await proc.stdin.drain()
    except Exception:
        pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            await proc.wait()

    err = ""
    try:
        if proc.stderr:
            err = (await proc.stderr.read()).decode("utf-8", errors="ignore").strip()
    except Exception:
        pass
    return err[:300]


async def start_live_preview(rtsp_url: str):
    if shutil.which("ffplay") is None:
        raise RuntimeError("ffplay not found")

    cmd = [
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-framedrop",
        "-window_title",
        "ONVIF Live Preview",
        rtsp_url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return proc


async def stop_live_preview(proc):
    try:
        proc.terminate()
    except Exception:
        pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=1.5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        await proc.wait()


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

    step = float(os.environ.get("PTZ_STEP", "0.10"))
    margin = float(os.environ.get("PTZ_MARGIN", "0.02"))
    settle = float(os.environ.get("PTZ_SETTLE_SEC", "0.12"))
    probe = float(os.environ.get("PTZ_PROBE", "0.12"))
    pan_sign_base = float(os.environ.get("PAN_SIGN", "-1.0"))

    capture_dir = Path(os.environ.get("CAPTURE_DIR", "./captures"))
    video_dir = Path(os.environ.get("VIDEO_DIR", "./captures"))
    fixed_sec = float(os.environ.get("VIDEO_SECONDS", "10"))

    curses.curs_set(0)
    # 固定秒録画の自動停止を回すため、短い周期でキー入力をポーリングする
    stdscr.nodelay(False)
    stdscr.timeout(100)
    stdscr.keypad(True)

    wsdl_dir = f"{os.path.dirname(onvif.__file__)}/wsdl/"
    cam = onvif.ONVIFCamera(host, port, user, password, wsdl_dir=wsdl_dir)

    video_proc = None
    video_path: Optional[Path] = None
    live_proc = None
    fixed_recording_deadline: Optional[float] = None

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

        pan_sign = pan_sign_base
        if mount_mode == "ceiling":
            pan_sign *= -1.0

        ui_line(stdscr, 0, "calibrating tilt ...")
        ui_line(stdscr, 2, f"range pan[{pan_min:.2f},{pan_max:.2f}] tilt[{tilt_min:.2f},{tilt_max:.2f}]")
        stdscr.refresh()

        await nudge_off_tilt_limit(ptz, token, tilt_min, tilt_max, settle=settle)
        tilt_up_sign = await decide_tilt_up_sign_by_limit(ptz, token, tilt_max, probe=probe, settle=max(0.20, settle))
        if mount_mode == "ceiling":
            tilt_up_sign *= -1

        pos = await get_pos(ptz, token)

        stdscr.clear()
        ui_line(stdscr, 0, "PTZ keyboard control (RelativeMove + photo + video)")
        ui_line(stdscr, 2, "Arrow / WASD : move")
        ui_line(stdscr, 4, "h            : go home (if supported)")
        ui_line(stdscr, 5, "i            : invert tilt (UP/DOWN swap)")
        ui_line(stdscr, 6, "p            : capture photo")
        ui_line(stdscr, 7, "v            : start/stop video recording")
        ui_line(stdscr, 8, f"V            : record {fixed_sec:.0f}s video")
        ui_line(stdscr, 9, "l            : start/stop live preview")
        ui_line(stdscr, 10, "q            : quit")
        ui_line(stdscr, 11, f"step={step} margin={margin} settle={settle} mount={mount_mode}")
        ui_line(stdscr, 12, f"tilt_up_sign={tilt_up_sign:+d}")
        stdscr.refresh()

        while True:
            loop_now = asyncio.get_running_loop().time()

            # Vで始めた固定秒録画の自動停止
            if (
                video_proc is not None
                and fixed_recording_deadline is not None
                and loop_now >= fixed_recording_deadline
            ):
                err = await stop_recording(video_proc)
                ui_line(stdscr, 17, f"video saved: {video_path}" if video_path else "video stopped")
                if err:
                    ui_line(stdscr, 18, f"ffmpeg: {err[:120]}")
                else:
                    ui_line(stdscr, 18, "")
                video_proc = None
                video_path = None
                fixed_recording_deadline = None

            if live_proc is not None and live_proc.returncode is not None:
                live_proc = None
                ui_line(stdscr, 17, "live preview stopped")

            if pos is not None:
                x, y = pos
                ui_line(stdscr, 14, f"pos pan={x:+.3f} tilt={y:+.3f}")
            else:
                ui_line(stdscr, 14, "pos (not available)")

            if video_proc is not None and video_path is not None:
                if fixed_recording_deadline is not None:
                    remain = max(0, int(fixed_recording_deadline - loop_now + 0.999))
                    ui_line(stdscr, 15, f"video: REC {remain}s -> {video_path}")
                else:
                    ui_line(stdscr, 15, f"video: REC -> {video_path}")
            else:
                ui_line(stdscr, 15, "video: idle")
            ui_line(stdscr, 16, "live: on" if live_proc is not None else "live: off")

            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                continue

            if key in (ord("q"), ord("Q")):
                break

            if key in (ord("h"), ord("H")):
                ok = await goto_home(ptz, token)
                ui_line(stdscr, 17, "home: ok" if ok else "home: not supported / failed")
                stdscr.refresh()
                await asyncio.sleep(0.4)
                pos = await get_pos(ptz, token)
                continue

            if key in (ord("i"), ord("I")):
                tilt_up_sign *= -1
                ui_line(stdscr, 12, f"tilt_up_sign={tilt_up_sign:+d}")
                ui_line(stdscr, 17, "tilt inverted")
                stdscr.refresh()
                continue

            if key in (ord("p"), ord("P")):
                ui_line(stdscr, 17, "capturing photo ...")
                stdscr.refresh()
                try:
                    img = await capture_photo(cam, media, token, user, password, host)
                    path = save_bytes(img, capture_dir, "capture", "jpg")
                    ui_line(stdscr, 17, f"saved: {path}")
                except Exception as e:
                    ui_line(stdscr, 17, f"photo failed: {type(e).__name__}: {e}")
                stdscr.refresh()
                continue

            # v: toggle recording
            if key == ord("v"):
                rtsp_url = os.environ.get("STREAM_URL") or get_rtsp_url(host, user, password)

                if video_proc is None:
                    video_path = video_out_path(video_dir, ext="mkv")
                    ui_line(stdscr, 17, f"video start ... {video_path}")
                    stdscr.refresh()
                    try:
                        video_proc = await start_recording(rtsp_url, video_path)
                        fixed_recording_deadline = None
                        ui_line(stdscr, 17, "video recording started")
                    except Exception as e:
                        video_proc = None
                        video_path = None
                        fixed_recording_deadline = None
                        ui_line(stdscr, 17, f"video start failed: {type(e).__name__}: {e}")
                    stdscr.refresh()
                else:
                    ui_line(stdscr, 17, "video stopping ...")
                    stdscr.refresh()
                    err = await stop_recording(video_proc)
                    ui_line(stdscr, 17, f"video saved: {video_path}" if video_path else "video stopped")
                    if err:
                        # うるさければ消してOK。問題切り分け用に一応残す。
                        ui_line(stdscr, 18, f"ffmpeg: {err[:120]}")
                    else:
                        ui_line(stdscr, 18, "")
                    video_proc = None
                    video_path = None
                    fixed_recording_deadline = None
                    stdscr.refresh()
                continue

            # V: fixed duration recording
            if key == ord("V"):
                rtsp_url = os.environ.get("STREAM_URL") or get_rtsp_url(host, user, password)
                path = video_out_path(video_dir, ext="mkv")
                if video_proc is not None:
                    ui_line(stdscr, 17, "video is already recording")
                    stdscr.refresh()
                    continue
                ui_line(stdscr, 17, f"recording {fixed_sec:.0f}s ... {path}")
                stdscr.refresh()
                try:
                    video_proc = await start_recording(rtsp_url, path)
                    video_path = path
                    fixed_recording_deadline = asyncio.get_running_loop().time() + max(0.0, fixed_sec)
                    ui_line(stdscr, 17, "video recording started")
                    ui_line(stdscr, 18, "")
                except Exception as e:
                    video_proc = None
                    video_path = None
                    fixed_recording_deadline = None
                    ui_line(stdscr, 17, f"video failed: {type(e).__name__}: {e}")
                stdscr.refresh()
                continue

            if key in (ord("l"), ord("L")):
                if live_proc is None:
                    rtsp_url = os.environ.get("STREAM_URL") or get_rtsp_url(host, user, password)
                    ui_line(stdscr, 17, "live preview starting ...")
                    stdscr.refresh()
                    try:
                        live_proc = await start_live_preview(rtsp_url)
                        ui_line(stdscr, 17, "live preview started")
                    except Exception as e:
                        live_proc = None
                        ui_line(stdscr, 17, f"live preview failed: {type(e).__name__}: {e}")
                else:
                    ui_line(stdscr, 17, "live preview stopping ...")
                    stdscr.refresh()
                    await stop_live_preview(live_proc)
                    live_proc = None
                    ui_line(stdscr, 17, "live preview stopped")
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
                except Exception as e1:
                    # 録画中は失敗しやすい個体があるため、移動量を半分にして1回だけ再試行する
                    try:
                        await relative_move(ptz, token, dx * 0.5, dy * 0.5)
                        await asyncio.sleep(settle)
                        msg = "移動: 半分のステップで再試行しました"
                    except Exception as e2:
                        detail = str(e2).strip().replace("\n", " ")
                        if not detail:
                            detail = str(e1).strip().replace("\n", " ")
                        msg = f"移動エラー: {detail[:120]}"

            ui_line(stdscr, 17, msg)
            stdscr.refresh()
            pos = await get_pos(ptz, token)

    finally:
        # 録画中なら止めてから終了
        if video_proc is not None:
            try:
                await stop_recording(video_proc)
            except Exception:
                pass
        if live_proc is not None:
            try:
                await stop_live_preview(live_proc)
            except Exception:
                pass
        try:
            await cam.close()
        except Exception:
            pass


def main():
    curses.wrapper(lambda stdscr: asyncio.run(async_main(stdscr)))


if __name__ == "__main__":
    main()
