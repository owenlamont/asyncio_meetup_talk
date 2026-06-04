# Asynchronous Programming with Python code examples

## Presentation Slides

You can find the slide materials below:

* [Google sheets slides](https://docs.google.com/presentation/d/1m6Hznp17GQsVnrGIt7OcbewADWIQqrjvOBab86CrZoA/edit?usp=sharing)

## Running the examples

The easiest way to open and run the async_example.ipynb notebook is by uploading it into
[JupyterLite](https://jupyterlite.github.io/demo/lab/index.html). No need to setup any local Python environment -
you can just run it in a modern browser.

The file_read_example.py and httpx_query_example.py modules have PEP723 metadata so the easiest way to run them is with
[uv](https://docs.astral.sh/uv/) as:

```shell
uv run --script file_read_example.py
```

```shell
uv run --script httpx_query_example.py
```

Which will handle installing the dependencies quickly into a temporary venv.

## Generating the slide animations

The animated GIFs used in the slides are produced by two further PEP723 scripts:

* `asyncio_wait_animation.py` — visualises how `await` lets tasks overlap their wait time.
* `clock_anim.py` — contrasts human time with CPU time (a whole year of CPU time ticks by every few real seconds).

These render frames with Pillow and stitch them together with ffmpeg, so they need `ffmpeg` on your `PATH`. Run them with
uv and pass `--help` (or read the module docstring) for the available options such as resolution, fps and GIF vs MP4
output:

```shell
uv run --script asyncio_wait_animation.py
```

```shell
uv run --script clock_anim.py
```
