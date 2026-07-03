"""Streamlit dashboard for inspecting relay controller and IK messages.

Run while ``vr-teleop-relay`` is up:

    streamlit run src/vr_teleop_kit/tools/controller_inspector.py

or, after installing the ``inspector`` extra:

    vr-teleop-inspector
"""

from __future__ import annotations

import asyncio
import json
import math
import queue
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

try:
    import pandas as pd
    import streamlit as st
    import websockets
except ImportError as e:  # pragma: no cover - import guard for CLI users
    raise SystemExit(
        "The controller inspector needs the optional inspector dependencies. "
        'Install them with: pip install -e ".[inspector]"'
    ) from e


DEFAULT_URL = "ws://127.0.0.1:8443/ws"
MAX_DRAIN_PER_RERUN = 5000
DEFAULT_SAMPLE_HZ = 30
DEFAULT_MAX_POINTS = 500
STREAM_QUEUE_MAX = 2000
BUTTON_NAMES = {
    0: "trigger",
    1: "grip",
    3: "thumbstick",
    4: "precision",
    5: "handoff",
}


def _ssl_context_for(url: str) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@dataclass
class RelayStream:
    url: str
    out: "queue.Queue[dict[str, Any]]" = field(default_factory=lambda: queue.Queue(maxsize=STREAM_QUEUE_MAX))
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="relay-inspector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _put(self, item: dict[str, Any]) -> None:
        try:
            self.out.put_nowait(item)
        except queue.Full:
            try:
                self.out.get_nowait()
            except queue.Empty:
                pass
            try:
                self.out.put_nowait(item)
            except queue.Full:
                pass

    def _run(self) -> None:
        asyncio.run(self._runner())

    async def _runner(self) -> None:
        backoff_s = 0.5
        while not self._stop.is_set():
            try:
                self._put({"kind": "status", "status": "connecting", "url": self.url})
                async with websockets.connect(self.url, ssl=_ssl_context_for(self.url), ping_interval=10, ping_timeout=10) as ws:
                    self._put({"kind": "status", "status": "connected", "url": self.url})
                    backoff_s = 0.5
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                        except asyncio.TimeoutError:
                            continue
                        self._put({"kind": "message", "received_at": time.time(), "raw": raw})
            except Exception as e:
                self._put({"kind": "status", "status": "error", "error": str(e), "url": self.url})
                await asyncio.sleep(backoff_s)
                backoff_s = min(5.0, backoff_s * 1.5)
        self._put({"kind": "status", "status": "stopped", "url": self.url})




def _vec(values: Any, n: int, default: Any = None) -> list[Any]:
    if isinstance(values, (list, tuple)):
        out = list(values[:n])
    else:
        out = []
    if len(out) < n:
        out.extend([default] * (n - len(out)))
    return out

def _button_value(buttons: list[dict[str, Any]], index: int, key: str, default: float = 0.0) -> float:
    if len(buttons) <= index:
        return default
    value = buttons[index].get(key, default)
    return float(bool(value)) if key == "p" else float(value or 0.0)


def _flatten_xr_frame(msg: dict[str, Any], received_at: float) -> dict[str, Any]:
    row: dict[str, Any] = {
        "received_at": received_at,
        "type": "xr_frame",
        "client_time": msg.get("t_client"),
    }
    controllers = msg.get("controllers") or {}
    for hand in ("left", "right"):
        ctrl = controllers.get(hand) or {}
        pos = _vec(ctrl.get("position"), 3)
        quat = _vec(ctrl.get("orientation"), 4)
        diag = ctrl.get("diagnostics") or {}
        raw_pos = _vec(diag.get("raw_position") or pos, 3)
        corrected_pos = _vec(diag.get("corrected_position") or pos, 3)
        compensated_pos = _vec(diag.get("twist_artifact_compensated_position") or pos, 3)
        artifact_delta = _vec(diag.get("twist_artifact_delta"), 3)
        artifact_model = _vec(diag.get("twist_artifact_model_local"), 3)
        correction_delta = diag.get("correction_delta")
        if correction_delta is None and raw_pos and corrected_pos:
            correction_delta = [
                corrected_pos[i] - raw_pos[i]
                if corrected_pos[i] is not None and raw_pos[i] is not None else None
                for i in range(3)
            ]
        correction_delta = _vec(correction_delta, 3)
        offset_local = _vec(diag.get("correction_offset_local"), 3)
        buttons = ctrl.get("buttons") or []
        row.update({
            f"{hand}_x": pos[0],
            f"{hand}_y": pos[1],
            f"{hand}_z": pos[2],
            f"{hand}_raw_x": raw_pos[0],
            f"{hand}_raw_y": raw_pos[1],
            f"{hand}_raw_z": raw_pos[2],
            f"{hand}_corrected_x": corrected_pos[0],
            f"{hand}_corrected_y": corrected_pos[1],
            f"{hand}_corrected_z": corrected_pos[2],
            f"{hand}_compensated_x": compensated_pos[0],
            f"{hand}_compensated_y": compensated_pos[1],
            f"{hand}_compensated_z": compensated_pos[2],
            f"{hand}_artifact_x": artifact_delta[0],
            f"{hand}_artifact_y": artifact_delta[1],
            f"{hand}_artifact_z": artifact_delta[2],
            f"{hand}_artifact_model_local_x": artifact_model[0],
            f"{hand}_artifact_model_local_y": artifact_model[1],
            f"{hand}_artifact_model_local_z": artifact_model[2],
            f"{hand}_artifact_calibrated": float(bool(diag.get("twist_artifact_calibrated"))),
            f"{hand}_twist_delta_rad_client": diag.get("twist_delta_rad"),
            f"{hand}_correction_x": correction_delta[0],
            f"{hand}_correction_y": correction_delta[1],
            f"{hand}_correction_z": correction_delta[2],
            f"{hand}_offset_local_x": offset_local[0],
            f"{hand}_offset_local_y": offset_local[1],
            f"{hand}_offset_local_z": offset_local[2],
            f"{hand}_correction_mag": diag.get("correction_magnitude"),
            f"{hand}_wrist_correction_enabled": float(
                diag.get("wrist_calibration_correction_enabled", True) is not False
            ),
            f"{hand}_qx": quat[0],
            f"{hand}_qy": quat[1],
            f"{hand}_qz": quat[2],
            f"{hand}_qw": quat[3],
        })
        for idx, name in BUTTON_NAMES.items():
            row[f"{hand}_{name}_pressed"] = _button_value(buttons, idx, "p")
            row[f"{hand}_{name}_value"] = _button_value(buttons, idx, "v")
    viewer = msg.get("viewer") or {}
    vpos = _vec(viewer.get("position"), 3)
    vquat = _vec(viewer.get("orientation"), 4)
    row.update({
        "viewer_x": vpos[0],
        "viewer_y": vpos[1],
        "viewer_z": vpos[2],
        "viewer_qx": vquat[0],
        "viewer_qy": vquat[1],
        "viewer_qz": vquat[2],
        "viewer_qw": vquat[3],
    })
    return row


def _flatten_ik_state(msg: dict[str, Any], received_at: float) -> dict[str, Any]:
    row: dict[str, Any] = {
        "received_at": received_at,
        "type": "ik_state",
        "teleop_id": msg.get("teleop_id"),
        "loop_hz": msg.get("loop_hz"),
        "left_engaged": float(bool(msg.get("left_engaged"))),
        "right_engaged": float(bool(msg.get("right_engaged"))),
        "left_haptic": msg.get("left_haptic"),
        "right_haptic": msg.get("right_haptic"),
        "left_force_haptic": msg.get("left_force_haptic"),
        "right_force_haptic": msg.get("right_force_haptic"),
    }
    for hand in ("left", "right"):
        qpos = msg.get(f"{hand}_qpos") or []
        for i, value in enumerate(qpos[:8], start=1):
            row[f"{hand}_qpos_{i}"] = value
    return row


def _drain_stream(sample_hz: float, window_s: float) -> int:
    stream: RelayStream | None = st.session_state.get("stream")
    if stream is None:
        return 0
    min_dt = 1.0 / max(1.0, sample_hz)
    cutoff = time.time() - window_s
    drained = 0
    while drained < MAX_DRAIN_PER_RERUN:
        try:
            item = stream.out.get_nowait()
        except queue.Empty:
            break
        drained += 1
        if item["kind"] == "status":
            st.session_state.status = item
            continue
        try:
            msg = json.loads(item["raw"])
        except Exception:
            continue

        msg_type = msg.get("type")
        received_at = item["received_at"]
        st.session_state.last_message = msg
        try:
            if msg_type == "xr_frame":
                st.session_state.xr_seen.append(received_at)
                if received_at - st.session_state.last_xr_saved_at >= min_dt:
                    st.session_state.xr_rows.append(_flatten_xr_frame(msg, received_at))
                    st.session_state.last_xr_saved_at = received_at
            elif msg_type == "ik_state":
                st.session_state.ik_seen.append(received_at)
                if received_at - st.session_state.last_ik_saved_at >= min_dt:
                    st.session_state.ik_rows.append(_flatten_ik_state(msg, received_at))
                    st.session_state.last_ik_saved_at = received_at
        except Exception as e:
            st.session_state.last_parse_error = f"{msg_type}: {e}"
            continue

    for seen in (st.session_state.xr_seen, st.session_state.ik_seen):
        while seen and seen[0] < cutoff:
            seen.popleft()
    return drained


def _frame(rows: deque[dict[str, Any]], window_s: float, max_points: int) -> "pd.DataFrame":
    if not rows:
        return pd.DataFrame()
    cutoff = time.time() - window_s
    while rows and rows[0]["received_at"] < cutoff:
        rows.popleft()
    selected = list(rows)
    if max_points > 0 and len(selected) > max_points:
        step = max(1, len(selected) // max_points)
        selected = selected[::step][-max_points:]
    df = pd.DataFrame(selected)
    if df.empty:
        return df
    df["elapsed_s"] = df["received_at"] - df["received_at"].iloc[0]
    return df


def _norm3(df: "pd.DataFrame", columns: list[str]) -> "pd.Series":
    existing = [c for c in columns if c in df.columns]
    if len(existing) != 3:
        return pd.Series([math.nan] * len(df), index=df.index)
    values = df[existing].apply(pd.to_numeric, errors="coerce")
    return (values.pow(2).sum(axis=1)) ** 0.5


def _quat_mul(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _quat_conj(q: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x, y, z, w = q
    return (-x, -y, -z, w)


def _quat_norm(q: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
    n = math.sqrt(sum(v * v for v in q))
    if not math.isfinite(n) or n < 1e-9:
        return None
    return tuple(v / n for v in q)  # type: ignore[return-value]


def _rotate_vec_by_quat(v: tuple[float, float, float], q: tuple[float, float, float, float]) -> tuple[float, float, float]:
    vx, vy, vz = v
    qx, qy, qz, qw = q
    c1x = qy * vz - qz * vy
    c1y = qz * vx - qx * vz
    c1z = qx * vy - qy * vx
    c2x = qy * c1z - qz * c1y
    c2y = qz * c1x - qx * c1z
    c2z = qx * c1y - qy * c1x
    return (
        vx + 2 * qw * c1x + 2 * c2x,
        vy + 2 * qw * c1y + 2 * c2y,
        vz + 2 * qw * c1z + 2 * c2z,
    )


def _add_wrist_diagnostics(df: "pd.DataFrame") -> "pd.DataFrame":
    if df.empty:
        return df
    df = df.copy()
    for hand in ("left", "right"):
        raw_cols = [f"{hand}_raw_{axis}" for axis in "xyz"]
        corrected_cols = [f"{hand}_corrected_{axis}" for axis in "xyz"]
        correction_cols = [f"{hand}_correction_{axis}" for axis in "xyz"]
        if all(c in df.columns for c in raw_cols):
            raw = df[raw_cols].apply(pd.to_numeric, errors="coerce")
            df[f"{hand}_raw_step_m"] = _norm3(raw.diff(), raw_cols)
        if all(c in df.columns for c in corrected_cols):
            corrected = df[corrected_cols].apply(pd.to_numeric, errors="coerce")
            df[f"{hand}_corrected_step_m"] = _norm3(corrected.diff(), corrected_cols)
        compensated_cols = [f"{hand}_compensated_{axis}" for axis in "xyz"]
        artifact_cols = [f"{hand}_artifact_{axis}" for axis in "xyz"]
        if all(c in df.columns for c in compensated_cols):
            compensated = df[compensated_cols].apply(pd.to_numeric, errors="coerce")
            df[f"{hand}_compensated_step_m"] = _norm3(compensated.diff(), compensated_cols)
        if all(c in df.columns for c in artifact_cols):
            df[f"{hand}_artifact_step_m"] = _norm3(
                df[artifact_cols].apply(pd.to_numeric, errors="coerce"), artifact_cols
            )
        if all(c in df.columns for c in correction_cols):
            df[f"{hand}_correction_step_m"] = _norm3(
                df[correction_cols].apply(pd.to_numeric, errors="coerce").diff(), correction_cols
            )
            df[f"{hand}_correction_mag_calc_m"] = _norm3(df, correction_cols)

        quat_cols = [f"{hand}_qx", f"{hand}_qy", f"{hand}_qz", f"{hand}_qw"]
        twist_scores: list[float] = []
        angular_steps: list[float] = []
        if all(c in df.columns for c in quat_cols):
            quats = df[quat_cols].apply(pd.to_numeric, errors="coerce").itertuples(index=False, name=None)
            prev = None
            for raw_q in quats:
                q = _quat_norm(tuple(float(v) for v in raw_q)) if all(pd.notna(v) for v in raw_q) else None
                if q is None or prev is None:
                    twist_scores.append(math.nan)
                    angular_steps.append(math.nan)
                    prev = q
                    continue
                dq = _quat_norm(_quat_mul(q, _quat_conj(prev)))
                if dq is None:
                    twist_scores.append(math.nan)
                    angular_steps.append(math.nan)
                    prev = q
                    continue
                x, y, z, w = dq
                if w < 0:
                    x, y, z, w = -x, -y, -z, -w
                sin_half = math.sqrt(max(0.0, 1.0 - min(1.0, w * w)))
                angle = 2.0 * math.atan2(sin_half, max(-1.0, min(1.0, w)))
                if angle < 1e-5 or sin_half < 1e-7:
                    twist_scores.append(0.0)
                    angular_steps.append(angle)
                    prev = q
                    continue
                axis = (x / sin_half, y / sin_half, z / sin_half)
                local_z_world = _rotate_vec_by_quat((0.0, 0.0, 1.0), q)
                score = abs(axis[0] * local_z_world[0] + axis[1] * local_z_world[1] + axis[2] * local_z_world[2])
                twist_scores.append(max(0.0, min(1.0, score)))
                angular_steps.append(angle)
                prev = q
        else:
            twist_scores = [math.nan] * len(df)
            angular_steps = [math.nan] * len(df)
        df[f"{hand}_twist_score"] = twist_scores
        df[f"{hand}_angular_step_rad"] = angular_steps
    return df


def _line_chart(df: "pd.DataFrame", columns: list[str], title: str) -> None:
    st.subheader(title)
    if df.empty or "elapsed_s" not in df.columns:
        st.info("No samples for this plot yet.")
        return
    existing = [c for c in columns if c in df.columns]
    if not existing:
        st.info("No samples for this plot yet.")
        return
    chart_df = df[["elapsed_s", *existing]].copy()
    for c in existing:
        chart_df[c] = pd.to_numeric(chart_df[c], errors="coerce")
    existing = [c for c in existing if chart_df[c].notna().any()]
    if not existing:
        st.info("No numeric samples for this plot yet.")
        return
    try:
        st.line_chart(chart_df[["elapsed_s", *existing]].set_index("elapsed_s"), width="stretch")
    except Exception as e:
        st.warning(f"Could not render chart: {e}")


def _message_rate(times_or_df) -> float:
    if isinstance(times_or_df, pd.DataFrame):
        if len(times_or_df) < 2:
            return 0.0
        first = float(times_or_df["received_at"].iloc[0])
        last = float(times_or_df["received_at"].iloc[-1])
        count = len(times_or_df)
    else:
        if len(times_or_df) < 2:
            return 0.0
        first = float(times_or_df[0])
        last = float(times_or_df[-1])
        count = len(times_or_df)
    duration = max(1e-6, last - first)
    return (count - 1) / duration


def _init_state() -> None:
    st.session_state.setdefault("xr_rows", deque(maxlen=10000))
    st.session_state.setdefault("ik_rows", deque(maxlen=10000))
    st.session_state.setdefault("xr_seen", deque(maxlen=10000))
    st.session_state.setdefault("ik_seen", deque(maxlen=10000))
    st.session_state.setdefault("status", {"status": "idle", "url": DEFAULT_URL})
    st.session_state.setdefault("last_message", None)
    st.session_state.setdefault("last_xr_saved_at", 0.0)
    st.session_state.setdefault("last_ik_saved_at", 0.0)
    st.session_state.setdefault("last_parse_error", "")
    st.session_state.setdefault("stream_url", DEFAULT_URL)
    st.session_state.setdefault("auto_connect", True)


def run_app() -> None:
    st.set_page_config(page_title="VR Teleop Inspector", layout="wide")
    _init_state()

    st.title("VR Teleop Inspector")

    with st.sidebar:
        st.header("Connection")
        url = st.text_input("Relay WebSocket", value=st.session_state.get("stream_url") or st.session_state.status.get("url") or DEFAULT_URL)
        st.session_state.stream_url = url
        auto_connect = st.checkbox("Auto-connect", key="auto_connect")
        window_s = st.slider("Plot window", 5, 300, 30, help="Seconds of recent samples to keep visible.")
        sample_hz = st.slider("Inspector sample rate", 5, 120, DEFAULT_SAMPLE_HZ, help="Caps how many relay messages per second are retained for plotting.")
        max_points = st.slider("Max plotted points", 100, 3000, DEFAULT_MAX_POINTS, 100)
        refresh_s = st.slider("Refresh", 0.1, 2.0, 0.25, 0.05)
        def start_stream() -> None:
            old = st.session_state.get("stream")
            if old is not None:
                old.stop()
            st.session_state.stream = RelayStream(url)
            st.session_state.stream.start()
            st.session_state.status = {"status": "starting", "url": url}
            st.session_state.xr_rows.clear()
            st.session_state.ik_rows.clear()
            st.session_state.xr_seen.clear()
            st.session_state.ik_seen.clear()
            st.session_state.last_xr_saved_at = 0.0
            st.session_state.last_ik_saved_at = 0.0
            st.session_state.last_parse_error = ""

        stream = st.session_state.get("stream")
        if auto_connect and (stream is None or not stream.alive or stream.url != url):
            start_stream()
        cols = st.columns(2)
        if cols[0].button("Reconnect", width="stretch"):
            start_stream()
            st.rerun()
        if cols[1].button("Stop", width="stretch"):
            st.session_state.auto_connect = False
            stream = st.session_state.get("stream")
            if stream is not None:
                stream.stop()
            st.session_state.status = {"status": "stopped", "url": url}
            st.rerun()

    drained = _drain_stream(float(sample_hz), float(window_s))
    status = st.session_state.status
    stream = st.session_state.get("stream")
    connected = bool(stream and stream.alive and status.get("status") == "connected")
    st.caption(f"Status: {status.get('status', 'idle')} | {status.get('url', url)}")
    if status.get("error"):
        st.warning(status["error"])
    if st.session_state.last_parse_error:
        st.warning(f"Last dropped message: {st.session_state.last_parse_error}")

    xr_df = _add_wrist_diagnostics(_frame(st.session_state.xr_rows, float(window_s), int(max_points)))
    ik_df = _frame(st.session_state.ik_rows, float(window_s), int(max_points))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("XR frames", len(st.session_state.xr_rows), f"{_message_rate(st.session_state.xr_seen):.1f} Hz")
    m2.metric("IK states", len(st.session_state.ik_rows), f"{_message_rate(st.session_state.ik_seen):.1f} Hz")
    loop_text = "n/a"
    if "loop_hz" in ik_df and ik_df["loop_hz"].notna().any():
        loop_text = f"{ik_df['loop_hz'].dropna().iloc[-1]:.1f}"
    m3.metric("Loop Hz", loop_text)
    if not ik_df.empty and {"left_engaged", "right_engaged"}.issubset(ik_df.columns):
        last = ik_df.iloc[-1]
        m4.metric("Engaged", f"L {int(last.get('left_engaged', 0))} / R {int(last.get('right_engaged', 0))}")
    else:
        m4.metric("Engaged", "n/a")

    view = st.radio("View", ["Controllers", "Wrist Diagnostics", "Buttons", "IK", "Raw"], horizontal=True)
    st.caption(f"Rendering {len(xr_df)} XR points and {len(ik_df)} IK points. Drained {drained} queued relay messages this refresh.")

    if view == "Controllers":
        c1, c2 = st.columns(2)
        with c1:
            _line_chart(xr_df, ["left_x", "left_y", "left_z", "right_x", "right_y", "right_z"], "Controller Position")
        with c2:
            _line_chart(xr_df, ["viewer_x", "viewer_y", "viewer_z"], "Viewer Position")
        c3, c4 = st.columns(2)
        with c3:
            _line_chart(xr_df, ["left_qx", "left_qy", "left_qz", "left_qw"], "Left Orientation Quaternion")
        with c4:
            _line_chart(xr_df, ["right_qx", "right_qy", "right_qz", "right_qw"], "Right Orientation Quaternion")
    elif view == "Wrist Diagnostics":
        hand = st.segmented_control("Hand", ["left", "right"], default="right")
        hand = str(hand or "right")
        c1, c2 = st.columns(2)
        with c1:
            _line_chart(
                xr_df,
                [
                    f"{hand}_raw_step_m",
                    f"{hand}_corrected_step_m",
                    f"{hand}_compensated_step_m",
                    f"{hand}_artifact_step_m",
                ],
                f"{hand.title()} Frame-to-Frame Translation",
            )
        with c2:
            _line_chart(
                xr_df,
                [f"{hand}_twist_score", f"{hand}_angular_step_rad", f"{hand}_twist_delta_rad_client"],
                f"{hand.title()} Twist Score / Angular Step",
            )
        c3, c4 = st.columns(2)
        with c3:
            _line_chart(
                xr_df,
                [f"{hand}_raw_x", f"{hand}_raw_y", f"{hand}_raw_z"],
                f"{hand.title()} Raw Grip Position",
            )
        with c4:
            _line_chart(
                xr_df,
                [f"{hand}_corrected_x", f"{hand}_corrected_y", f"{hand}_corrected_z"],
                f"{hand.title()} Wrist-Corrected Position",
            )
        c5, c6 = st.columns(2)
        with c5:
            _line_chart(
                xr_df,
                [f"{hand}_compensated_x", f"{hand}_compensated_y", f"{hand}_compensated_z"],
                f"{hand.title()} Twist-Artifact Compensated Position",
            )
        with c6:
            _line_chart(
                xr_df,
                [f"{hand}_artifact_x", f"{hand}_artifact_y", f"{hand}_artifact_z"],
                f"{hand.title()} Subtracted Twist Artifact",
            )
        c7, c8 = st.columns(2)
        with c7:
            _line_chart(
                xr_df,
                [f"{hand}_correction_x", f"{hand}_correction_y", f"{hand}_correction_z"],
                f"{hand.title()} Wrist Correction Delta",
            )
        with c8:
            _line_chart(
                xr_df,
                [
                    f"{hand}_correction_mag",
                    f"{hand}_correction_mag_calc_m",
                    f"{hand}_wrist_correction_enabled",
                    f"{hand}_artifact_calibrated",
                ],
                f"{hand.title()} Correction Magnitude / Enabled",
            )
    elif view == "Buttons":
        c1, c2 = st.columns(2)
        left_cols = [f"left_{name}_value" for name in ("trigger", "grip")]
        left_cols += [f"left_{name}_pressed" for name in ("precision", "handoff", "thumbstick")]
        right_cols = [c.replace("left_", "right_") for c in left_cols]
        with c1:
            _line_chart(xr_df, left_cols, "Left Controller Buttons")
        with c2:
            _line_chart(xr_df, right_cols, "Right Controller Buttons")
    elif view == "IK":
        c1, c2 = st.columns(2)
        with c1:
            _line_chart(ik_df, [f"left_qpos_{i}" for i in range(1, 9)], "Left qpos")
            _line_chart(ik_df, ["left_haptic", "left_force_haptic", "left_engaged"], "Left Haptics / Engagement")
        with c2:
            _line_chart(ik_df, [f"right_qpos_{i}" for i in range(1, 9)], "Right qpos")
            _line_chart(ik_df, ["right_haptic", "right_force_haptic", "right_engaged"], "Right Haptics / Engagement")
    else:
        st.subheader("Latest Message")
        st.json(st.session_state.last_message or {})
        if st.checkbox("Show recent data tables"):
            st.subheader("Recent XR Rows")
            st.dataframe(xr_df.tail(20), width="stretch")
            st.subheader("Recent IK Rows")
            st.dataframe(ik_df.tail(20), width="stretch")

    should_refresh = bool(stream and stream.alive) or status.get("status") in {"starting", "connecting", "error"}
    if should_refresh:
        time.sleep(float(refresh_s))
        st.rerun()


def main() -> None:
    if not st.runtime.exists():
        from streamlit.web import cli as stcli
        import sys

        sys.argv = [
            "streamlit",
            "run",
            __file__,
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
            *sys.argv[1:],
        ]
        raise SystemExit(stcli.main())
    run_app()


if __name__ == "__main__":
    main()
