# watch

`agentflow watch` shows the state of a run and follows its events.

```text
agentflow watch RUN --once [--json]
agentflow watch RUN [--json] [--cursor BYTE] [--execution ID] [--until]
```

With no `--cursor`, the command prints the current snapshot and then follows events appended after that snapshot. `--cursor BYTE` replays `events.jsonl` from that offset and does not print the snapshot first. `--cursor 0` replays the whole log.

`--once` prints the snapshot and `events_cursor`, then exits. It does not follow and cannot be combined with `--cursor`.

`--json` with `--once` prints one snapshot object. `--json` while following prints one JSON object per event. The two shapes are tied to those two forms above.

`--execution` selects the execution shown in the snapshot. While following, it limits events to that execution. `--until` exits when that execution is `completed`, `failed`, or `cancelled`, and it requires `--execution`. `--once` and `--until` cannot be combined.

The snapshot fields are run, status, execution, time, outputs, and `events_cursor`. Event lines come from `events.jsonl`.

Run files: `agentflow man runs`.
