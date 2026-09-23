# SquelchWT avionics HMI

The client is a fixed 700 × 700 logical-pixel glass display. The square display creates an MFD-like canvas while leaving enough room for grouped DSP controls and the persistent operational console at 150% scaling. The right-hand operational controls and top annunciators remain available on every page. No physical bezel, artificial frequencies or simulated signal telemetry are drawn.

## Research and decisions

Primary sources consulted on 22 September 2026:

- [Garmin GTR 200/200B Pilot's Guide, Rev H](https://static.garmin.com/pumac/190-01553-01_h.pdf), display, softkeys, volume and main-menu sections: separates active radio data from secondary configuration, with compact status labels. This informed the always-visible operational strip and subordinate settings pages.
- [Garmin G3X Touch Pilot's Guide, Rev G](https://static.garmin.com/pumac/190-02472-00_g.pdf), indexed CNS and annunciation sections: use text and color together to identify system status. The full large PDF could not be fetched by the web reader; indexed excerpts were used.
- [Garmin AXIS audio/intercom instructions](https://www8.garmin.com/manuals/webhelp/GUID-7317DC85-4516-4684-BBAF-FD7BE84D5E86/EN-US/GUID-8B40ACF3-9D97-4DF7-80C1-F55F07E2C48E.html): horizontal sliders make audio level changes direct. SquelchWT uses a slider with a persistent numeric readout instead of a mouse-operated imitation rotary knob.
- [Garmin GTN Xi Pilot's Guide, Rev G](https://static.garmin.com/pumac/190-02327-03_g.pdf), indexed audio-panel screenshot and controls: persistent status above a focused configuration page supports operational awareness while editing.
- [Eagle Dynamics F-16C manual](https://www.digitalcombatsimulator.com/upload/iblock/3dc/5p9ejw7gi488n9zd15csrxxs8os2dflk/DCS_F-16C_Early_Access_Guide_EN.pdf), MFD format and symbology controls: display formats have explicit selection and adjustable brightness/contrast. The app retains faint edge shading without scanlines.
- [ITU-R F.1487 Watterson HF channel model](https://www.itu.int/dms_pubrec/itu-r/rec/f/R-REC-F.1487-0-200005-I%21%21PDF-E.pdf): delayed paths with independent complex fading, Doppler offsets, additive noise and interference informed the SSB profile's delayed fading paths and tuning error.
- [UC Berkeley, Sensitivity Analysis for AM Detectors](https://www2.eecs.berkeley.edu/Pubs/TechRpts/2008/EECS-2008-31.html): envelope-detector noise is nonlinear; AM profiles therefore demodulate noisy in-phase/quadrature complex baseband instead of adding only post-detection hiss.
- [ETSI EN 300 698-3](https://www.etsi.org/deliver/etsi_en/300600_300699/30069803/01.01.01_60/en_30069803v010101p.pdf): land/mobile FM measurement uses 6 dB/octave de-emphasis with a time constant of at least 750 µs. FM profiles use a paired 750 µs pre/de-emphasis stage with limiting and in-channel noise.

These are design interpretations, not claims that the application implements aircraft procedures. Garmin contributes hierarchy and direct audio adjustment; fighter displays contribute concise headings and clearly selected pages. Ordinary words, tooltips and keyboard focus take priority over authentic abbreviations.

## Framework and visual system

PySide6 was already installed. Qt replaces Tk for anti-aliased system-font rendering, vector icons, custom immediate tooltips, DPI-aware fixed sizing, keyboard input and queued worker delivery. The backend and `radio_processor.py` remain independent of Qt. The source uses the installed **Hornet Display** family with its **Bold** style, verified with Qt's font database; it falls back to Bahnschrift if absent. No font is bundled.

`ui/theme.py` owns semantic colors, type selection, spacing/radius/line tokens and shared widget styling. The palette is pure black with green symbology, fully blue allied log entries and fully red enemy entries. Operationally disabled ON/OFF controls are red and explicitly labeled. The passive annunciator strip has no button chrome. A low-opacity edge overlay has no scanlines. The NO LINK warning uses restrained red bloom. Every widget, tooltip, menu and dialog uses Hornet Display Bold; there is no secondary UI font.

Qt's frameless top-level window retains a normal Windows application identity. The header invokes native system dragging; minimize and close are real window operations. The client is fixed-size, has no maximize control, and is not always-on-top. Confirmation dialogs default to Cancel, including Enter-key handling.

## Function inventory and final location

| Existing capability | Final location / implementation |
|---|---|
| Chat polling, initial history, reconnect | `engine.RadioService.poll_once`; COMMS and GAME CHAT annunciator |
| Transmission history, allied/enemy colors, clear | COMMS; explicit sender/channel/allegiance, local receipt time, newest at bottom; bounded at 80 records |
| Slang normalization | Existing `slang.json` and regex in `engine.py` |
| Neural/local engines, allied/enemy neural voices | VOICE; original voice catalogs retained |
| Piper installed status, model selection/download, SAPI fallback | VOICE / LOCAL; independent ally/enemy models, per-model percentage progress, model and config validation |
| Test ally, enemy, local, current DSP | Persistent sidebar test with ally/enemy selection; one test pending/synthesizing/queued/playing at a time |
| Slower/faster, rate 0.5–2.5x | Persistent right console |
| Volume 0–1.5, noise 0–1 | Persistent volume and RADIO noise; no change to sound mappings |
| Mute | Persistent console; connection state remains independently visible |
| Radio FX master, PTT, EQ, sync, speaker | RADIO; master also in console; compact state readout always visible |
| 13 profiles + Custom, descriptions | Persistent profile selector; only explicit profile activation applies DSP values |
| Codec cvsd/none; legacy lpc/melp/opus values | RADIO; saved legacy codec appears without rewriting it |
| All 34 sliders, original ranges/steps/formats/help | DSP / SIGNAL, CODEC, DYNAMICS, RF, TX, OUTPUT |
| Existing control help | `ui/help_text.py`, immediate custom tooltips; DSP also has a persistent contextual explanation |
| Reset all + backup, restart | Sidebar beside mute, protected confirmations |
| Save/load and debounce | Backward-compatible JSON schema; 350 ms debounce, close flush, atomic file replacement |
| Speaking/muted/no-link/live | Four independent annunciators, updated from actual state |
| Speech decoding, synthesis, download, playback/save errors | Actionable status notices and normal technical logging |
| Window drag/minimize/close/taskbar | Qt native window operations; queued callbacks disabled during shutdown |

Deliberate interaction changes: new transmissions appear at the bottom in normal chat order; timestamps mean local receipt time (not server time); the log is bounded by records instead of text lines. Common built-in phrases can remain visible while their synthesis is muted through a RADIO toggle and user-editable JSON lists. Named profiles now select coherent AM, FM, SSB or digital channel behavior. Old and custom configurations retain the exact legacy channel path until a named profile is explicitly selected. Test/reset/restart share the persistent sidebar; tests are visibly disabled when muted/busy. Tooltips appear on hover immediately.

## Integration safeguards

- Worker callbacks cross a queued Qt signal/slot and never call widgets directly.
- Each utterance snapshots the radio dictionary, preventing mid-synthesis UI edits from mutating its settings.
- Programmatic widget synchronization blocks value-change signals. Legacy out-of-range values (AM profile LP cutoff 5000 Hz vs editable maximum 3900 Hz) remain intact and readable until the user deliberately adjusts that control.
- The original bounded queues, executor, initial-history suppression and test phrase are retained. A test-busy flag now spans all stages and is cleared on success/failure. Incoming bursts do not evict an accepted test.
- Config replacement is atomic and a failed save produces an actionable notice. Destructive actions retain confirmation and backup behavior.
- Neural network synthesis has a finite timeout; miniaudio decodes its MP3 output without a console subprocess. Background SAPI subprocesses use `CREATE_NO_WINDOW`.

## Validation

`tests/test_avionics.py` exercises the real Qt controls using a temporary configuration: all parameter bindings, every profile, no-write navigation, selection/toggle/voice changes, persistence, error recovery, test exclusion, queued UI delivery, history handling, reset backup, restart dispatch, confirmation defaults, fixed sizing and minimize/restore.

All 29 integration tests passed. `tests/launch_check.py` also passed against the actual entry point: Windows native top-level identity, minimize style, no tool-window/topmost style, duplicate-instance rejection and graceful WM_CLOSE/process exit. Restart dispatch and confirmation are covered with the process replacement call intercepted; a live in-game restart was not exercised.

`tests/visual_check.py` captures every page and each DSP group, with explicit synthetic traffic fixtures. It checks visible widget bounds and single-line label clipping. Run with the native Windows Qt platform: the offscreen plugin on this machine does not discover system fonts and is not a valid font-rendering test. `QT_SCALE_FACTOR` multiplies native screen scaling; use the reported `device_pixel_ratio` to verify the effective scale rather than assuming the environment variable is the final DPI.

Native Windows captures of the updated layout at effective ratios 1.20 and 1.80 passed without widget overflow or single-line label clipping, including download progress, the busiest TX page, tooltip, voice-test menu and confirmation dialog. An example display is in `docs/screenshot.png`. These checks emulate the scaling factor inside Qt; system-wide Windows display preferences were not changed.

`tests/performance_check.py` checks isolated idle polling, memory and download-progress redraws, then synthesizes through a real installed local model without playback. On this Windows machine, idle CPU measured about 1.3% of one core over five seconds, the working set rose by about 0.4 MB, and all 45 timer ticks produced a poll. A full 0–100% progress redraw took about 0.29 seconds. Loading the existing Ryan low model from a cold process took roughly 2.7 seconds; local synthesis plus the Air Cover default profile took about 0.05 seconds of additional radio processing. A separate real-speech pass exercised all 13 profiles: modulation-stage work took roughly 45–102 ms for analog profiles and about 239 ms for the LPC-family digital profile, with finite output and bounded peaks in every case. The app prewarms installed models in the background when LOCAL is active, allied first. Network timings remain variable.

`tests/smoke_audio.py` is an opt-in audible check of real LOCAL and NEURAL synthesis, WAV decoding and Windows playback. Both completed successfully during implementation. The War Thunder endpoint returned 16 records during this pass; no live chat message was audibly played. Polling, history and disconnect/reconnect behavior also passed with controlled input.
