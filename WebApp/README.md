# EMG Experiment

A single-window web app that collects labeled EMG for model training. It shows a participant a series of target squeeze levels, records the wristband's signal over USB while they match each one, and downloads a CSV.

The instructed target is the label. Nothing is derived from the signal.

## Requirements

- **Desktop Chrome or Edge.** The app reads the board through the Web Serial API, which Safari and Firefox do not have.
- The nRF5340 DK flashed with the firmware in `../Firmware`, connected by USB. No firmware change is needed; the app reads the `>CH1:<mV>` lines the firmware already prints at 115200 baud.
- Nothing else may hold the serial port (close any serial monitor and the Python tools).

## Run

```sh
npm install
npm run dev        # http://localhost:5173
npm test           # unit tests
npm run check      # type check
npm run build      # production build into dist/
```

Under `npm run dev`, adding `?speed=10` to the URL runs the session clock ten times faster. It has no effect in a production build.

## Demo mode

**Try Demo** on the first screen runs the full session without a wristband, on mock data. It works in any browser, including ones without Web Serial.

- The mock signal is noise around a fixed offset. It does not respond to the cues, so a demo file is useless for training.
- Every screen is marked "Demo" and the file is named `emg_demo_<YYYYMMDD_HHMMSS>.csv`. Keep these out of the training set.

## Deploy to Vercel

1. Import the repository in Vercel.
2. Set **Root Directory** to `WebApp`. Vercel detects Vite; leave the build settings at their defaults.

Web Serial needs HTTPS, which Vercel provides.

## Running a session

1. Enter a participant ID and press **Connect Wristband**. Pick the J-Link port in the browser's dialog.
2. The DK exposes more than one serial port. If the app reports no signal, press **Choose Another Port** and pick the other one.
3. Press **Begin**, then **Start** at the top of each block. Recording runs from **Begin** to the end of the session.
4. When Block 3 ends the CSV downloads automatically. The final screen also has a **Download CSV** button, in case the browser blocked the automatic download.

The top-right corner shows the wristband's status on every screen: not connected, connecting, connected, no signal, or demo mode. "Connected" means samples are arriving, not just that the port is open.

| Block | What the participant does | Length |
| --- | --- | --- |
| 1 Calibration | 10 s rest, then two 3 s maximum squeezes with 7 s rests | 30 s |
| 2 Steady Holds | Levels 1 to 5 in order, three passes; 4 s rest then 3 s hold | 105 s |
| 3 Random Cues | 18 cues in random order, no level twice in a row; 4 s rest then 2 s hold | 108 s |

Timings and the cue count are in `src/lib/protocol.ts`.

**Skip Block** appears under **Start** and at the bottom of the screen while a block is running. It moves straight to the next block, or ends the session if pressed in Block 3.

- A block skipped before it starts has no rows in the CSV.
- A block skipped part-way keeps the rows recorded so far, so it has fewer trials than the table above and its last trial may be cut short. The filename does not mark this; check the trial counts per block when loading.
- If all three blocks are skipped before any starts, the file still downloads but every row is `Block` 0, so it holds nothing usable.

If the cable is pulled or the signal stops mid-session, the run ends and the partial recording can be downloaded (`_partial` in the filename).

## CSV format

One file per session: `emg_<participant>_<YYYYMMDD_HHMMSS>.csv`.

| Column | Meaning |
| --- | --- |
| `Voltage_mV` | Raw sample from the firmware, unfiltered |
| `Count` | Sample index from 0, in arrival order |
| `Time_s` | Seconds since recording started, stamped when the sample's batch arrived. Samples in one batch share a value |
| `Block` | 1 to 3. **0 marks samples captured while a block's intro screen was showing; drop these before training** |
| `Trial` | Squeeze number within the block, from 1. 0 during rest |
| `Target_Level` | Instructed level: 0 for rest, 1 to 5 for a squeeze. Block 1's maximum squeezes are 5 |

The first two columns match the repo's existing recordings, so `emg_core.load_emg_csv` reads these files unchanged.

Two things to account for in the training pipeline:

- Labels mark what the participant was *told* to do. The first few hundred milliseconds of each squeeze and each rest are reaction time, so the signal lags the label at every transition.
- The sample rate is nominally 500 Hz. `Count` and `Time_s` together give the rate actually achieved.
