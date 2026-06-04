# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10", "rich>=13"]
# ///
"""Human-time vs CPU-time animation for an asyncio talk.

Two side-by-side cards (analog clock + nameless-month calendar + readouts).
The left card runs at real wall-clock speed; the right card runs MULT (=1e7)
times faster, so its clock hands are a uniform blur and a *year* ticks by every
~3.2 real seconds while the human second hand barely twitches.

Usage:
    uv run clock_anim.py                 # render the full MP4
    uv run clock_anim.py --still 11      # render one frame at t=11s -> preview.png
    uv run clock_anim.py --gif           # also emit a downscaled looping GIF
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text


class RateColumn(ProgressColumn):
    """Live render rate in frames/second."""

    def render(self, task) -> Text:
        speed = task.finished_speed or task.speed
        if not speed:
            return Text("--.- fps", style="cyan")
        return Text(f"{speed:5.1f} fps", style="cyan")


# ----------------------------------------------------------------------------
# Parameters (override via CLI)
# ----------------------------------------------------------------------------
W, H = 900, 900  # logical layout space (panels hug clock/calendar)
OUT = 1080  # final square output resolution (just rescales the layout)
FPS = 30
DURATION = 60.0  # real seconds (60 = one seamless human-minute loop)
MULT = 1e7  # CPU is this many times faster (≈ 1 GHz / 100 Hz)
SS = 2  # supersample factor for crisp anti-aliasing
BASE_YEAR = 2026
START = dt.datetime(BASE_YEAR, 5, 27, 10, 10, 0)

SECONDS_PER_YEAR = 365.25 * 86400
DAYS_IN_MONTH = 31  # nameless month: cells 1..31, highlight loops over it

# CPU clock: at ×1e7 every hand spins so fast it's genuinely a uniform blurred
# circle. We draw soft concentric rings and fake some *life* with a transparency
# pulse — each ring breathes at its own (loop-synced) rate so they shimmer and
# desync instead of sitting dead-static. The blur is honest; the pulse is candy.
RING_R = {"second": 0.88, "minute": 0.78, "hour": 0.52}  # match human hand lengths
RING_COL = {
    "second": (255, 92, 108),  # match human hand colors:
    "minute": (224, 231, 240),  #   SECOND_RED / HAND_MIN /
    "hour": (198, 210, 228),
}  #   HAND_HOUR
RING_HZ = {"second": 2.5, "minute": 1.2, "hour": 0.6}  # pulse rate (Hz, loop-snapped)
RING_PHASE = {"second": 0.0, "minute": 2.0, "hour": 4.0}  # phase offset, rad
# Drawn hour->minute->second (hour at the bottom/core, second on top). Hour is
# strongest and each outward hand weaker in both base and swing -> a denser core
# fading to a faint rim. Kept translucent so the black clock face shows through
# everywhere, and the top (second) disc washes the whole face a faint red.
RING_BASE = {"hour": 80, "minute": 58, "second": 36}
RING_AMP = {"hour": 12, "minute": 9, "second": 7}
# CPU digital readout: each position changes so fast it blurs into the
# superposition of every digit it can show (respecting the per-position max),
# alpha-oscillating per group like the rings. The colons are static (crisp).
DIGIT_ALPHA = 30  # alpha of each overlaid digit
DIGIT_BASE, DIGIT_AMP = 0.82, 0.18  # opacity pulse (× per group hour/min/sec)
LOOP_PERIOD = DURATION  # exact loop length (set to n/FPS in main)
DRAW_HUMAN_SECOND = True

# ----------------------------------------------------------------------------
# Palette
# ----------------------------------------------------------------------------
BG = (255, 255, 255, 255)  # white outer background (matches the slide)
CARD = (20, 26, 38, 255)
CARD_EDGE = (38, 48, 66, 255)
INK = (232, 237, 246, 255)
MUTED = (138, 150, 173, 255)
FAINT = (70, 82, 104, 255)
HUMAN = (76, 201, 240, 255)  # cool
CPU = (255, 183, 3, 255)  # hot
SECOND_RED = (255, 92, 108, 255)
HAND_MIN = (224, 231, 240, 255)
HAND_HOUR = (198, 210, 228, 255)
HILITE_HUMAN = (37, 99, 235, 255)
COMET = (255, 183, 3, 255)


# ----------------------------------------------------------------------------
# Scaled drawing helpers (author in logical 1x coords, render at SS)
# ----------------------------------------------------------------------------
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, round(size * SS))


FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
SANS = FONT_DIR + "DejaVuSans.ttf"
SANS_B = FONT_DIR + "DejaVuSans-Bold.ttf"
MONO_B = FONT_DIR + "DejaVuSansMono-Bold.ttf"


class Canvas:
    """Thin ImageDraw wrapper that scales logical coordinates by SS."""

    def __init__(self, img: Image.Image):
        self.img = img
        self.d = ImageDraw.Draw(img, "RGBA")

    def line(self, x0, y0, x1, y1, fill, width=1.0):
        self.d.line([x0 * SS, y0 * SS, x1 * SS, y1 * SS], fill=fill, width=max(1, round(width * SS)))

    def ellipse(self, x0, y0, x1, y1, fill=None, outline=None, width=1.0):
        self.d.ellipse(
            [x0 * SS, y0 * SS, x1 * SS, y1 * SS], fill=fill, outline=outline, width=max(1, round(width * SS))
        )

    def circle(self, cx, cy, r, fill=None, outline=None, width=1.0):
        self.ellipse(cx - r, cy - r, cx + r, cy + r, fill, outline, width)

    def rrect(self, x0, y0, x1, y1, r, fill=None, outline=None, width=1.0):
        self.d.rounded_rectangle(
            [x0 * SS, y0 * SS, x1 * SS, y1 * SS],
            radius=r * SS,
            fill=fill,
            outline=outline,
            width=max(1, round(width * SS)),
        )

    def text(self, x, y, s, font, fill, anchor="mm"):
        self.d.text((x * SS, y * SS), s, font=font, fill=fill, anchor=anchor)


# ----------------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------------
CARD_TOP, CARD_BOT = 22, 878
CARDS = {  # two tight panels centered in the square
    "human": {"cx": 274, "x0": 114, "x1": 434},
    "cpu": {"cx": 626, "x0": 466, "x1": 786},
}
CLOCK_CY, CLOCK_R = 268, 128
DIGITAL_Y = 440
YEAR_Y = 506
WEEKDAY_Y = 548
GRID_TOP, CELL = 566, 36
GRID_COLS, GRID_ROWS = 7, 5
ELAPSED_LBL_Y, ELAPSED_VAL_Y = 792, 834


def cell_center(cx: float, idx: int) -> tuple[float, float]:
    row, col = divmod(idx, GRID_COLS)
    gx = cx - GRID_COLS * CELL / 2
    return (gx + col * CELL + CELL / 2, GRID_TOP + row * CELL + CELL / 2)


def draw_clock_face(c: Canvas, cx: float, accent):
    cy, R = CLOCK_CY, CLOCK_R
    c.circle(cx, cy, R + 10, fill=(13, 17, 27, 255), outline=CARD_EDGE, width=2)
    c.circle(cx, cy, R + 10, outline=accent, width=2)
    for i in range(60):
        ang = math.radians(i * 6)
        major = i % 5 == 0
        r_in = R - (12 if major else 6)
        col = INK if major else FAINT
        wid = 2.5 if major else 1.2
        c.line(
            cx + r_in * math.sin(ang),
            cy - r_in * math.cos(ang),
            cx + R * math.sin(ang),
            cy - R * math.cos(ang),
            fill=col,
            width=wid,
        )


def draw_hand(c: Canvas, cx, cy, angle_deg, length, width, fill, back=0.0):
    a = math.radians(angle_deg)
    x1 = cx + length * math.sin(a)
    y1 = cy - length * math.cos(a)
    x0 = cx - back * math.sin(a)
    y0 = cy + back * math.cos(a)
    c.line(x0, y0, x1, y1, fill=fill, width=width)
    c.circle(x1, y1, width * 0.6, fill=fill)


def draw_cpu_clock(frame: Image.Image, cx, cy, t):
    """Draw the CPU clock as soft concentric blurred rings.

    Second/minute/hour are smeared into circles, each pulsing in opacity at its
    own loop-synced rate so they shimmer and breathe out of sync instead of
    looking static.
    """
    box = CLOCK_R + 16
    size = int(2 * box * SS)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    lc = box * SS
    for name in ("hour", "minute", "second"):  # hour at core, second on top
        cycles = max(1, round(RING_HZ[name] * LOOP_PERIOD))  # integer cycles -> loops
        phase = 2 * math.pi * cycles * (t / LOOP_PERIOD) + RING_PHASE[name]
        alpha = max(0, min(255, int(RING_BASE[name] + RING_AMP[name] * math.sin(phase))))
        rad = CLOCK_R * RING_R[name] * SS
        col = RING_COL[name]
        # composite each disc on its own layer so they actually source-over BLEND
        # (a shared ImageDraw overwrites color+alpha -> only the top disc survives)
        disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(disc).ellipse([lc - rad, lc - rad, lc + rad, lc + rad], fill=(col[0], col[1], col[2], alpha))
        layer = Image.alpha_composite(layer, disc)
    layer = layer.filter(ImageFilter.GaussianBlur(radius=0.35 * SS))
    frame.alpha_composite(layer, (int((cx - box) * SS), int((cy - box) * SS)))


# ----------------------------------------------------------------------------
# Static base layer (everything that does not change frame to frame)
# ----------------------------------------------------------------------------
def build_base() -> Image.Image:
    base = Image.new("RGBA", (W * SS, H * SS), BG)
    c = Canvas(base)

    f_hdr = _font(SANS_B, 30)
    f_psub = _font(SANS, 17)
    f_week = _font(SANS_B, 15)
    f_day = _font(SANS, 16)
    f_lbl = _font(SANS_B, 15)
    f_digit = _font(MONO_B, 38)

    cfg = {"human": ("HUMAN TIME", "real wall-clock speed", HUMAN), "cpu": ("CPU TIME", "× 10,000,000 faster", CPU)}
    for key, card in CARDS.items():
        cx = card["cx"]
        label, sub, accent = cfg[key]
        c.rrect(card["x0"], CARD_TOP, card["x1"], CARD_BOT, 22, fill=CARD, outline=CARD_EDGE, width=2)
        c.text(cx, 64, label, f_hdr, accent)
        c.text(cx, 100, sub, f_psub, MUTED)
        draw_clock_face(c, cx, accent)

        # weekday header
        for i, ch in enumerate("SMTWTFS"):
            gx = cx - GRID_COLS * CELL / 2
            c.text(gx + i * CELL + CELL / 2, WEEKDAY_Y, ch, f_week, MUTED)
        # calendar cells 1..31
        for idx in range(GRID_COLS * GRID_ROWS):
            ccx, ccy = cell_center(cx, idx)
            if idx < DAYS_IN_MONTH:
                c.rrect(
                    ccx - CELL / 2 + 2,
                    ccy - CELL / 2 + 2,
                    ccx + CELL / 2 - 2,
                    ccy + CELL / 2 - 2,
                    5,
                    outline=CARD_EDGE,
                    width=1,
                )
                c.text(ccx, ccy, str(idx + 1), f_day, MUTED)

        # static human "today" highlight on day 1
        if key == "human":
            hcx, hcy = cell_center(cx, 0)
            c.rrect(
                hcx - CELL / 2 + 2,
                hcy - CELL / 2 + 2,
                hcx + CELL / 2 - 2,
                hcy + CELL / 2 - 2,
                5,
                fill=HILITE_HUMAN,
                outline=HUMAN,
                width=2,
            )
            c.text(hcx, hcy, "1", f_day, INK)

        c.text(cx, ELAPSED_LBL_Y, "ELAPSED", f_lbl, MUTED)

    # CPU clock blur discs (static) + smeared digital readout
    cpu_cx = CARDS["cpu"]["cx"]

    # CPU digital readout (HH:MM:SS): every digit position spins so fast it blurs
    # into the superposition of all digits it can show. Overlay those digits with
    # real alpha blending per group (hour/min/sec) so they actually combine; bake
    # the static colons crisp. The group layers get pulsed in render_frame.
    global _CPU_DIGIT
    dcol = (255, 205, 135)
    cw = f_digit.getlength("0")  # monospace -> uniform width
    left = cpu_cx * SS - 4 * cw
    dy = DIGITAL_Y * SS
    ox, oy = left - 0.4 * cw, dy - 46 * SS  # small working-region origin
    rw, rh = int(8.8 * cw), int(92 * SS)
    slots = [
        ("hour", range(0, 3)),
        ("hour", range(0, 10)),
        (":", None),
        ("minute", range(0, 6)),
        ("minute", range(0, 10)),
        (":", None),
        ("second", range(0, 6)),
        ("second", range(0, 10)),
    ]
    grp_layers = {g: Image.new("RGBA", (rw, rh), (0, 0, 0, 0)) for g in ("hour", "minute", "second")}
    base_draw = ImageDraw.Draw(base, "RGBA")
    for i, (grp, digits) in enumerate(slots):
        sx = left + (i + 0.5) * cw
        if grp == ":":
            base_draw.text((sx, dy), ":", font=f_digit, fill=(*dcol, 255), anchor="mm")
            continue
        assert digits is not None  # narrow: only the ":" slots carry a None digit range
        for dval in digits:  # each digit its own layer
            sub = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
            ImageDraw.Draw(sub).text(
                (sx - ox, dy - oy), str(dval), font=f_digit, fill=(*dcol, DIGIT_ALPHA), anchor="mm"
            )
            grp_layers[grp] = Image.alpha_composite(grp_layers[grp], sub)
    _CPU_DIGIT = {
        "box": (int(ox), int(oy)),
        "layers": {g: grp_layers[g].filter(ImageFilter.GaussianBlur(1.4 * SS)) for g in grp_layers},
    }

    # human clock hub (over hands? hands drawn per-frame, hub drawn there)
    return base


# ----------------------------------------------------------------------------
# Per-frame dynamic drawing
# ----------------------------------------------------------------------------
F_DIGIT = None
F_YEAR = None
F_EVAL = None
F_CAP = None
_CPU_DIGIT: dict | None = None  # {"box": (x,y), "layers": {group: superposed-digit image}}


def _ensure_fonts():
    global F_DIGIT, F_YEAR, F_EVAL, F_CAP
    if F_DIGIT is None:
        F_DIGIT = _font(MONO_B, 38)
        F_YEAR = _font(SANS_B, 44)
        F_EVAL = _font(SANS_B, 30)
        F_CAP = _font(SANS_B, 27)


def render_frame(base: Image.Image, t: float) -> Image.Image:
    _ensure_fonts()
    frame = base.copy()
    c = Canvas(frame)

    # ---- Human side ----
    now = START + dt.timedelta(seconds=t)
    sec = now.second + now.microsecond / 1e6
    minute = now.minute + sec / 60
    hour = (now.hour % 12) + minute / 60
    hcx = CARDS["human"]["cx"]
    draw_hand(c, hcx, CLOCK_CY, hour / 12 * 360, CLOCK_R * 0.52, 6, HAND_HOUR, back=18)
    draw_hand(c, hcx, CLOCK_CY, minute / 60 * 360, CLOCK_R * 0.78, 4.5, HAND_MIN, back=18)
    if DRAW_HUMAN_SECOND:
        draw_hand(c, hcx, CLOCK_CY, sec / 60 * 360, CLOCK_R * 0.88, 2.2, SECOND_RED, back=24)
    c.circle(hcx, CLOCK_CY, 5, fill=INK)

    c.text(hcx, DIGITAL_Y, now.strftime("%H:%M:%S"), F_DIGIT, HUMAN)
    c.text(hcx, YEAR_Y, str(BASE_YEAR), F_YEAR, INK)
    c.text(hcx, ELAPSED_VAL_Y, f"+{t:0.1f} s", F_EVAL, INK)

    # ---- CPU side ----
    cpu_days = t * MULT / 86400.0
    cpu_years = t * MULT / SECONDS_PER_YEAR
    ccx = CARDS["cpu"]["cx"]

    # blurred-circle hands with a faked transparency pulse (see draw_cpu_clock)
    draw_cpu_clock(frame, ccx, CLOCK_CY, t)
    c.circle(ccx, CLOCK_CY, 5, fill=CPU)

    # superposed digital readout: pulse each group's smear in opacity at the same
    # rate as its ring (colons are already baked crisp into the base)
    assert _CPU_DIGIT is not None  # populated by build_base() before any frame is rendered
    bx, by = _CPU_DIGIT["box"]
    for g in ("hour", "minute", "second"):
        cyc = max(1, round(RING_HZ[g] * LOOP_PERIOD))
        ph = 2 * math.pi * cyc * (t / LOOP_PERIOD) + RING_PHASE[g]
        fac = max(0.0, min(1.0, DIGIT_BASE + DIGIT_AMP * math.sin(ph)))
        lyr = _CPU_DIGIT["layers"][g]
        faded = lyr.copy()
        faded.putalpha(lyr.getchannel("A").point(lambda v, f=fac: int(v * f)))
        frame.alpha_composite(faded, (bx, by))

    # climbing year (the human-watchable readout)
    c.text(ccx, YEAR_Y, str(BASE_YEAR + int(cpu_years)), F_YEAR, CPU)
    c.text(ccx, ELAPSED_VAL_Y, f"+{cpu_years:0.1f} yr", F_EVAL, CPU)

    # comet of "days flying by" on the calendar (drawn on a small glow layer)
    pad = CELL
    gx = ccx - GRID_COLS * CELL / 2
    bx0, by0 = (gx - pad), (GRID_TOP - pad)
    lw = int((GRID_COLS * CELL + 2 * pad) * SS)
    lh = int((GRID_ROWS * CELL + 2 * pad) * SS)
    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer, "RGBA")
    head = int(cpu_days) % DAYS_IN_MONTH
    trail = 14
    for k in range(trail):
        idx = (head - k) % DAYS_IN_MONTH
        a = int(220 * (1 - k / trail) ** 1.5)
        cc_x, cc_y = cell_center(ccx, idx)
        x0 = (cc_x - CELL / 2 + 2 - bx0) * SS
        y0 = (cc_y - CELL / 2 + 2 - by0) * SS
        x1 = (cc_x + CELL / 2 - 2 - bx0) * SS
        y1 = (cc_y + CELL / 2 - 2 - by0) * SS
        ld.rounded_rectangle([x0, y0, x1, y1], radius=5 * SS, fill=(COMET[0], COMET[1], COMET[2], a))
    layer = layer.filter(ImageFilter.GaussianBlur(radius=2.5 * SS))
    frame.alpha_composite(layer, (int(bx0 * SS), int(by0 * SS)))

    return frame.convert("RGB").resize((OUT, OUT), Image.LANCZOS)


# ----------------------------------------------------------------------------
# Encode
# ----------------------------------------------------------------------------
_QUIET = ["-hide_banner", "-loglevel", "warning", "-stats"]


def encode_mp4(frame_dir: str, out: str):
    subprocess.run(
        [
            "ffmpeg",
            *_QUIET,
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            os.path.join(frame_dir, "%05d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            "-movflags",
            "+faststart",
            out,
        ],
        check=True,
    )


def encode_gif(frame_dir: str, out: str):
    subprocess.run(
        [
            "ffmpeg",
            *_QUIET,
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            os.path.join(frame_dir, "%05d.png"),
            "-vf",
            "fps=15,scale=800:-1:flags=lanczos,split[s0][s1];"
            "[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer",
            out,
        ],
        check=True,
    )


def main():
    global DURATION, FPS, MULT, SS, LOOP_PERIOD, DRAW_HUMAN_SECOND
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=DURATION)
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--mult", type=float, default=MULT)
    ap.add_argument("--ss", type=int, default=SS)
    ap.add_argument("--out", default="cpu_vs_human.mp4")
    ap.add_argument("--gif", action="store_true", help="also emit a looping GIF")
    ap.add_argument(
        "--no-human-second", action="store_true", help="hide the human second hand (for clean sub-minute loops)"
    )
    ap.add_argument("--still", type=float, default=None, help="render one frame at time T (s) to --out (png) and exit")
    ap.add_argument("--keep", action="store_true", help="keep PNG frames")
    args = ap.parse_args()

    DURATION, FPS, MULT, SS = args.duration, args.fps, args.mult, args.ss
    DRAW_HUMAN_SECOND = not args.no_human_second
    n = round(DURATION * FPS)
    LOOP_PERIOD = n / FPS  # exact period so hand revs land seamlessly
    base = build_base()

    if args.still is not None:
        out = args.out if args.out.endswith(".png") else "preview.png"
        render_frame(base, args.still).save(out)
        print(f"wrote {out} (t={args.still}s)")
        return

    frame_dir = tempfile.mkdtemp(prefix="clockframes_")
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]rendering frames"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        RateColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as prog:
        task = prog.add_task("render", total=n)
        for i in range(n):
            render_frame(base, i / FPS).save(os.path.join(frame_dir, f"{i:05d}.png"))
            prog.advance(task)

    encode_mp4(frame_dir, args.out)
    print(f"wrote {args.out}")
    if args.gif:
        gif = os.path.splitext(args.out)[0] + ".gif"
        encode_gif(frame_dir, gif)
        print(f"wrote {gif}")
    if not args.keep:
        shutil.rmtree(frame_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
