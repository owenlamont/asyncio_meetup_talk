# /// script
# requires-python = ">=3.14"
# dependencies = ["pillow>=10.3", "typer>=0.12", "tqdm>=4.66"]
# ///
"""Animated GIF visualising how asyncio parallelises wait time.

Three "tasks" are drawn, each made of three coloured segments:

    [make request] [ ........ wait for response ........ ] [process]

Synchronous timeline (start state):
    the three tasks sit end-to-end on a single row -> one long line on the
    time axis (X = time). This is the slow, blocking story.

The animation then:
    1. holds the synchronous layout for a moment,
    2. staggers the three tasks vertically onto their own rows,
    3. slides them left so the *request* blocks become consecutive and the
       *wait* blocks overlap (run concurrently) -> the efficient async story.

A "time saved" marker shows the total runtime shrinking from the synchronous
finish line to the async finish line.

Render PNG frames with Pillow, then assemble a high-quality looping GIF with
ffmpeg (palettegen/paletteuse). Falls back to Pillow's own GIF encoder if
ffmpeg is unavailable.

Iterate fast with --quick (low fps + no supersampling), then drop --quick for
the smooth final render.

Examples
--------
    uv run asyncio_wait_animation.py --quick --preview
    uv run asyncio_wait_animation.py --fps 30 --supersample 3 -o async.gif
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Annotated

from PIL import Image, ImageDraw, ImageFont
from tqdm import tqdm
import typer


RGB = tuple[int, int, int]

# --------------------------------------------------------------------------- #
# Small maths / colour helpers
# --------------------------------------------------------------------------- #


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease(t: float) -> float:
    """Smoothstep easing (smooth start/stop)."""
    t = clamp(t)
    return t * t * (3.0 - 2.0 * t)


def seg_progress(t: float, start: float, dur: float) -> float:
    """Linear 0..1 progress of a timeline segment [start, start+dur]."""
    if dur <= 1e-9:
        return 1.0 if t >= start else 0.0
    return clamp((t - start) / dur)


def hex_to_rgb(s: str) -> RGB:
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise typer.BadParameter(f"expected a hex colour like #4285F4, got {s!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def lerp_rgb(a: RGB, b: RGB, t: float) -> RGB:
    t = clamp(t)
    return (round(lerp(a[0], b[0], t)), round(lerp(a[1], b[1], t)), round(lerp(a[2], b[2], t)))


_FONT_REGULAR = (
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "Arial.ttf",
)
_FONT_BOLD = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "arialbd.ttf",
)


def load_font(px: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for cand in _FONT_BOLD if bold else _FONT_REGULAR:
        try:
            return ImageFont.truetype(cand, px)
        except OSError:
            continue
    try:  # Pillow >= 10.1 scalable default
        return ImageFont.load_default(px)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# Configuration / geometry (all values in *logical* pixels)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Cfg:
    # counts / sizes
    tasks: int
    req_w: float
    wait_w: float
    proc_w: float
    bar_h: float
    row_gap: float
    radius: float
    ss: int  # supersample factor
    # colours
    bg: RGB
    c_req: RGB
    c_wait: RGB
    c_proc: RGB
    text: RGB
    ghost: RGB
    finish: RGB
    savings_fill: RGB
    axis_col: RGB
    # labels / toggles
    request_label: str
    wait_label: str
    process_label: str
    sync_label: str
    async_label: str
    title_col: RGB
    show_title: bool
    show_legend: bool
    show_axis: bool
    show_savings: bool
    # font sizes
    title_font: int
    legend_font: int
    axis_font: int
    savings_font: int
    # derived geometry (filled in build_cfg)
    width: int = 0
    height: int = 0
    left: float = 0.0
    bars_top: float = 0.0
    bars_bottom: float = 0.0
    bars_center: float = 0.0
    title_cy: float = 0.0
    legend_cy: float = 0.0
    axis_y: float = 0.0
    axis_label_y: float = 0.0
    seq_end_x: float = 0.0
    async_end_x: float = 0.0

    @property
    def task_w(self) -> float:
        return self.req_w + self.wait_w + self.proc_w

    def row_y(self, row: float) -> float:
        return self.bars_top + row * (self.bar_h + self.row_gap)


def build_cfg(**kw) -> Cfg:
    """Compute the canvas + derived layout from the raw options."""
    margin_x, margin_top, margin_bottom = 72.0, 30.0, 30.0
    gap_title, gap_legend, gap_axis = 12.0, 26.0, 24.0

    n = kw["tasks"]
    req_w, wait_w, proc_w = kw["req_w"], kw["wait_w"], kw["proc_w"]
    task_w = req_w + wait_w + proc_w
    bar_h, row_gap = kw["bar_h"], kw["row_gap"]

    # stack the header zones top-down with a running cursor
    y = margin_top
    title_cy = 0.0
    if kw["show_title"]:
        title_h = kw["title_font"] + 14.0
        title_cy = y + title_h / 2.0
        y += title_h + gap_title
    legend_cy = 0.0
    if kw["show_legend"]:
        legend_h = kw["legend_font"] + 12.0
        legend_cy = y + legend_h / 2.0
        y += legend_h + gap_legend

    bars_top = y
    bars_h = n * bar_h + (n - 1) * row_gap
    bars_bottom = bars_top + bars_h

    axis_y = bars_bottom + gap_axis + 10.0
    axis_label_y = axis_y + 12.0
    height = round(axis_label_y + kw["axis_font"] + margin_bottom)
    width = round(margin_x + n * task_w + margin_x)

    return Cfg(
        **kw,
        width=width,
        height=height,
        left=margin_x,
        bars_top=bars_top,
        bars_bottom=bars_bottom,
        bars_center=(bars_top + bars_bottom) / 2.0,
        title_cy=title_cy,
        legend_cy=legend_cy,
        axis_y=axis_y,
        axis_label_y=axis_label_y,
        seq_end_x=margin_x + n * task_w,
        async_end_x=margin_x + (n - 1) * req_w + task_w,
    )


# --------------------------------------------------------------------------- #
# Drawing primitives (logical coords; scaled by S internally)
# --------------------------------------------------------------------------- #


def _box(x0, y0, x1, y1, s):
    return [round(x0 * s), round(y0 * s), round(x1 * s), round(y1 * s)]


def draw_segment(d, s, x0, y0, x1, y1, fill, radius=0.0, corners=None):
    if radius > 0 and corners is not None:
        d.rounded_rectangle(_box(x0, y0, x1, y1, s), radius=round(radius * s), fill=fill, corners=corners)
    else:
        d.rectangle(_box(x0, y0, x1, y1, s), fill=fill)


def draw_task(d, s, cfg: Cfg, x: float, y: float):
    """One task = request | wait | process, drawn at top-left (x, y)."""
    r = min(cfg.radius, cfg.bar_h / 2.0)
    eps = 1.0  # tiny overlap so anti-aliased seams don't leak white
    rx = x + cfg.req_w
    wx = rx + cfg.wait_w
    bottom = y + cfg.bar_h
    # request: round the left corners only
    draw_segment(d, s, x, y, rx + eps, bottom, cfg.c_req, r, (True, False, False, True))
    # wait: square (continuous with neighbours)
    draw_segment(d, s, rx, y, wx + eps, bottom, cfg.c_wait)
    # process: round the right corners only
    draw_segment(d, s, wx, y, x + cfg.task_w, bottom, cfg.c_proc, r, (False, True, True, False))


def text_at(d, s, x, y, text, font, fill, anchor="la"):
    d.text((round(x * s), round(y * s)), text, font=font, fill=fill, anchor=anchor)


def dashed_vline(d, s, x, y0, y1, fill, width, dash=9.0, gap=6.0):
    y = y0
    w = max(1, round(width * s))
    while y < y1:
        d.line([(round(x * s), round(y * s)), (round(x * s), round(min(y + dash, y1) * s))], fill=fill, width=w)
        y += dash + gap


def solid_vline(d, s, x, y0, y1, fill, width):
    d.line([(round(x * s), round(y0 * s)), (round(x * s), round(y1 * s))], fill=fill, width=max(1, round(width * s)))


def double_arrow(d, s, x0, x1, y, fill, width, head=7.0):
    w = max(1, round(width * s))
    d.line([(round(x0 * s), round(y * s)), (round(x1 * s), round(y * s))], fill=fill, width=w)
    for tip, sign in ((x0, 1), (x1, -1)):
        d.polygon(
            [
                (round(tip * s), round(y * s)),
                (round((tip + sign * head) * s), round((y - head * 0.7) * s)),
                (round((tip + sign * head) * s), round((y + head * 0.7) * s)),
            ],
            fill=fill,
        )


def right_arrow(d, s, x0, x1, y, fill, width, head=9.0):
    w = max(1, round(width * s))
    d.line([(round(x0 * s), round(y * s)), (round(x1 * s), round(y * s))], fill=fill, width=w)
    d.polygon(
        [
            (round(x1 * s), round(y * s)),
            (round((x1 - head) * s), round((y - head * 0.6) * s)),
            (round((x1 - head) * s), round((y + head * 0.6) * s)),
        ],
        fill=fill,
    )


# --------------------------------------------------------------------------- #
# Frame rendering
# --------------------------------------------------------------------------- #


def render_frame(cfg: Cfg, fonts: dict, vert_p: float, horiz_p: float) -> Image.Image:
    """Render one frame given vertical-stagger and horizontal-collapse progress."""
    s = cfg.ss
    img = Image.new("RGB", (cfg.width * s, cfg.height * s), cfg.bg)
    d = ImageDraw.Draw(img)

    # current finish line (rightmost extent of any task) interpolates seq -> async
    finish_x = lerp(cfg.seq_end_x, cfg.async_end_x, horiz_p)

    # --- time-saved region (drawn first, sits to the right of all bars) ----- #
    if cfg.show_savings:
        gap_w = cfg.seq_end_x - finish_x
        if gap_w > 4:
            fill = lerp_rgb(cfg.bg, cfg.savings_fill, horiz_p)
            draw_segment(d, s, finish_x, cfg.bars_top - 8, cfg.seq_end_x, cfg.bars_bottom + 8, fill)

    # --- the tasks ---------------------------------------------------------- #
    for k in range(cfg.tasks):
        seq_x = cfg.left + k * cfg.task_w
        async_x = cfg.left + k * cfg.req_w
        x = lerp(seq_x, async_x, horiz_p)
        y = cfg.row_y(lerp(0.0, k, vert_p))
        draw_task(d, s, cfg, x, y)

    # --- finish lines + savings label --------------------------------------- #
    if cfg.show_savings:
        # faint ghost line marking the synchronous (slow) finish
        dashed_vline(d, s, cfg.seq_end_x, cfg.bars_top - 8, cfg.bars_bottom + 8, cfg.ghost, 2.0)
        # solid line marking the current finish
        solid_vline(d, s, finish_x, cfg.bars_top - 8, cfg.bars_bottom + 8, cfg.finish, 2.0)
        gap_w = cfg.seq_end_x - finish_x
        if gap_w > 60:
            mid = (finish_x + cfg.seq_end_x) / 2.0
            col = lerp_rgb(cfg.bg, cfg.text, horiz_p)
            text_at(d, s, mid, cfg.bars_center - 10, "time saved", fonts["savings"], col, "mm")
            double_arrow(d, s, finish_x + 6, cfg.seq_end_x - 6, cfg.bars_center + 12, col, 2.0)

    # --- morphing heading: "Synchronous" -> "Asynchronous" ------------------ #
    if cfg.show_title:
        # crossfade around the collapse, with a brief gap so they never overlap
        a_sync = 1.0 - ease(clamp(horiz_p / 0.45))
        a_async = ease(clamp((horiz_p - 0.55) / 0.45))
        if a_sync > 0.01:
            col = lerp_rgb(cfg.bg, cfg.title_col, a_sync)
            text_at(d, s, cfg.width / 2.0, cfg.title_cy, cfg.sync_label, fonts["title"], col, "mm")
        if a_async > 0.01:
            col = lerp_rgb(cfg.bg, cfg.title_col, a_async)
            text_at(d, s, cfg.width / 2.0, cfg.title_cy, cfg.async_label, fonts["title"], col, "mm")

    # --- legend (static) ---------------------------------------------------- #
    if cfg.show_legend:
        items = [(cfg.c_req, cfg.request_label), (cfg.c_wait, cfg.wait_label), (cfg.c_proc, cfg.process_label)]
        sw = cfg.legend_font  # swatch size
        pad, spacing = 8.0, 30.0
        widths = [sw + pad + d.textlength(lbl, font=fonts["legend"]) / s for _, lbl in items]
        total = sum(widths) + spacing * (len(items) - 1)
        x = (cfg.width - total) / 2.0
        for (col, lbl), w in zip(items, widths, strict=True):
            draw_segment(
                d,
                s,
                x,
                cfg.legend_cy - sw / 2.0,
                x + sw,
                cfg.legend_cy + sw / 2.0,
                col,
                radius=sw * 0.28,
                corners=(True, True, True, True),
            )
            text_at(d, s, x + sw + pad, cfg.legend_cy, lbl, fonts["legend"], cfg.text, "lm")
            x += w + spacing

    # --- time axis ---------------------------------------------------------- #
    if cfg.show_axis:
        right_arrow(d, s, cfg.left - 6, cfg.width - cfg.left + 6, cfg.axis_y, cfg.axis_col, 2.0)
        text_at(d, s, cfg.width / 2.0, cfg.axis_label_y, "time", fonts["axis"], cfg.axis_col, "ma")

    if s != 1:
        img = img.resize((cfg.width, cfg.height), Image.LANCZOS)
    return img


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def encode_ffmpeg(frame_dir: Path, out: Path, fps: int, dither: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg not found on PATH")
    pattern = str(frame_dir / "f_%05d.png")
    palette = frame_dir / "palette.png"
    common = ["-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps), "-i", pattern]
    subprocess.run([ffmpeg, *common, "-vf", "palettegen=stats_mode=full", str(palette)], check=True)
    subprocess.run(
        [ffmpeg, *common, "-i", str(palette), "-lavfi", f"paletteuse=dither={dither}", "-loop", "0", str(out)],
        check=True,
    )


def encode_pillow(frames: list[Image.Image], out: Path, fps: int) -> None:
    duration = round(1000 / fps)
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=duration, loop=0, disposal=2, optimize=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    output: Annotated[Path, typer.Option("--output", "-o", help="Output GIF path.")] = Path("asyncio_wait.gif"),
    # --- timeline (seconds) --------------------------------------------------
    hold_start: Annotated[float, typer.Option(help="Hold the synchronous layout (s).")] = 0.8,
    stagger: Annotated[float, typer.Option(help="Vertical stagger duration (s).")] = 0.9,
    pause_mid: Annotated[float, typer.Option(help="Pause between stagger and collapse (s).")] = 0.3,
    collapse: Annotated[float, typer.Option(help="Horizontal collapse duration (s).")] = 1.1,
    hold_end: Annotated[float, typer.Option(help="Hold the async layout before looping (s).")] = 1.6,
    fps: Annotated[int, typer.Option(help="Frames per second.")] = 25,
    pingpong: Annotated[bool, typer.Option(help="Animate back to the start for a seamless loop.")] = False,
    # --- geometry ------------------------------------------------------------
    tasks: Annotated[int, typer.Option(min=2, max=8, help="Number of tasks/rows.")] = 3,
    req_w: Annotated[float, typer.Option(help="Width of the 'make request' block (px).")] = 66,
    wait_w: Annotated[float, typer.Option(help="Width of the 'wait' block (px).")] = 240,
    proc_w: Annotated[float, typer.Option(help="Width of the 'process' block (px).")] = 66,
    bar_h: Annotated[float, typer.Option(help="Bar height (px).")] = 54,
    row_gap: Annotated[float, typer.Option(help="Vertical gap between staggered rows (px).")] = 26,
    radius: Annotated[float, typer.Option(help="Corner radius of bar ends (px).")] = 11,
    supersample: Annotated[
        int, typer.Option("--supersample", "--ss", min=1, max=4, help="Anti-alias supersampling factor.")
    ] = 2,
    # --- colours -------------------------------------------------------------
    palette: Annotated[
        str, typer.Option(help="Colour preset: 'google' (brand) or 'cvd' (colourblind-safe).")
    ] = "google",
    bg: Annotated[str, typer.Option(help="Background colour (hex).")] = "#FFFFFF",
    request_color: Annotated[str | None, typer.Option(help="Override 'make request' colour (hex).")] = None,
    wait_color: Annotated[str | None, typer.Option(help="Override 'wait' colour (hex).")] = None,
    process_color: Annotated[str | None, typer.Option(help="Override 'process' colour (hex).")] = None,
    # --- labels / toggles ----------------------------------------------------
    request_label: Annotated[str, typer.Option(help="Legend label for the request block.")] = "Make request",
    wait_label: Annotated[str, typer.Option(help="Legend label for the wait block.")] = "Wait for response",
    process_label: Annotated[str, typer.Option(help="Legend label for the process block.")] = "Process response",
    sync_label: Annotated[str, typer.Option(help="Heading shown for the start (sync) state.")] = "Synchronous",
    async_label: Annotated[str, typer.Option(help="Heading shown for the end (async) state.")] = "Asynchronous",
    title: Annotated[bool, typer.Option(help="Show the morphing Synchronous->Asynchronous heading.")] = True,
    legend: Annotated[bool, typer.Option(help="Show the colour legend.")] = True,
    axis: Annotated[bool, typer.Option(help="Show the time axis.")] = True,
    savings: Annotated[bool, typer.Option(help="Show the 'time saved' marker.")] = True,
    # --- workflow ------------------------------------------------------------
    quick: Annotated[bool, typer.Option(help="Fast draft: low fps + no supersampling.")] = False,
    dither: Annotated[str, typer.Option(help="ffmpeg paletteuse dither (none, bayer, sierra2_4a...).")] = "none",
    encoder: Annotated[str, typer.Option(help="auto | ffmpeg | pillow.")] = "auto",
    preview: Annotated[bool, typer.Option(help="Also dump start/stagger/async PNG stills.")] = False,
):
    """Render the asyncio wait-parallelisation animation to a looping GIF."""
    if quick:
        fps = min(fps, 12)
        supersample = 1

    # colourblind-safe (Okabe-Ito) vs the Google brand palette; explicit
    # --*-color flags override whichever preset is chosen.
    palettes = {"cvd": ("#0072B2", "#E69F00", "#009E73"), "google": ("#4285F4", "#FBBC04", "#34A853")}
    if palette not in palettes:
        raise typer.BadParameter(f"palette must be one of {sorted(palettes)}")
    p_req, p_wait, p_proc = palettes[palette]

    cfg = build_cfg(
        tasks=tasks,
        req_w=req_w,
        wait_w=wait_w,
        proc_w=proc_w,
        bar_h=bar_h,
        row_gap=row_gap,
        radius=radius,
        ss=supersample,
        bg=hex_to_rgb(bg),
        c_req=hex_to_rgb(request_color or p_req),
        c_wait=hex_to_rgb(wait_color or p_wait),
        c_proc=hex_to_rgb(process_color or p_proc),
        text=(95, 99, 104),
        ghost=(218, 220, 224),
        finish=(128, 134, 139),
        savings_fill=(232, 240, 254),
        axis_col=(154, 160, 166),
        request_label=request_label,
        wait_label=wait_label,
        process_label=process_label,
        sync_label=sync_label,
        async_label=async_label,
        title_col=(60, 64, 67),
        show_title=title,
        show_legend=legend,
        show_axis=axis,
        show_savings=savings,
        title_font=28,
        legend_font=19,
        axis_font=17,
        savings_font=19,
    )
    s = cfg.ss
    fonts = {
        "title": load_font(cfg.title_font * s, bold=True),
        "legend": load_font(cfg.legend_font * s),
        "axis": load_font(cfg.axis_font * s),
        "savings": load_font(cfg.savings_font * s),
    }

    # progress (eased) for a given time t (seconds)
    t_stagger = hold_start
    t_collapse = hold_start + stagger + pause_mid
    total_t = hold_start + stagger + pause_mid + collapse + hold_end
    n_frames = max(1, round(total_t * fps))

    def progress(t: float) -> tuple[float, float]:
        return (ease(seg_progress(t, t_stagger, stagger)), ease(seg_progress(t, t_collapse, collapse)))

    typer.echo(
        f"Canvas {cfg.width}x{cfg.height}px (ss={s}) | {fps} fps | "
        f"{total_t:.1f}s | {n_frames} frames" + ("  [QUICK DRAFT]" if quick else "")
    )

    output.parent.mkdir(parents=True, exist_ok=True)

    # optional preview stills (synchronous / staggered / async)
    if preview:
        stem = output.with_suffix("")
        for tag, (vp, hp) in {"1_sync": (0.0, 0.0), "2_stagger": (1.0, 0.0), "3_async": (1.0, 1.0)}.items():
            p = Path(f"{stem}_preview_{tag}.png")
            render_frame(cfg, fonts, vp, hp).save(p)
            typer.echo(f"  preview -> {p}")

    use_ffmpeg = encoder == "ffmpeg" or (encoder == "auto" and shutil.which("ffmpeg"))
    if encoder == "ffmpeg" and not shutil.which("ffmpeg"):
        raise typer.BadParameter("encoder=ffmpeg but ffmpeg is not on PATH")

    indices = list(range(n_frames))
    if pingpong and n_frames > 2:
        indices += list(range(n_frames - 2, 0, -1))

    if use_ffmpeg:
        with tempfile.TemporaryDirectory(prefix="asyncio_anim_") as tmp:
            tmp_dir = Path(tmp)
            for out_idx, frame_idx in enumerate(tqdm(indices, desc="render", unit="frame")):
                vp, hp = progress(frame_idx / fps)
                render_frame(cfg, fonts, vp, hp).save(tmp_dir / f"f_{out_idx:05d}.png")
            typer.echo("encoding GIF with ffmpeg...")
            encode_ffmpeg(tmp_dir, output, fps, dither)
    else:
        frames = [render_frame(cfg, fonts, *progress(i / fps)) for i in tqdm(indices, desc="render", unit="frame")]
        typer.echo("encoding GIF with Pillow...")
        encode_pillow(frames, output, fps)

    size_kb = output.stat().st_size / 1024
    typer.echo(f"Wrote {output}  ({size_kb:.0f} KB, {len(indices)} frames)")


if __name__ == "__main__":
    app()
