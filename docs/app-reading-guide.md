# Reading `app.py` without knowing much Python

`app.py` runs the local web control panel. It receives requests from the browser, calls project helpers, and sends results back. It does not contain the main job-search loop; that is mostly in `runAiBot.py` and `modules/`.

## A good reading order

1. **File overview and imports** — start at the top. An `import` makes code from Python or another project file available here.
2. **Configuration helpers** — `_load_defaults`, `_effective_config`, and `_coerce` explain how saved settings are loaded and checked.
3. **Bot process helpers** — the functions under “Bot subprocess management” track runs and coordinate start/stop behavior. The platform-specific `identify_job_bot` and `stop_job_bot_process` functions live in [`modules/worker_process.py`](../modules/worker_process.py).
4. **Page routes and job history** — look at `home`, `history`, and `get_applied_jobs` to see how the browser gets pages and history data.
5. **Control-panel API routes** — read `api_get_config` and `api_save_config`, then `api_run`, `api_pause`, `api_resume`, `api_stop`, and `api_status`.
6. **Extra features** — comment drafts, LinkedIn feed scanning, AI prompts/traces, logs, and updates are later in the file.
7. **Startup** — the final `if __name__ == '__main__':` section starts the local web server when you run `app.py` directly.

## The main pattern: a route

A route connects a web address to a Python function. For example:

```python
@app.route('/history')
def history():
    return render_template('index.html')
```

Read this as: “When the browser opens `/history`, run `history()` and return the rendered page.” The `@app.route(...)` line is a decorator: it attaches web behavior to the function directly below it.

Routes with `/api/...` usually exchange data with the page's JavaScript. `request` contains incoming browser data; `jsonify(...)` sends JSON back. A return such as `return jsonify({'error': '...'}), 404` sends both a JSON message and the HTTP status code `404` (not found).

## Python patterns you will see often

- **Names:** `job_id = ...` stores a value. Names beginning with `_`, such as `_coerce`, are helpers meant for use inside this file.
- **Functions:** `def api_run():` defines a reusable block. The indented lines below it are that function's body. Parameters in parentheses receive values from the caller.
- **Dictionaries:** `{'running': True, 'pid': 123}` store related values under labels. They are commonly sent as JSON responses.
- **Lists:** `['a', 'b']` store an ordered group of values.
- **Conditions:** `if ...:` chooses whether an indented block runs; `else:` gives another path.
- **Errors:** `try:` runs code that may fail; `except ...:` handles a failure. `return ..., 500` reports a server error to the browser.
- **Function calls:** `automation_control.status(...)` calls the `status` function from the imported `automation_control` module.
- **`None`:** means “no value” or “not available.”
- **`global`:** says a function will update a variable defined outside that function.

## Follow one action through the code

To understand the **Run** button, trace it in this order:

1. The browser sends a request to `/api/run`.
2. Flask routes it to `api_run()` in `app.py`.
3. `api_run()` checks whether a run already exists and starts `runAiBot.py` as a separate process.
4. `runAiBot.py` performs the job-search work and writes output to a log.
5. The browser asks the API for status and log updates while the process runs.

This gives you a useful distinction: `app.py` is the local control panel and its web API; `runAiBot.py` coordinates job applications; `modules/` contains supporting features.

## Small glossary

| Term | Plain-language meaning |
|---|---|
| Flask | A Python tool used here to create the local web server. |
| Route | A web address connected to a Python function. |
| API | A way for the browser and Python program to exchange data. |
| JSON | A text format for data made of labels and values, lists, and simple values. |
| Subprocess | A separate program started and managed by this app. |
| Decorator | The `@...` line above a function that adds behavior to it. |

You can skip details inside long functions on the first pass. First identify the route, read its docstring/comment, and follow the names of the helper functions it calls.
