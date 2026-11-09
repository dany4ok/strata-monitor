# Strata Monitor

A standalone, read-only Windows desktop widget. It does not load a model, change Strata settings, or write task history.

## One-time setup and launch

Install PySide6 into a private environment for the widget (not Strata's Python):

```powershell
cd path\to\strata-monitor
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then run `launch-strata-monitor.bat`. To use a separate Python interpreter, set `STRATA_MONITOR_PYTHON` to its `pythonw.exe` path before launching.

## Telemetry and limitations

- The widget discovers Strata's local desktop host from its recent desktop log and reads `/api/monitor` and `/api/agent/status`. It also discovers active local inference servers from Strata ready files and reads their existing `/metrics?requests=all` endpoint. API keys, if needed, are read from the matching local native-instance config and sent only to that loopback server; they are never saved or logged by the widget.
- Requests are refreshed every 250 ms using async Qt networking, so brief prompt-reading phases are less likely to be missed. `Tokens/s` shows the engine's mean over the current generation, not its moving instantaneous rate. `Read tok/s` uses only the engine's reported prefill rate; if Strata has not reported one yet, the widget keeps `N/A` rather than reusing an old rate. TTS, TSR, and TTFT use the same adaptive 10/20/30-minute window. TTFT is averaged only from requests that report a time to first token; if none do, it remains `N/A`. These calculations use telemetry already retained by Strata, so the bounded in-memory request history is the limit.
- Success/failure means a verified Strata request finish (`stop`/`length` vs known terminal failures), or the host request's explicit `done`/`error` state when finish details are absent. This is request-level telemetry, not a judgment of an agent's semantic answer quality.
- A menu on right-click selects one of five styles (White, Midnight, Ocean, Forest, Lavender), chooses transparency from opaque through 90%, and toggles Always on Top. Drag the widget to move it and drag any edge/corner to resize. Geometry, style, transparency, and the topmost preference are stored in the user's `AppData\Roaming\StrataMonitor` settings file.
- The right-click menu also includes **Close**.

## Tests

Run the calculation tests without PySide6:

```powershell
py -3 -m unittest discover -s tests -v
```
