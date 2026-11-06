# AI Vision board: `embeddedci.aivision`

The AI Vision board (ai-vision-2) watches a device's screen through a camera. It reads text, finds
buttons and whole screens, checks the screen's health, and presses buttons through an actuator.
`embeddedci.aivision` is its client. It uses only the standard library, and it works next to
`embeddedci.benchpod`: the pod powers and talks to the device, the vision board checks what the
device shows.

```python
from embeddedci.aivision import AIVision

with AIVision("ai-vision-2-5dcec6.local") as v:
    v.calibrate(corners=[(212, 98), (1830, 120), (1822, 760), (205, 741)], screen_size=(1920, 720))
    v.set_camera(lock=True)
    v.wait_for("Home", timeout=20)
    v.press("Radio", verify={"text": "FM"})
    assert v.screen_health().ok
    assert v.compare("radio-fm", mask=["clock"]).score > 0.95
    v.frame(space="screen").save("radio.jpg")
```

## Connecting

`AIVision(connection=None, *, token=None, timeout=30.0, lease=None, lease_ttl_s=300)`

| `connection` | Meaning |
| --- | --- |
| `"ai-vision-2-5dcec6.local"`, `"10.1.0.165"` | the board on port 80 |
| `"host:8080"`, `"http://host:8080"` | another port (the agent's simulator listens on 8080) |
| `"discover"` | the one board on the LAN over mDNS (`_aivision._tcp`); needs `embeddedci[discovery]` |
| omitted | the `AIVISION_CONNECTION` environment variable |

`token` is the board's API token (`/etc/ai-vision-agent/token`); it defaults to `AIVISION_TOKEN`,
and a board without a token file needs none.

`lease="<who>"` takes the board's lease when the connection opens, renews it in the background and
releases it on `close()` (or at the end of the `with` block). While someone holds the lease, other
clients get `BusyError` for calls that change something (camera, calibration, references,
actuators, presses, recordings). Reads (`ocr`, `find`, `wait_for`, `compare`, `frame`, ...) always
work. `acquire_lease(who, ttl_s=300)`, `release_lease()` and the `lease` property do the same by
hand.

`discover(timeout=3.0)` lists every board it hears (`DiscoveredBoard`: `hostname`, `addresses`,
`port`, `addr`). `resolve_connection(connection)` gives the base URL a connection string means.

## Coordinates

After `calibrate`, every box (`Box`: `x`, `y`, `w`, `h`, `space`, `center`) and every tap is in
**screen pixels**: the device's own resolution, for example 1920x720. Before calibration, boxes are
in camera pixels (2592x1944). A region argument is `(x, y, w, h)` in the current default space, or
a `Box`, or a dict with `space` (`"screen"`, `"camera"` or `"norm"` for 0..1).

## Calls

| Method | Returns |
| --- | --- |
| `status()`, `capabilities()`, `has(capability)` | board state; feature names such as `ocr`, `find.text`, `actuator.tap` |
| `calibrate(corners, screen_size)` or `calibrate(method="auto"\|"markers")` | `Calibration` (`corners`, `screen_size`, `method`, `quality`) |
| `screen()`, `forget_calibration()` | the current `Calibration` or None |
| `camera()`, `set_camera(lock=, exposure_ms=, gain=, rotation=)` | lock exposure and white balance after calibrating, so health checks see a dim backlight |
| `frame(space=, res=, overlay=, quality=, scale=, after=, format=)` | `Image` (`data`, `content_type`, `frame_id`, `save(path)`); `space="screen"` is the rectified screen |
| `zoom(region, zoom=, width=)` | `Image` of a region at full sensor resolution |
| `snapshot(label)` | `Snapshot` stored on the board (`id`, `urls`) |
| `ocr(region=, zoom=, fresh=, enhance=)` | `OCRResult` (`text`, `lines`, `blocks` of `TextBlock`, `frame_id`) |
| `find(text, label=, template=, match=, region=, near=, ...)` | list of `Element` (`kind`, `label`, `text`, `confidence`, `box`, `center`), best first |
| `detect(engine="ui"\|"live", region=, classes=, ...)` | list of `Element`: buttons, icons and text, or the Hailo model's objects |
| `compare(reference, mask=, region=)` | `Comparison` (`score` 0..1, `changed_pct`, `diff_image`) |
| `identify(min_score=)` | `Identification`: which reference screen is showing |
| `screen_health(window_ms=, expect_motion=, stuck_pixels=, black_luma=, dim_luma=)` | `Health` (`ok`, `problems`, `black`, `dim`, `frozen`, `flicker_hz`, `mean_luma`, ...) |
| `wait_for(text, timeout=10, stable=0, ...)`, `wait_until_gone(text, ...)` | `WaitResult` (`met`, `match`, `matches`, `elapsed_ms`) |
| `wait_until(condition, timeout=, stable=, raise_on_timeout=True)` | `WaitResult` for any board condition (`{"compare": ...}`, `{"identify": "home"}`, `{"health": {...}}`, `{"changed": true}`) |
| `check({"name": condition, ...})` | `CheckResult` (`passed`, `failed`, `results` of `CheckItem`, `evidence_zip`) |
| `press(text, verify=, verify_timeout=, hold_ms=, offset=, actuator=, ...)` | `PressResult` (`found`, `tapped_at`, `verified`, `verify`, `evidence`) |
| `tap(x, y, hold_ms=)`, `swipe(start, end)` or `swipe(direction="left")`, `press_key("VOL_UP")` | the actuator's raw actions |
| `actuators()`, `add_actuator(id, kind, **settings)`, `remove_actuator(id)` | actuators: `kind` `simulated` (logs only) or `http` (a controller) |
| `references()`, `add_reference(name, kind=, image=, from_snapshot=, region=, mask=, overwrite=)`, `delete_reference(name)` | reference screens and templates |
| `export_references(path)`, `import_references(path)` | move a reference set with the test suite as a zip |
| `models()`, `set_live_model(name)` | the Hailo models on the board |
| `with record(path, format="mp4"):` | record the live view for the block |
| `evidence(id, path)`, `download(board_path, path)` | save an evidence zip, a diff image or a snapshot file |

`wait_for` raises `WaitTimeout` when the time runs out; `detail` is the last `WaitResult` (with the
board's `evidence` id). Pass `raise_on_timeout=False` to get `met=False` back instead. A wait longer
than `MAX_WAIT_REQUEST_S` (60 s) runs as several requests to the board, and `stable` then starts
again with each request.

`verify_timeout` on `press`, `tap` and `swipe` is at most `MAX_VERIFY_S` (90 s), because the board
answers within one request; for a longer wait, press without `verify` and call `wait_for`.

`press` of something that is not on the screen raises `NotFoundError`; `detail["visible_text"]`
lists what the board could read.

## Errors

Every error derives from `AIVisionError` (`code`, `status`, `detail`):

| Exception | When |
| --- | --- |
| `TransportError` | the board cannot be reached |
| `AuthError` | missing or wrong token |
| `BadRequestError` | the board rejected the arguments |
| `NotCalibratedError` | the call needs `calibrate` first |
| `BusyError` | someone else holds the lease (`detail["who"]`, `detail["expires"]`) |
| `WaitTimeout` | a wait ran out of time |
| `NotFoundError` | nothing matched |
| `ActuatorError` | the actuator failed or is missing |
| `ModelError` | the OCR worker or a Hailo model failed |
| `CameraError` | no frame from the camera |
| `StorageError` | no SD card on the board |

## pytest

The plugin registers itself. A test asks for the `aivision` fixture; it is skipped when no board is
configured.

```python
def test_radio_screen(aivision, benchpod):
    benchpod.power_on()
    aivision.wait_for("Home", timeout=20)
    aivision.press("Radio", verify={"text": "FM"})
```

```bash
pytest --aivision ai-vision-2-5dcec6.local
```

| Option | Meaning |
| --- | --- |
| `--aivision` | the board (falls back to the `aivision` ini key, then `AIVISION_CONNECTION`) |
| `--aivision-token` | its token (falls back to `AIVISION_TOKEN`) |
| `--aivision-lease` | take the lease under this name for the session (default `pytest`; `""` for none) |
| `--aivision-artifacts` | where a failing test's screen image goes (default `aivision-artifacts/`) |

Fixtures: `aivision` (an open `AIVision`, one per session) and `aivision_connection` (the
connection string). When a test that used `aivision` fails, the plugin saves the screen it saw and
names the file in the report (`screen at failure: ...`, also in the test's `user_properties` as
`aivision_screen`).

## Without a board

The board's agent has a simulator: in the ai-vision-agent repository, `make sim` serves the whole
API on port 8080 with an image file as the camera. `tests/test_aivision_live.py` in this package
runs against it:

```bash
AIVISION_CONNECTION=127.0.0.1:8080 pytest tests/test_aivision_live.py
```
