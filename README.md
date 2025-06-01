# Asynchronous Programming with Python code examples

## Running the examples

The easiest way to open and run the async_example.ipynb notebook is by uploading it into [JupyterLite](https://jupyterlite.github.io/demo/lab/index.html). No need to setup any local Python environment - you can just run it in a modern browser.

The file_read_example.py and httpx_query_example.py modules have PEP723 metadata so the easiest way to run them is with [uv](https://docs.astral.sh/uv/) as:

```shell
uv run --script file_read_example.py
```

```shell
uv run --script httpx_query_example.py
```

Which will handle installing the dependencies quickly into a temporary venv.