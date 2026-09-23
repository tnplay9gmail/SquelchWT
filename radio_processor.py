"""Realistic modern military/tactical digital radio transmission DSP chain.

Genuine narrowband vocoder-emulation signal chain applied to raw TTS output
before playback / WAV export.

Chain (all internal processing at narrowband rate, default 8 kHz):

  mono mix -> normalize -> resample to NB -> 300-3400 Hz bandpass
  -> codec round-trip (LPC vocoder | CVSD | Opus-low-bitrate)
  -> hard compressor -> tanh saturation -> PTT/squelch/noise envelope
  -> final limiter -> resample back to input rate

What makes this a *true* digital codec emulation rather than an "LPC
effect" is that the LPC vocoder path now actually behaves like a
bitrate-limited link:

  - LPC filter coefficients are converted to reflection coefficients
    (PARCORs), log-area-ratio (LAR) transformed, and uniformly quantized
    to ``lsf_quant_bits`` per coefficient (the same parameterization used
    by LPC-10 / early tactical secure-voice systems) before synthesis.
  - Pitch period is quantized to a small log-spaced table
    (``pitch_quant_levels``), and frame gain is quantized in dB steps
    (``gain_quant_bits``) -- both are transmitted as coarse indices on a
    real link, not passed through at full precision.
  - ``bit_error_prob`` randomly corrupts one quantized parameter per
    frame (a LAR index, the pitch index, or the gain index), producing
    the characteristic sudden formant/pitch glitch of a marginal digital
    link -- a different failure mode than amplitude-domain dropout.
  - ``frame_loss_prob`` drives real packet-loss concealment (PLC): a
    lost frame reuses the last good frame's quantized parameters at
    decaying gain for up to ``plc_max_hold_frames`` frames, exactly as
    real low-bitrate voice codecs conceal lost frames, instead of
    gating audio to silence.
  - Mixed excitation now has per-pulse jitter/shimmer (glottal
    jitter/shimmer analogue) and spectrally tilted noise so voiced
    frames don't buzz with a perfectly periodic pulse train.
  - The comfort-noise bed's speech/silence ducking is quantized to
    frame boundaries (``comfort_noise_frame_ms``), matching real DTX
    (discontinuous transmission) behavior instead of a smooth analog
    envelope follower.
  - An optional sync/crypto warble burst (``sync_burst_enabled``) can
    precede the carrier ramp, distinct from the mechanical PTT key
    click, reinforcing "digital secure link" rather than analog FM.

Architecture:
  ``RadioConfig`` holds every tunable as a dataclass field (no hardcoded
  inline constants in the DSP path). ``RadioConfig.digital_tactical()``
  is a preset constructor that locks all analog/HF-only fields (tube
  stage, ionospheric fading, heterodyne whistle, multipath, slow AGC) to
  zero so a digital-tactical caller can't accidentally blend in analog
  HF character. ``RadioProcessor`` is standalone with a single
  ``process(audio, sample_rate)`` method so it can be swapped, disabled,
  or A/B tested without touching TTS generation code.

Performance note:
  The full LPC-analysis vocoder step is the most likely bottleneck
  (per-frame Levinson-Durbin + pitch search + quantization). It is
  profiled first in ``process()`` when ``profile=True`` is passed. At
  8 kHz, 20 ms frames with 50% overlap, typical cost is well under TTS
  generation time (tens of ms per utterance of a few seconds).

Missing dependencies are handled gracefully with a logged warning and
fallback rather than silent failure or crash:
  - True MELP (MIL-STD-3005 / STANAG 4591): no maintained Python/C-bindable
    reference binding is available in this environment, so the "melp"
    codec name is accepted as an alias that logs a warning and falls back
    to the manual LPC mixed-excitation vocoder (same LPC mechanism
    family, now with quantization/PLC/bit-errors matching real low-bitrate
    secure-voice behavior).
  - Opus (opuslib / pyogg): if neither import is available, logs a warning
    and falls back to the LPC path.
  - pedalboard.Compressor: if available it is used; otherwise a manual
    envelope-follower compressor with identical parameters is used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from math import gcd

import numpy as np
from scipy.signal import bilinear, butter, hilbert, lfilter, resample_poly, sosfilt

logger = logging.getLogger("squelchwt.radio")

NB_RATE = 8000


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class RadioConfig:
    """All tunable parameters. No DSP constant is hardcoded inline."""

    # Master switches / I/O
    enabled: bool = True
    eq_enabled: bool = True       # bandpass on/off (A/B)
    ptt_enabled: bool = True      # PTT clicks / squelch tail on/off
    noise_amount: float = 0.8     # 0..1+ overall static multiplier (UI slider)
    volume: float = 1.0           # 0..1.5 log-mapped output volume (UI slider)
    output_gain: float = 0.82

    # Modulation-aware audio channel. ``legacy`` is deliberately a strict
    # bypass so existing/custom configurations retain their established
    # sound. Named profiles opt into one of the models below.
    channel_model: str = "legacy"  # legacy | digital | fm | am | ssb
    channel_snr_db: float = 36.0
    fm_preemphasis_us: float = 750.0
    am_modulation_index: float = 0.65
    ssb_tuning_offset_hz: float = 0.0

    # Pre-processing / narrowband rate
    nb_rate: int = 8000
    normalize_peak: float = 0.8

    # Bandpass filter
    hp_cut: float = 300.0
    lp_cut: float = 3400.0
    filter_order: int = 4          # Butterworth; 2 -> 12 dB/oct, 4 -> 24 dB/oct
    filter_type: str = "butter"    # "butter" (Chebyshev also accepted via code path)

    # Codec selection: "lpc" (MELP-style mixed-excitation LPC vocoder,
    #   with real parameter quantization -- see module docstring),
    #   "melp" (alias -> warns + falls back to "lpc"),
    #   "cvsd", "opus", "none" (bypass codec stage)
    codec: str = "lpc"
    opus_bitrate: int = 8000       # 6000..12000, modern-digital-radio alternative

    # LPC vocoder parameters
    lpc_order: int = 12
    lpc_frame_ms: float = 20.0
    lpc_hop_ms: float = 10.0       # 50% overlap with frame -> OLA
    lpc_voiced_noise_mix: float = 0.08   # small noise added to voiced excitation
    lpc_pitch_min_hz: float = 50.0
    lpc_pitch_max_hz: float = 400.0
    lpc_preemph: float = 0.97       # pre-emphasis before LPC analysis
    # (de-emphasis applied after synthesis); 0 disables
    # Parallel dry/vocoder blend: 1.0 = full vocoder, 0.0 = dry bandpassed
    # speech. Keeps vocoder grit audible while guaranteeing intelligibility.
    vocoder_mix: float = 0.7
    # Presence (clarity lift): gentle high-shelf above presence_freq,
    # applied after the codec blend, before the compressor catches peaks.
    presence_db: float = 3.0
    presence_freq: float = 2500.0

    # -- True codec-rate parameter quantization (LPC-10/MELP-family) -----
    # Reflection coefficients are LAR-transformed and uniformly quantized
    # to this many bits each before synthesis -- this is what actually
    # produces authentic low-bitrate digital-voice character, rather than
    # a full-precision LPC filter that merely "sounds vocoder-ish."
    codec_quant_enabled: bool = True
    lsf_quant_bits: int = 5
    pitch_quant_levels: int = 60
    gain_quant_bits: int = 5

    # Per-frame bit-error injection: corrupts one quantized parameter
    # (a reflection-coefficient index, the pitch index, or the gain index)
    # to simulate a marginal digital link -- distinct failure mode from
    # amplitude-domain dropout below (that one simulates fades/gating).
    bit_error_prob: float = 0.02

    # Packet-loss concealment: on a lost frame, reuse the last good
    # frame's quantized parameters at decaying gain instead of gating to
    # silence. This is how real low-bitrate voice codecs conceal loss.
    plc_enabled: bool = True
    frame_loss_prob: float = 0.01
    plc_max_hold_frames: int = 3
    plc_decay: float = 0.55

    # Excitation naturalism: glottal jitter (period wobble) and shimmer
    # (amplitude wobble) on the voiced pulse train, plus a spectral split
    # so the noise component of mixed excitation is tilted toward highs
    # (matches real mixed-excitation LPC, not a flat broadband blend).
    excitation_jitter: float = 0.03
    excitation_shimmer: float = 0.08
    noise_band_split_hz: float = 1000.0

    # CVSD parameters
    cvsd_oversample: int = 4       # 8 kHz * N = N*8 kbps @1 bit (2 -> VINSON 16k)
    cvsd_min_step: float = 0.002
    cvsd_max_step: float = 0.08
    cvsd_leak: float = 0.9995

    # Ionospheric-style slow fading (HF-SSB character). 0 disables.
    # Analog/HF-only: zeroed by RadioConfig.digital_tactical().
    fade_depth_db: float = 0.0     # RMS depth of the slow gain wander
    fade_rate_hz: float = 0.5      # wander rate (fraction of a Hz)

    # Heterodyne whistle (nighttime AM signature): a faint steady tone from
    # an adjacent carrier. 0 disables. Must sit below fs/2 to be audible.
    # Analog/HF-only: zeroed by RadioConfig.digital_tactical().
    het_freq_hz: float = 0.0
    het_level: float = 0.0

    # Valve/tube stage (AM receivers/transmitters only; digital presets
    # leave these at 0): single-ended-triode-style even-harmonic warmth plus
    # slow power-supply sag under loud passages.
    # Analog/HF-only: zeroed by RadioConfig.digital_tactical().
    tube_drive: float = 0.0      # 0 disables; ~0.2-0.4 typical
    tube_sag: float = 0.0        # 0..1 slow gain ducking amount

    # Two-ray multipath selective fading (skywave ordinary + extraordinary
    # paths with independent Rayleigh envelopes + delay -> moving comb
    # notches that wash parts of the audio out independently). 0 disables.
    # Analog/HF-only: zeroed by RadioConfig.digital_tactical().
    mpath_delay_ms: float = 0.0
    mpath_doppler_hz: float = 0.5
    mpath_ratio: float = 0.7      # second-path gain 0..1

    # Dynamics (hard compressor/limiter)
    comp_ratio: float = 8.0
    comp_thresh_db: float = -18.0
    comp_attack_ms: float = 5.0
    comp_release_ms: float = 100.0
    use_pedalboard: bool = True    # try pedalboard.Compressor first, else manual

    # Slow AGC riding fade-induced swings (AM-only; 0 disables, 1 = full).
    # Analog/HF-only: zeroed by RadioConfig.digital_tactical().
    # Tamed (gain 0.6..1.8, slow 0.35 Hz follower, P85 reference) so AM/HF
    # modes keep character without pumping speech volume up/down.
    agc_amount: float = 0.0
    agc_max_gain: float = 1.8
    agc_min_gain: float = 0.6

    # Receiver small-speaker resonance (all modes): a modest peak around
    # speaker_res_freq models the 5-10 cm radio loudspeaker that makes
    # real traffic sound "boxy" even on a clean link. Subtle by design.
    speaker_enabled: bool = True
    speaker_res_freq: float = 950.0
    speaker_res_db: float = 3.5

    # Squelch crash: loud hiss burst at the tail start before the chop
    # (real squelch opens briefly on carrier drop), then abrupt mute.
    squelch_burst_gain: float = 1.6
    squelch_burst_ms: float = 60.0

    # VOX / mic handling: short raised-cosine fades on speech edges
    # simulate syllable clipping from VOX gating + mic handling.
    vox_clip_ms: float = 8.0

    # Saturation (subtle grit AFTER codec so vocoder character is preserved)
    sat_drive: float = 1.5

    # Transmission envelope (per utterance)
    lead_ms: float = 75.0          # silence + carrier/noise ramp before speech
    carrier_ramp_ms: float = 75.0  # AGC/sync-settling noise fade-in (50-100 ms)
    tail_ms: float = 120.0
    tail_hold_ms: float = 40.0
    ptt_dur_ms: float = 7.0        # 5-10 ms key-click
    ptt_freq: float = 240.0
    ptt_gain: float = 0.55
    ptt_decay: float = 260.0
    squelch_dur_ms: float = 16.0
    squelch_gain: float = 0.5

    # Sync/crypto warble burst: brief FSK-like tone-hop preceding the
    # carrier ramp, distinct from the mechanical PTT key click -- real
    # secure digital tactical radios (crypto/frame sync) produce this;
    # analog FM radios do not. Off by default, on in digital_tactical().
    sync_burst_enabled: bool = False
    sync_burst_dur_ms: float = 90.0
    sync_burst_freq_lo: float = 600.0
    sync_burst_freq_hi: float = 2200.0
    sync_burst_gain: float = 0.35

    # Noise floor bed (shaped through the same bandpass)
    noise_level: float = 0.015     # base RMS-ish scale of the static bed
    noise_duck_speech: float = 0.35  # bed multiplier under active speech
    noise_duck_silence: float = 1.0  # bed multiplier during silence
    noise_jitter: float = 0.25     # +/- fractional randomization of noise level
    # DTX-style comfort noise: duck decision is quantized to frame
    # boundaries (real discontinuous-transmission behavior) rather than a
    # continuously smoothed analog envelope follower.
    comfort_noise_frame_ms: float = 20.0

    # Realism randomization
    seed: int | None = None        # settable for reproducibility; None = entropy
    dropout_prob: float = 0.03     # 2-5% per transmission (amplitude-domain fade)
    dropout_min_ms: float = 5.0
    dropout_max_ms: float = 20.0
    click_gain_jitter: float = 0.15
    click_time_jitter_ms: float = 2.0

    # Enemy/mode severity is applied by the caller adapter (kept here so the
    # core process() signature stays process(audio, sample_rate)).
    enemy_noise_mult: float = 1.15
    enemy_drop_mult: float = 1.5

    _calls: int = field(default=0, repr=False, compare=False)

    @classmethod
    def digital_tactical(cls, **overrides) -> "RadioConfig":
        """Preset for a modern digital tactical radio link.

        Locks every analog/HF-only parameter (ionospheric fading,
        heterodyne whistle, tube warmth, multipath, slow AGC) to zero so
        a digital-tactical caller can't accidentally blend in analog HF
        character, and turns on the digital-specific realism features
        (parameter quantization, packet-loss concealment, sync burst).
        Any field can still be overridden via kwargs.
        """
        cfg = cls(
            codec="lpc",
            codec_quant_enabled=True,
            plc_enabled=True,
            sync_burst_enabled=True,
            fade_depth_db=0.0,
            het_freq_hz=0.0,
            het_level=0.0,
            tube_drive=0.0,
            tube_sag=0.0,
            mpath_delay_ms=0.0,
            agc_amount=0.0,
        )
        for key, value in overrides.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
            else:
                logger.warning("unknown RadioConfig field ignored: %s", key)
        return cfg


class CodecUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

class RadioProcessor:
    """Standalone tactical-radio DSP. Single entry point: process()."""

    def __init__(self, config: RadioConfig | None = None, **overrides):
        self.config = config or RadioConfig()
        for key, value in overrides.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
            else:
                logger.warning("unknown RadioConfig field ignored: %s", key)
        self._sos_cache: dict = {}
        self._pedalboard_board = None

    # -- public API ------------------------------------------------------
    def process(self, audio: np.ndarray, sample_rate: int, profile: bool = False) -> np.ndarray:
        """Apply the radio chain. Returns float32 mono at the input rate."""
        t_all = time.perf_counter() if profile else 0.0
        cfg = self.config

        x = np.asarray(audio, dtype=np.float64)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)
        if x.ndim > 1:
            x = x.mean(axis=-1)
        x = x.astype(np.float64, copy=False)

        # Digital-silence gate: never blast PTT clicks + static for an
        # all-zero (or denormal-level) TTS glitch; normalize only real audio.
        peak_in = float(np.max(np.abs(x))) if x.size else 0.0
        if peak_in <= 1e-6:
            return np.zeros(x.shape, dtype=np.float32)

        if not cfg.enabled:
            peak = float(np.max(np.abs(x))) if x.size else 0.0
            if peak > 0:
                x = x / peak * 0.8
            return x.astype(np.float32)

        rng = self._spawn_rng()

        # 1. Pre-processing: normalize, then proper polyphase resample to NB.
        peak = peak_in
        if peak > 1e-9:
            x = x / peak * cfg.normalize_peak
        nb = self._resample(x, int(sample_rate), int(cfg.nb_rate))
        fs = int(cfg.nb_rate)

        # 2. Bandpass 300-3400 Hz via stable SOS cascade.
        if cfg.eq_enabled:
            nb = self._bandpass(nb, fs)

        # 2b. Tube stage (AM-only params; digital presets leave these at 0).
        nb = self._tube(nb, fs)

        # 3. True vocoder / codec round-trip (core digital character).
        t_codec = time.perf_counter() if profile else 0.0
        nb = self._codec_stage(nb, fs, rng)
        if profile:
            logger.info("radio codec stage: %.1f ms (codec=%s)",
                        (time.perf_counter() - t_codec) * 1000, cfg.codec)

        # 3b. Multipath selective fading (AM-only params).
        nb = self._multipath(nb, fs, rng)

        # 3b. Presence: gentle clarity lift before dynamics catch peaks.
        nb = self._presence(nb, fs)

        # 4. Dynamics: hard compressor (audible pumping).
        nb = self._compress(nb, fs)

        # 5. Saturation: light tanh waveshaper AFTER codec artifacts.
        nb = self._saturate(nb)

        # 5a. Modulation-aware transmitter/channel/receiver approximation.
        # Legacy configurations bypass this exactly. The models remain at
        # complex/baseband or audio level so they are inexpensive enough for
        # real-time TTS, while preserving the defining detector mechanisms.
        nb = self._channel_roundtrip(nb, fs, rng)

        # 5b. VOX edge gating: short raised-cosine fades simulate TX VOX
        #     syllable clipping (real push-to-talk/VOX cuts leading/trailing
        #     fragments instead of a sample-perfect start).
        nb = self._vox_edges(nb, fs)

        # 6. Transmission envelope: sync burst, PTT clicks, carrier ramp,
        #    squelch tail, quantized-duck noise bed, randomized dropout.
        nb = self._envelope(nb, fs, rng)

        # 7. Slow AGC: rides fading-induced loudness swings the fast
        #    compressor cannot follow (AM-only param; 0 disables).
        #    Tamed range/rate so AM/HF keep character without pumping.
        nb = self._slow_agc(nb, fs)

        # 7b. Receiver small-speaker resonance (boxy loudspeaker color).
        nb = self._speaker(nb, fs)

        # 8. Output: final limiter, upsample back, volume, ceiling.
        # Clean normalize-to-ceiling (not a hard chop): the VOLUME knob can
        # add up to +20 dB, so instead of flat-topping hot peaks into
        # digital clipping, pull the utterance down to the ceiling and keep
        # the waveform intact. Normal levels are untouched.
        nb = self._limiter(nb)
        out = self._resample(nb, fs, int(sample_rate))
        out = out * float(cfg.output_gain) * _vol_gain(float(cfg.volume))
        peak_out = float(np.max(np.abs(out))) if out.size else 0.0
        if peak_out > 0.98:
            out = out / peak_out * 0.98
        out = out.astype(np.float32)

        if profile:
            logger.info("radio total: %.1f ms for %.2fs utterance @%dHz",
                        (time.perf_counter() - t_all) * 1000,
                        x.size / max(1, int(sample_rate)), int(sample_rate))
        return out

    # -- helpers ---------------------------------------------------------
    def _spawn_rng(self) -> np.random.Generator:
        cfg = self.config
        if cfg.seed is None:
            return np.random.default_rng()
        # Reproducible sequence that still varies per call (no "looped" sound).
        self.config._calls += 1
        return np.random.default_rng(int(cfg.seed) + int(self.config._calls))

    @staticmethod
    def _resample(x: np.ndarray, fs_in: int, fs_out: int) -> np.ndarray:
        if fs_in == fs_out or x.size == 0:
            return x
        g = gcd(int(fs_in), int(fs_out))
        up, down = int(fs_out) // g, int(fs_in) // g
        return resample_poly(x, up, down).astype(np.float64, copy=False)

    def _bandpass_sos(self, fs: int):
        cfg = self.config
        # Clamp user-tunable cutoffs to the narrowband Nyquist: an LP above
        # fs/2 (or HP above LP) would make scipy raise and kill the whole
        # transmission, so sanitize instead of crashing.
        nyq = float(fs) / 2.0
        lp = float(np.clip(cfg.lp_cut, 500.0, nyq * 0.99))
        hp = float(np.clip(cfg.hp_cut, 10.0, lp - 50.0))
        if not np.isfinite(hp) or hp < 10.0:
            hp = 10.0
        if lp <= hp:
            lp = min(hp + 50.0, nyq * 0.99)
        order = int(np.clip(cfg.filter_order, 1, 8))
        if (lp != cfg.lp_cut or hp != cfg.hp_cut):
            logger.warning("bandpass cutoffs clamped to Nyquist: "
                           "hp %s->%s, lp %s->%s (fs=%d)",
                           cfg.hp_cut, round(hp, 1), cfg.lp_cut, round(lp, 1), fs)
        key = (cfg.filter_type, order, hp, lp, fs)
        sos = self._sos_cache.get(key)
        if sos is None:
            if len(self._sos_cache) >= 64:
                self._sos_cache.clear()
            if cfg.filter_type.lower().startswith("cheb"):
                from scipy.signal import cheby1
                sos_hp = cheby1(order, 0.5, hp, btype="highpass",
                                fs=fs, output="sos")
                sos_lp = cheby1(order, 0.5, lp, btype="lowpass",
                                fs=fs, output="sos")
            else:
                sos_hp = butter(order, hp, btype="highpass",
                                fs=fs, output="sos")
                sos_lp = butter(order, lp, btype="lowpass",
                                fs=fs, output="sos")
            sos = (sos_hp, sos_lp)
            self._sos_cache[key] = sos
        return sos

    def _bandpass(self, x: np.ndarray, fs: int) -> np.ndarray:
        sos_hp, sos_lp = self._bandpass_sos(fs)
        return sosfilt(sos_lp, sosfilt(sos_hp, x))

    # -- codec stage -----------------------------------------------------
    def _codec_stage(self, x: np.ndarray, fs: int, rng: np.random.Generator) -> np.ndarray:
        choice = str(self.config.codec or "lpc").lower().strip()
        if choice in ("none", "off", "bypass"):
            return x
        if choice == "melp":
            logger.warning(
                "no maintained Python/C-bindable MELP (MIL-STD-3005/STANAG 4591) "
                "binding available; falling back to manual LPC mixed-excitation "
                "vocoder with codec-rate parameter quantization and packet-loss "
                "concealment (same mechanism family as real low-bitrate MELP).")
            choice = "lpc"
        try:
            if choice == "opus":
                coded = self._opus_roundtrip(x, fs)
            elif choice == "cvsd":
                coded = self._cvsd_roundtrip(x, fs)
            else:
                coded = self._lpc_vocoder(x, fs, rng)
            return self._blend_dry(x, coded)
        except CodecUnavailable as exc:
            logger.warning("%s; falling back to LPC vocoder path", exc)
            if choice != "lpc":
                return self._blend_dry(x, self._lpc_vocoder(x, fs, rng))
            return x

    def _blend_dry(self, dry: np.ndarray, coded: np.ndarray) -> np.ndarray:
        """Parallel dry/vocoder mix (level-matched): intelligibility + grit."""
        mix = float(np.clip(self.config.vocoder_mix, 0.0, 1.0))
        if mix >= 0.999:
            return coded
        if mix <= 0.001 or coded.size != dry.size:
            return dry
        with np.errstate(all="ignore"):
            rms_dry = float(np.sqrt(np.mean(dry * dry)) + 1e-9)
            rms_cod = float(np.sqrt(np.mean(
                np.nan_to_num(coded) * np.nan_to_num(coded))) + 1e-9)
        if rms_cod > 1e-9 and np.isfinite(rms_cod):
            coded = coded * min(2.0, rms_dry / rms_cod)
        return mix * coded + (1.0 - mix) * dry

    # ---- manual LPC mixed-excitation vocoder (genuine LPC artifacts,
    #      with real codec-rate parameter quantization, bit-error
    #      injection, and packet-loss concealment) ----------------------
    def _lpc_vocoder(self, x: np.ndarray, fs: int, rng: np.random.Generator) -> np.ndarray:
        cfg = self.config
        order = int(max(4, cfg.lpc_order))
        # Pre-emphasis before analysis (standard vocoder practice): without
        # it the LPC poles bunch up in the lows and highs go muffled/mumbled.
        pre = float(cfg.lpc_preemph)
        if 0.0 < pre < 1.0:
            xa = lfilter([1.0, -pre], [1.0], x)
        else:
            xa = x
        frame = max(order + 8, int(fs * cfg.lpc_frame_ms / 1000.0))
        hop = max(1, int(fs * cfg.lpc_hop_ms / 1000.0))
        if xa.size < frame:
            frame = max(order + 8, int(xa.size))
            hop = max(1, frame // 2)
        window = np.hanning(frame)
        out = np.zeros(xa.size + frame, dtype=np.float64)
        wsum = np.zeros(xa.size + frame, dtype=np.float64)
        max_energy = float(np.max(xa * xa)) if xa.size else 0.0
        if max_energy <= 1e-12:
            return x

        min_lag = max(2, int(fs / cfg.lpc_pitch_max_hz))
        max_lag = min(frame - 2, int(fs / cfg.lpc_pitch_min_hz))
        if max_lag <= min_lag:
            max_lag = min_lag + 1
        prev_period = 0  # pitch continuity across frames (avoids beepy jumps)
        last_good: dict | None = None  # PLC state: last transmitted frame
        loss_streak = 0
        eps = 1e-9

        pos = 0
        while pos < xa.size:
            seg = np.zeros(frame)
            n = min(frame, xa.size - pos)
            seg[:n] = xa[pos:pos + n]
            win = seg * window

            energy = float(np.sum(win * win) / frame)
            zcr = float(np.mean(np.abs(np.diff(np.signbit(win).astype(np.int8))))) / 2.0 \
                if frame > 1 else 0.5

            # -- Packet-loss concealment: simulate a dropped frame by
            # reusing the last transmitted frame's *quantized* parameters
            # at decaying gain, instead of computing a fresh frame. This
            # is what real low-bitrate voice codecs do on lost frames.
            frame_lost = (cfg.plc_enabled and last_good is not None
                          and loss_streak < int(cfg.plc_max_hold_frames)
                          and float(rng.random()) < float(cfg.frame_loss_prob))

            if frame_lost:
                ks_q = last_good["ks"]
                period = last_good["period"]
                voiced = last_good["voiced"]
                gain_lin = last_good["gain"] * (float(cfg.plc_decay) ** (loss_streak + 1))
                loss_streak += 1
                a = _reflection_to_lpc(ks_q) if ks_q is not None else None
            else:
                raw = _lpc_coeffs(win, order)
                if raw is None:
                    # Near-silence: pass low-level noise so the bed stays alive.
                    out[pos:pos + frame] += np.zeros(frame) * window
                    wsum[pos:pos + frame] += window * window
                    pos += hop
                    continue
                denom, ks = raw
                voiced, period = _classify_voiced(
                    seg, energy, zcr, max_energy, fs, min_lag, max_lag,
                    prev_period=prev_period)
                if voiced:
                    # Light smoothing: prevents single-frame octave jumps
                    # that sound like beeps.
                    if prev_period > 0:
                        period = int(round(0.6 * prev_period + 0.4 * period))
                        period = max(min_lag, min(max_lag, period))
                    prev_period = period

                orig_rms = float(np.sqrt(np.mean(win * win)) + eps)

                if cfg.codec_quant_enabled:
                    # True codec-rate quantization: reflection coefficients
                    # -> LAR -> uniform quantization (LPC-10/MELP-family
                    # parameterization), pitch to a log-spaced table, gain
                    # to dB steps. This -- not the LPC filter itself -- is
                    # what produces authentic low-bitrate digital-voice
                    # character.
                    ks_q = _quantize_reflection_coeffs(ks, int(cfg.lsf_quant_bits))
                    if voiced and period > 1:
                        period = _quantize_pitch(
                            period, fs, cfg.lpc_pitch_min_hz,
                            cfg.lpc_pitch_max_hz, int(cfg.pitch_quant_levels))
                    gain_lin = _quantize_gain_db(orig_rms, int(cfg.gain_quant_bits))
                else:
                    ks_q = ks.copy()
                    gain_lin = orig_rms

                # -- Bit-error injection: corrupt exactly one transmitted
                # parameter to simulate a marginal digital link. Distinct
                # from amplitude-domain dropout: this produces a sudden
                # formant/pitch glitch for one frame, not silence.
                if cfg.bit_error_prob > 0 and float(rng.random()) < float(cfg.bit_error_prob):
                    which = int(rng.integers(0, 3))
                    if which == 0 and ks_q.size > 0:
                        idx = int(rng.integers(0, ks_q.size))
                        k_val = float(np.clip(ks_q[idx], -0.9999, 0.9999))
                        lar = np.log((1.0 + k_val) / (1.0 - k_val) + eps)
                        bit_step = 16.0 / (2 ** max(1, int(cfg.lsf_quant_bits)))
                        lar += float(rng.choice([-1.0, 1.0])) * bit_step * float(rng.integers(1, 4))
                        lar = float(np.clip(lar, -8.0, 8.0))
                        ks_q[idx] = float(np.tanh(lar / 2.0))
                    elif which == 1 and voiced and period > 0:
                        nudge = max(1, period // 10) * int(rng.integers(2, 6))
                        period = int(np.clip(period + int(rng.choice([-1, 1])) * nudge,
                                              min_lag, max_lag))
                    else:
                        gain_lin *= float(rng.uniform(0.35, 1.9))

                a = _reflection_to_lpc(ks_q)
                loss_streak = 0
                last_good = {"ks": ks_q.copy(), "period": period,
                            "voiced": voiced, "gain": gain_lin}

            if a is None:
                synth = np.zeros(frame)
            else:
                exc = _mixed_excitation(frame, fs, voiced, period,
                                        cfg.lpc_voiced_noise_mix, rng,
                                        frame_start=pos,
                                        jitter=float(cfg.excitation_jitter),
                                        shimmer=float(cfg.excitation_shimmer),
                                        noise_split_hz=float(cfg.noise_band_split_hz))
                # Synthesis filter 1/A(z); match quantized frame gain.
                # Guard against unstable frames: if the IIR blows up
                # (non-finite or huge peak), discard it in favour of the
                # excitation at a safe level so one bad frame can't click.
                try:
                    with np.errstate(all="ignore"):
                        synth = lfilter([1.0], a, exc)
                    peak_syn = float(np.max(np.abs(synth))) if synth.size else 0.0
                    if (not np.all(np.isfinite(synth))) or peak_syn > 10.0:
                        synth = exc * 0.3
                except Exception:
                    synth = exc * 0.3
                with np.errstate(all="ignore"):
                    syn_rms = float(np.sqrt(np.mean(np.nan_to_num(synth) ** 2)) + eps)
                if syn_rms > 1e-9:
                    synth *= min(3.0, max(0.0, float(gain_lin)) / syn_rms)

            out[pos:pos + frame] += synth * window
            wsum[pos:pos + frame] += window * window
            pos += hop

        nz = wsum > 1e-6
        out[nz] /= wsum[nz]
        y = out[:xa.size]
        # De-emphasis (inverse of pre-emphasis) restores natural tilt.
        if 0.0 < pre < 1.0:
            with np.errstate(all="ignore"):
                y = lfilter([1.0], [1.0, -pre], np.nan_to_num(y))
        # Safety: never let a synthesis blowup escape.
        peak = float(np.max(np.abs(np.nan_to_num(y))) + 1e-9)
        in_peak = float(np.max(np.abs(x)) + 1e-9)
        if peak > 3.0 * in_peak:
            y *= (3.0 * in_peak / peak)
        return y

    # ---- CVSD round-trip (genuine delta-modulation artifacts) -----------
    def _cvsd_roundtrip(self, x: np.ndarray, fs: int) -> np.ndarray:
        cfg = self.config
        u = int(max(1, cfg.cvsd_oversample))
        up = self._resample(x, fs, fs * u)
        mn, mx = float(cfg.cvsd_min_step), float(cfg.cvsd_max_step)
        leak = float(cfg.cvsd_leak)
        ref = 0.0
        step = mn
        last = [0, 0, 0]
        decoded = np.empty_like(up)
        # Sequential 1-bit loop (feedback); local vars for speed.
        for i, s in enumerate(up):
            bit = 1 if s >= ref else -1
            ref += bit * step
            ref *= leak
            decoded[i] = ref
            b = 1 if bit > 0 else 0
            if b == last[0] == last[1] == last[2]:
                step = min(mx, step * 1.5)
            else:
                step = max(mn, step * 0.85)
            last = [b, last[0], last[1]]
        # Reconstruction low-pass (same NB band) then decimate.
        sos_lp = butter(4, min(cfg.lp_cut, fs * u / 2 - 50),
                        btype="lowpass", fs=fs * u, output="sos")
        decoded = sosfilt(sos_lp, decoded)
        y = self._resample(decoded, fs * u, fs)
        # Level-match to input.
        irms = float(np.sqrt(np.mean(x * x)) + 1e-9)
        orms = float(np.sqrt(np.mean(y * y)) + 1e-9) if y.size else 1.0
        if orms > 1e-9 and y.size:
            y = y[:x.size] * min(2.0, irms / orms)
        return y[:x.size] if y.size >= x.size else np.pad(y, (0, x.size - y.size))

    # ---- Opus low-bitrate round-trip (clean modern digital alternative) --
    def _opus_roundtrip(self, x: np.ndarray, fs: int) -> np.ndarray:
        bitrate = int(np.clip(self.config.opus_bitrate, 6000, 12000))
        # Try opuslib first.
        try:
            import opuslib  # type: ignore
            return _opus_via_opuslib(x, fs, bitrate)
        except Exception as exc_lib:
            lib_err = exc_lib
        # Try pyogg / python-opus fallbacks.
        try:
            import pyogg  # type: ignore  # noqa: F401
            raise CodecUnavailable(
                f"pyogg present but raw Opus packet round-trip not wired "
                f"(opuslib error was: {lib_err}); install opuslib for Opus path")
        except ImportError:
            pass
        raise CodecUnavailable(
            f"Opus codec requested at {bitrate} bps but no Opus binding "
            f"(opuslib/pyogg) is installed (import failed: {lib_err})")

    # -- presence ----------------------------------------------------------
    def _presence(self, x: np.ndarray, fs: int) -> np.ndarray:
        db = float(self.config.presence_db)
        if abs(db) < 0.05 or x.size == 0:
            return x
        fc = float(np.clip(self.config.presence_freq, 500.0, fs * 0.45))
        sos_hp = butter(2, fc, btype="highpass", fs=fs, output="sos")
        with np.errstate(all="ignore"):
            h = sosfilt(sos_hp, x)
            g = 10.0 ** (db / 20.0) - 1.0
            y = np.nan_to_num(x + g * np.nan_to_num(h))
        peak = float(np.max(np.abs(y)) + 1e-12)
        if peak > 1.5:
            y *= 1.5 / peak
        return y

    # -- tube --------------------------------------------------------------
    def _tube(self, x: np.ndarray, fs: int) -> np.ndarray:
        drv = float(self.config.tube_drive)
        sag = float(self.config.tube_sag)
        if (drv <= 0.001 and sag <= 0.001) or x.size == 0:
            return x
        y = x.astype(np.float64, copy=True)
        if drv > 0.001:
            # Asymmetric (single-ended triode style) curve: the square term
            # generates predominantly even harmonics; DC blocked after.
            with np.errstate(all="ignore"):
                y = np.nan_to_num(y + drv * y * y)
            sos_dc = butter(1, 20.0, btype="highpass", fs=fs, output="sos")
            y = sosfilt(sos_dc, np.nan_to_num(y))
            with np.errstate(all="ignore"):
                y = np.tanh(y * (1.0 + drv)) / max(np.tanh(1.0 + drv), 1e-6)
        if sag > 0.001:
            # Supply sag: ~3 Hz envelope rides the gain down on loud passages.
            sos_e = butter(1, 3.0, btype="lowpass", fs=fs, output="sos")
            with np.errstate(all="ignore"):
                e = sosfilt(sos_e, np.abs(y))
                e = np.nan_to_num(e) / max(float(np.max(np.abs(e))), 1e-9)
                y = y * (1.0 - 0.5 * float(np.clip(sag, 0.0, 1.0)) * np.clip(e, 0.0, 1.0))
        peak = float(np.max(np.abs(np.nan_to_num(y))) + 1e-12)
        if peak > 1.5:
            y *= 1.5 / peak
        return y

    # -- multipath selective fading -----------------------------------------
    def _multipath(self, x: np.ndarray, fs: int, rng: np.random.Generator) -> np.ndarray:
        cfg = self.config
        dly = float(cfg.mpath_delay_ms)
        if dly <= 0.01 or x.size < 8:
            return x
        d = max(1, int(fs * dly / 1000.0))
        dop = float(np.clip(cfg.mpath_doppler_hz, 0.05, 10.0))
        ratio = float(np.clip(cfg.mpath_ratio, 0.0, 1.0))
        n = x.size
        sos_f = butter(2, min(dop * 3.0, fs / 2 * 0.9), btype="lowpass",
                       fs=fs, output="sos")

        def rayleigh(m: int) -> np.ndarray:
            a = sosfilt(sos_f, rng.standard_normal(m))
            b = sosfilt(sos_f, rng.standard_normal(m))
            e = np.sqrt(np.nan_to_num(a) ** 2 + np.nan_to_num(b) ** 2)
            e = e / max(float(np.mean(e)), 1e-9)  # mean gain 1
            # Floor nulls: real AGC + limiter never let the path vanish
            # completely; this keeps selective character without speech
            # pumping to silence.
            return 0.35 + 0.65 * np.clip(e, 0.0, 2.5)

        with np.errstate(all="ignore"):
            g1 = rayleigh(n)
            g2 = rayleigh(n)
            xd = np.concatenate([np.zeros(min(d, n)), x[:max(0, n - d)]]) \
                if d < n else np.zeros(n)
            y = np.nan_to_num(x) * g1 + ratio * np.nan_to_num(xd) * g2
            # Power normalization so enabling multipath doesn't jump loudness
            # (previously mean gain was 1+ratio).
            y = y / max(1e-9, (1.0 + ratio * 0.8))
        peak = float(np.max(np.abs(y)) + 1e-12)
        in_peak = float(np.max(np.abs(x)) + 1e-12)
        if peak > 3.0 * in_peak:
            y *= (3.0 * in_peak / peak)
        return y

    # -- dynamics ---------------------------------------------------------
    def _compress(self, x: np.ndarray, fs: int) -> np.ndarray:
        cfg = self.config
        if cfg.use_pedalboard:
            board = self._try_pedalboard(fs)
            if board is not None:
                try:
                    import numpy as _np
                    y = board(_np.asarray(x, dtype=_np.float32), fs)
                    return _np.asarray(y, dtype=_np.float64)
                except Exception as exc:
                    logger.warning("pedalboard.Compressor failed (%s); "
                                   "using manual envelope compressor", exc)
        # Manual envelope-follower gain reduction (fast attack -> pumping).
        atk = np.exp(-1.0 / max(1.0, fs * cfg.comp_attack_ms / 1000.0))
        rel = np.exp(-1.0 / max(1.0, fs * cfg.comp_release_ms / 1000.0))
        env = 0.0
        gain_smooth = 1.0
        y = np.empty_like(x)
        ratio = max(1.0, float(cfg.comp_ratio))
        thresh = float(cfg.comp_thresh_db)
        eps = 1e-9
        for i, s in enumerate(x):
            a = abs(float(s))
            coeff = atk if a > env else rel
            env = coeff * env + (1.0 - coeff) * a
            env_db = 20.0 * np.log10(env + eps)
            if env_db > thresh:
                over = env_db - thresh
                target_db = -over * (1.0 - 1.0 / ratio)
                target = 10.0 ** (target_db / 20.0)
            else:
                target = 1.0
            # Slight smoothing of gain itself avoids zipper noise.
            gain_smooth = 0.6 * gain_smooth + 0.4 * target
            y[i] = float(s) * gain_smooth
        return y

    def _try_pedalboard(self, fs: int):
        if self._pedalboard_board is not None:
            return self._pedalboard_board
        try:
            from pedalboard import Compressor, Pedalboard  # type: ignore
            cfg = self.config
            board = Pedalboard([Compressor(
                threshold_db=float(cfg.comp_thresh_db),
                ratio=float(cfg.comp_ratio),
                attack_ms=float(cfg.comp_attack_ms),
                release_ms=float(cfg.comp_release_ms),
            )])
            self._pedalboard_board = board
            return board
        except Exception:
            self._pedalboard_board = None
            return None

    # -- saturation --------------------------------------------------------
    def _saturate(self, x: np.ndarray) -> np.ndarray:
        drive = max(0.5, float(self.config.sat_drive))
        denom = np.tanh(drive)
        if denom < 1e-6:
            return x
        return np.tanh(x * drive) / denom

    # -- modulation-aware channel -----------------------------------------
    def _channel_roundtrip(self, x: np.ndarray, fs: int,
                           rng: np.random.Generator) -> np.ndarray:
        """Approximate the audible baseband result of a radio link.

        This intentionally leaves ``legacy`` and ``digital`` untouched.
        Digital degradation belongs in the codec/frame-loss stage; adding an
        analog detector on top would create a physically incoherent hybrid.
        """
        model = str(getattr(self.config, "channel_model", "legacy") or "legacy").lower()
        if model in ("legacy", "digital", "none") or x.size < 8:
            return x

        cfg = self.config
        snr_db = float(np.clip(getattr(cfg, "channel_snr_db", 36.0), 6.0, 60.0))
        in_rms = float(np.sqrt(np.mean(np.nan_to_num(x) ** 2)) + 1e-9)

        def level_match(y: np.ndarray) -> np.ndarray:
            y = np.nan_to_num(np.asarray(y, dtype=np.float64))
            out_rms = float(np.sqrt(np.mean(y * y)) + 1e-9)
            if out_rms > 1e-9:
                # A matched transmitter/receiver emphasis pair should be
                # unity-gain for wanted speech. Restore that reference level;
                # the generous safety cap only guards pathological input.
                y *= min(12.0, in_rms / out_rms)
            return y

        if model == "fm":
            # Land-mobile FM applies rising pre-emphasis before the channel
            # and reciprocal de-emphasis after the discriminator. Noise is
            # inserted between them, so receiver de-emphasis suppresses its
            # high-frequency component. Soft limiting approximates the
            # transmitter deviation limiter without clipping flat tops.
            tau = float(np.clip(getattr(cfg, "fm_preemphasis_us", 750.0),
                                50.0, 1000.0)) * 1e-6
            b_pre, a_pre = bilinear([tau, 1.0], [1.0], fs=fs)
            b_de, a_de = bilinear([1.0], [tau, 1.0], fs=fs)
            pre = lfilter(b_pre, a_pre, x)
            pre_rms = float(np.sqrt(np.mean(pre * pre)) + 1e-9)
            pre *= in_rms / pre_rms
            limited = np.tanh(pre * 1.35) / np.tanh(1.35)
            noise_rms = in_rms / (10.0 ** (snr_db / 20.0))
            discriminator = limited + rng.standard_normal(x.size) * noise_rms
            return level_match(lfilter(b_de, a_de, discriminator))

        if model == "am":
            # Ideal noisy AM envelope detector in complex baseband:
            # sqrt((carrier + m*x + nI)^2 + nQ^2). This retains the nonlinear
            # weak-signal behavior absent from simply mixing white noise into
            # already-demodulated speech.
            peak = float(np.max(np.abs(x)) + 1e-9)
            message = np.clip(x / peak, -1.0, 1.0)
            depth = float(np.clip(getattr(cfg, "am_modulation_index", 0.65),
                                  0.1, 0.95))
            sigma = 1.0 / (10.0 ** (snr_db / 20.0))
            ni = rng.standard_normal(x.size) * sigma
            nq = rng.standard_normal(x.size) * sigma
            envelope = np.sqrt((1.0 + depth * message + ni) ** 2 + nq ** 2)
            detected = (envelope - float(np.mean(envelope))) / depth
            return level_match(detected)

        if model == "ssb":
            # Product detection of an analytic SSB signal. Receiver tuning
            # error shifts every speech component by the BFO error, which is
            # the characteristic pitch change of a mistuned SSB receiver.
            analytic = hilbert(np.nan_to_num(x))
            offset = float(np.clip(getattr(cfg, "ssb_tuning_offset_hz", 0.0),
                                   -120.0, 120.0))
            if abs(offset) > 1e-6:
                t = np.arange(x.size, dtype=np.float64) / float(fs)
                analytic *= np.exp(1j * 2.0 * np.pi * offset * t)
            sigma = in_rms / (10.0 ** (snr_db / 20.0))
            analytic += (rng.standard_normal(x.size) +
                         1j * rng.standard_normal(x.size)) * (sigma / np.sqrt(2.0))
            return level_match(np.real(analytic))

        logger.warning("unknown channel model %r; using legacy bypass", model)
        return x

    # -- transmission envelope ---------------------------------------------
    def _envelope(self, x: np.ndarray, fs: int, rng: np.random.Generator) -> np.ndarray:
        cfg = self.config
        lead = int(fs * cfg.lead_ms / 1000.0)
        tail = int(fs * cfg.tail_ms / 1000.0)
        n_speech = x.size

        # Randomized amplitude-domain dropout: gate one 5-20 ms chunk to
        # near-silence (fade/blockage simulation -- distinct from the
        # bit-error/PLC frame-level concealment already applied inside the
        # LPC codec stage).
        speech = x.copy()
        if n_speech > 8 and float(rng.random()) < float(cfg.dropout_prob):
            dmin = int(fs * cfg.dropout_min_ms / 1000.0)
            dmax = int(fs * cfg.dropout_max_ms / 1000.0)
            length = int(rng.integers(max(1, dmin), max(2, dmax + 1)))
            start = int(rng.integers(0, max(1, n_speech - length)))
            speech[start:start + length] *= 0.02

        # Optional sync/crypto warble burst precedes the carrier ramp.
        burst = None
        pre = 0
        if cfg.sync_burst_enabled and float(cfg.sync_burst_gain) > 1e-6:
            burst = _sync_burst(fs, rng, float(cfg.sync_burst_dur_ms),
                                float(cfg.sync_burst_freq_lo),
                                float(cfg.sync_burst_freq_hi),
                                float(cfg.sync_burst_gain))
            pre = int(burst.size)

        lead0 = pre + lead  # offset of speech-lead-in within the full buffer
        total = pre + lead + n_speech + tail
        tx = np.zeros(total, dtype=np.float64)
        if burst is not None and pre > 0:
            tx[:pre] += burst
        tx[lead0:lead0 + n_speech] = speech

        # Slow ionospheric-style fading (HF signature): lowpassed noise
        # wander mapped to dB gain. Off when fade_depth_db is 0.
        # Soft-clipped to +-2 sigma so AM/HF keep character without
        # occasional 12-16 dB volume collapses.
        fd = float(cfg.fade_depth_db)
        if fd > 0.05 and total > 8:
            fr = float(np.clip(cfg.fade_rate_hz, 0.05, 5.0))
            cutoff = min(fr * 2.0, fs / 2 * 0.9)
            sos_f = butter(2, cutoff, btype="lowpass", fs=fs, output="sos")
            with np.errstate(all="ignore"):
                wander = sosfilt(sos_f, rng.standard_normal(total))
                std = float(np.std(np.nan_to_num(wander)))
                if std > 1e-9:
                    wander = wander / std
                wander = np.clip(np.nan_to_num(wander), -2.0, 2.0)
                tx = tx * 10.0 ** (np.nan_to_num(wander) * fd / 20.0)

        # Noise bed: white noise shaped through the same 300-3400 Hz bandpass.
        level = float(cfg.noise_level) * float(cfg.noise_amount)
        level *= float(rng.uniform(1.0 - cfg.noise_jitter, 1.0 + cfg.noise_jitter))
        bed = np.zeros(total)
        if level > 0:
            raw = rng.standard_normal(total)
            bed = self._bandpass(raw, fs)
            std = float(np.std(bed))
            if std > 1e-9:
                bed *= level / std
            # Carrier ramp-in (AGC/sync settling): fade 0->1 over ramp,
            # starting right after any sync burst.
            ramp = min(int(fs * cfg.carrier_ramp_ms / 1000.0), lead)
            if ramp > 0:
                fade_in = np.linspace(0.0, 1.0, ramp)
                bed[pre:pre + ramp] *= fade_in
                if pre > 0:
                    bed[:pre] *= 0.15  # keep bed quiet under the sync burst
            # Squelch tail: loud hiss crash at carrier drop, then hold,
            # then abrupt chop (real squelch opens briefly on drop).
            hold = min(int(fs * cfg.tail_hold_ms / 1000.0), tail)
            if tail > 0:
                tail_fade = np.ones(tail)
                burst_n = min(tail, max(1, int(fs * float(cfg.squelch_burst_ms) / 1000.0)))
                if burst_n > 0:
                    # First ~60 ms stays loud (crash), then fades fast.
                    tail_fade[:burst_n] *= float(np.clip(cfg.squelch_burst_gain, 1.0, 2.5))
                if tail > hold:
                    tail_fade[hold:] *= np.linspace(1.0, 0.0, tail - hold)
                else:
                    tail_fade *= np.linspace(1.0, 0.0, tail)
                # Abrupt final chop instead of fading exactly to zero.
                cut = max(1, int(fs * 0.008))
                if tail > cut:
                    tail_fade[-cut:] = 0.0
                bed[-tail:] *= tail_fade
            # DTX-style comfort noise: duck decision quantized to frame
            # boundaries (real discontinuous-transmission behavior), not a
            # continuously smoothed analog envelope follower. A very short
            # anti-click smoothing pass only softens block-edge steps.
            block = max(1, int(fs * float(cfg.comfort_noise_frame_ms) / 1000.0))
            env = np.abs(tx)
            sos_lp = butter(2, 18, btype="lowpass", fs=fs, output="sos")
            env = sosfilt(sos_lp, env)
            thr = max(1e-4, float(np.max(env)) * 0.08)
            duck = np.empty(total, dtype=np.float64)
            for bstart in range(0, total, block):
                bend = min(total, bstart + block)
                level_block = (cfg.noise_duck_speech
                              if float(np.max(env[bstart:bend])) > thr
                              else cfg.noise_duck_silence)
                duck[bstart:bend] = level_block
            k = max(1, int(fs * 0.002))  # ~2 ms anti-click smoothing only
            if k > 1:
                kernel = np.ones(k) / k
                duck = np.convolve(duck, kernel, mode="same")
            bed *= duck

        out = tx + bed

        # Heterodyne whistle: faint steady adjacent-carrier tone, constant
        # under the whole transmission (hets don't duck with speech).
        het_f = float(cfg.het_freq_hz)
        het_lvl = float(cfg.het_level)
        if het_lvl > 0 and 10.0 < het_f < fs / 2 * 0.95 and len(out) > 0:
            phase0 = float(rng.uniform(0.0, 2 * np.pi))
            t = np.arange(len(out)) / fs
            out = out + het_lvl * np.sin(2 * np.pi * het_f * t + phase0)

        # PTT key-click (head) and squelch tail click (tail): distinct timbres.
        # Head uses switch bounce (two contacts 2-4 ms apart) like a real
        # mechanical PTT.
        if cfg.ptt_enabled:
            j = float(cfg.click_time_jitter_ms) / 1000.0 * fs
            gj = float(cfg.click_gain_jitter)
            head = _key_click(fs, rng, dur_ms=float(cfg.ptt_dur_ms),
                              freq=float(cfg.ptt_freq),
                              gain=float(cfg.ptt_gain) * float(rng.uniform(1 - gj, 1 + gj)),
                              decay=float(cfg.ptt_decay), bright=True)
            bounce = _key_click(fs, rng, dur_ms=float(cfg.ptt_dur_ms) * 0.7,
                                freq=float(cfg.ptt_freq) * 1.3,
                                gain=float(cfg.ptt_gain) * 0.45,
                                decay=float(cfg.ptt_decay) * 1.2, bright=True)
            tail_click = _key_click(fs, rng,
                                    dur_ms=float(cfg.ptt_dur_ms) * 1.6,
                                    freq=float(cfg.ptt_freq) * 0.6,
                                    gain=float(cfg.ptt_gain) * 1.05 *
                                    float(rng.uniform(1 - gj, 1 + gj)),
                                    decay=float(cfg.ptt_decay) * 0.8, bright=False)
            _mix_at(out, max(0, int(rng.uniform(-j, j))), head)
            _mix_at(out, max(0, int(fs * 0.002 + rng.uniform(0, fs * 0.002))), bounce)
            tail_pos = max(0, lead0 + n_speech + int(fs * 0.040)
                           + int(rng.uniform(-j, j)))
            _mix_at(out, tail_pos, tail_click)
        return out

    # -- VOX edge gating + receiver speaker ---------------------------------
    def _vox_edges(self, x: np.ndarray, fs: int) -> np.ndarray:
        ms = float(self.config.vox_clip_ms)
        if ms <= 0.1 or x.size < 16:
            return x
        n = min(x.size, max(1, int(fs * ms / 1000.0)))
        fade = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n))
        y = x.copy()
        y[:n] *= (0.25 + 0.75 * fade)  # leading syllable partially cut, not mute
        y[-n:] *= (0.25 + 0.75 * fade[::-1])
        return y

    def _speaker(self, x: np.ndarray, fs: int) -> np.ndarray:
        cfg = self.config
        if not bool(getattr(cfg, "speaker_enabled", True)) or x.size == 0:
            return x
        try:
            from scipy.signal import iirpeak
            fc = float(np.clip(cfg.speaker_res_freq, 300.0, fs * 0.4))
            q = 1.1
            b, a = iirpeak(fc / (fs / 2.0), q)
            with np.errstate(all="ignore"):
                y = lfilter(b, a, np.nan_to_num(x))
                g = 10.0 ** (float(cfg.speaker_res_db) / 20.0) - 1.0
                y = np.nan_to_num(x + g * np.nan_to_num(y - x))
        except Exception:
            return x
        peak = float(np.max(np.abs(np.nan_to_num(y))) + 1e-12)
        if peak > 1.5:
            y *= 1.5 / peak
        return y

    # -- slow AGC -----------------------------------------------------------
    def _slow_agc(self, x: np.ndarray, fs: int) -> np.ndarray:
        amt = float(np.clip(self.config.agc_amount, 0.0, 1.0))
        if amt <= 0.01 or x.size < fs // 4:
            return x
        lo = float(self.config.agc_min_gain)
        hi = float(self.config.agc_max_gain)
        with np.errstate(all="ignore"):
            # 0.35 Hz follower rides ionospheric fades, not syllables
            # (previously 0.5 Hz tracked syllable energy -> pumping).
            sos_e = butter(1, 0.35, btype="lowpass", fs=fs, output="sos")
            env = sosfilt(sos_e, np.abs(x))
            env = np.nan_to_num(env)
            # P85 reference: less yanked around by pauses than P70.
            target = float(np.percentile(env, 85))
            target = min(max(target, 0.02), 0.4)
            gain = np.clip(target / np.maximum(env, 1e-4), lo, hi)
            # Slow the correction itself so it rides fades, not words.
            sos_g = butter(1, 0.35, btype="lowpass", fs=fs, output="sos")
            gain = sosfilt(sos_g, np.nan_to_num(gain))
            y = x * (np.clip(gain, lo, hi) ** amt)
        peak = float(np.max(np.abs(np.nan_to_num(y))) + 1e-12)
        if peak > 1.5:
            y *= 1.5 / peak
        return y

    # -- output limiter -----------------------------------------------------
    def _limiter(self, x: np.ndarray) -> np.ndarray:
        peak = float(np.max(np.abs(x)) + 1e-12)
        if peak > 0.98:
            x = x / peak * 0.98
        return np.tanh(x * 1.0)


# ---------------------------------------------------------------------------
# Module-level DSP primitives (no hidden constants; all params passed in)
# ---------------------------------------------------------------------------

def _vol_gain(v: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 1.0
    v = max(0.0, min(1.5, v))
    return 10 ** ((v - 1.0) * 2.0)


def _mix_at(target: np.ndarray, pos: int, src: np.ndarray) -> None:
    start = max(0, pos)
    n = max(0, min(len(src) - (start - pos), len(target) - start))
    if n > 0:
        target[start:start + n] += src[start - pos:start - pos + n]


def _key_click(fs: int, rng: np.random.Generator, *, dur_ms: float,
               freq: float, gain: float, decay: float, bright: bool) -> np.ndarray:
    """Short synthesized PTT transient. Head and tail use distinct timbres."""
    n = max(1, int(fs * max(0.005, dur_ms) / 1000.0))
    t = np.arange(n) / fs
    env = np.exp(-t * decay)
    if bright:
        lo, hi = 1200.0, 3600.0   # sharp "key-down" snap
        tone = np.sin(2 * np.pi * freq * 2.0 * t) * 0.4
    else:
        lo, hi = 400.0, 2000.0    # duller "squelch-tail" thump
        tone = np.sin(2 * np.pi * freq * t) * 0.7
    sos = butter(2, [lo, hi], btype="bandpass", fs=fs, output="sos")
    noise = sosfilt(sos, rng.standard_normal(n))
    noise /= max(float(np.std(noise)), 1e-3)
    click = (noise * gain + tone * gain * 0.6) * env
    attack = min(n, max(1, int(fs * 0.0008)))
    click[:attack] *= np.linspace(0.2, 1.0, attack)
    return click


def _sync_burst(fs: int, rng: np.random.Generator, dur_ms: float,
                freq_lo: float, freq_hi: float, gain: float) -> np.ndarray:
    """Deterministic MSK-like crypto/frame-sync preamble.

    Real secure links send a fixed alternating preamble (not random
    warble): alternating lo/hi tones with phase continuity plus tiny
    timing jitter. Distinct from the mechanical PTT key click.
    """
    n = max(1, int(fs * max(1.0, dur_ms) / 1000.0))
    if n <= 1:
        return np.zeros(0)
    n_steps = int(np.clip(round(dur_ms / 15.0), 3, 10))
    lo = max(50.0, float(freq_lo))
    hi = min(float(fs) * 0.45, float(freq_hi))
    if hi <= lo:
        hi = lo + 100.0
    # Fixed alternating preamble with a 2-step sync word at the end.
    freqs = [lo if i % 2 == 0 else hi for i in range(n_steps)]
    if n_steps >= 2:
        freqs[-2:] = [hi, hi]  # sync word
    seg = max(1, n // n_steps)
    out = np.zeros(n)
    idx = 0
    phase = 0.0
    for f in freqs:
        m = min(seg, n - idx)
        if m <= 0:
            break
        t_local = np.arange(m) / fs
        out[idx:idx + m] = np.sin(2 * np.pi * float(f) * t_local + phase)
        phase = float((phase + 2 * np.pi * float(f) * m / fs) % (2 * np.pi))
        idx += m
    if idx < n:
        out[idx:] = out[idx - 1] if idx > 0 else 0.0
    env = np.ones(n)
    fade = max(1, int(fs * 0.003))
    fade = min(fade, n // 2) if n > 1 else 1
    if fade > 0:
        env[:fade] = np.linspace(0.0, 1.0, fade)
        env[-fade:] = np.linspace(1.0, 0.0, fade)
    return out * env * float(gain)


def _lpc_coeffs(frame: np.ndarray, order: int):
    """Levinson-Durbin LPC from windowed frame.

    Returns ``(denom, ks)`` where ``denom`` is the synthesis-filter
    denominator ``[1, -a1, -a2, ...]`` for use with ``lfilter``, and
    ``ks`` are the reflection (PARCOR) coefficients produced along the
    way -- these are what get LAR-quantized to simulate real codec-rate
    transmission (see ``_quantize_reflection_coeffs`` /
    ``_reflection_to_lpc``). Returns ``None`` if the frame is degenerate.
    """
    with np.errstate(all="ignore"):
        r = np.correlate(frame, frame, mode="full")[len(frame) - 1:len(frame) + order]
    if not np.all(np.isfinite(r)) or r[0] <= 1e-12:
        return None
    a = np.zeros(order + 1)
    ks = np.zeros(order)
    e = float(r[0])
    a[0] = 1.0
    for i in range(1, order + 1):
        with np.errstate(all="ignore"):
            acc = sum(a[j] * r[i - j] for j in range(1, i))
            if not np.isfinite(acc):
                return None
            k = (r[i] - acc) / max(e, 1e-12)
            if not np.isfinite(k):
                return None
            k = float(np.clip(k, -0.9999, 0.9999))
            a[i] = k
            ks[i - 1] = k
            for j in range(1, i):
                a[j] -= k * a[i - j]
            e *= max(1e-9, 1.0 - k * k)
            if not np.isfinite(e) or e <= 0:
                return None
    # Synthesis denominator is A(z) = 1 - sum a_k z^-k for k>=1 as Levinson
    # above solves with that convention; negate tail for lfilter use.
    denom = np.empty(order + 1)
    denom[0] = 1.0
    denom[1:] = -a[1:]
    return denom, ks


def _reflection_to_lpc(ks: np.ndarray) -> np.ndarray | None:
    """Rebuild the LPC synthesis-filter denominator from reflection
    coefficients via the standard forward Levinson recursion.

    This is the reconstruction half of the quantization round-trip: the
    encoder-side reflection coefficients get LAR-quantized (lossy, as on
    a real link), and this function rebuilds the filter the decoder would
    actually synthesize from those quantized coefficients -- so the
    filter really is bitrate-limited, not just LPC-shaped.
    """
    if ks is None or ks.size == 0:
        return None
    order = ks.size
    a = np.zeros(order + 1)
    a[0] = 1.0
    for i in range(1, order + 1):
        k = float(np.clip(ks[i - 1], -0.9999, 0.9999))
        new_a = a.copy()
        new_a[i] = k
        for j in range(1, i):
            new_a[j] = a[j] - k * a[i - j]
        a = new_a
    if not np.all(np.isfinite(a)):
        return None
    denom = np.empty(order + 1)
    denom[0] = 1.0
    denom[1:] = -a[1:]
    return denom


def _quantize_reflection_coeffs(ks: np.ndarray, bits: int) -> np.ndarray:
    """Uniformly quantize reflection coefficients in the log-area-ratio
    (LAR) domain -- the parameterization used by LPC-10 and early
    tactical secure-voice systems, chosen because it gives roughly
    perceptually-uniform quantization error across the coefficient range
    (a plain uniform quantizer on ``k`` itself under-resolves near
    +/-1 where formant bandwidth is most sensitive).

    LAR = log((1+k)/(1-k)); k = tanh(LAR/2).
    """
    bits = int(max(1, bits))
    k = np.clip(np.asarray(ks, dtype=np.float64), -0.9999, 0.9999)
    with np.errstate(all="ignore"):
        lar = np.log((1.0 + k) / (1.0 - k))
    lar = np.clip(np.nan_to_num(lar), -8.0, 8.0)
    levels = 2 ** bits
    step = 16.0 / levels
    lar_q = np.round(lar / step) * step
    lar_q = np.clip(lar_q, -8.0, 8.0)
    return np.tanh(lar_q / 2.0)


def _quantize_pitch(period: int, fs: int, min_hz: float, max_hz: float,
                    levels: int) -> int:
    """Quantize a pitch period to a small log-spaced frequency table,
    matching how real low-bitrate vocoders transmit pitch as a coarse
    index rather than a sample-accurate period.
    """
    if period <= 1:
        return period
    levels = int(max(2, levels))
    min_hz = max(1.0, float(min_hz))
    max_hz = max(min_hz + 1.0, float(max_hz))
    freq = float(np.clip(fs / period, min_hz, max_hz))
    log_lo, log_hi = np.log(min_hz), np.log(max_hz)
    idx = int(round((np.log(freq) - log_lo) / (log_hi - log_lo) * (levels - 1)))
    idx = int(np.clip(idx, 0, levels - 1))
    freq_q = float(np.exp(log_lo + idx / (levels - 1) * (log_hi - log_lo)))
    return max(1, int(round(fs / freq_q)))


def _quantize_gain_db(rms: float, bits: int, lo_db: float = -60.0,
                      hi_db: float = 0.0) -> float:
    """Quantize a linear RMS level to dB steps, matching the coarse
    per-frame gain index a real low-bitrate vocoder transmits.
    """
    bits = int(max(1, bits))
    rms = max(1e-9, float(rms))
    db = float(np.clip(20.0 * np.log10(rms), lo_db, hi_db))
    levels = 2 ** bits
    step = (hi_db - lo_db) / levels
    idx = int(round((db - lo_db) / step))
    idx = int(np.clip(idx, 0, levels - 1))
    db_q = lo_db + idx * step
    return float(10.0 ** (db_q / 20.0))


def _classify_voiced(frame: np.ndarray, energy: float, zcr: float,
                     max_energy: float, fs: int, min_lag: int, max_lag: int,
                     prev_period: int = 0):
    """Voiced/unvoiced via short-time energy + zero-crossing rate + pitch.

    Pitch is estimated on the *unwindowed* frame (Hann tapering flattens
    autocorrelation and hides the pitch peak). The estimator skips the
    short-lag formant lobe by searching from the first local minimum, then
    takes the earliest strong local maximum as the fundamental -- plain
    argmax would lock onto lag ~20 and drone everything at ~400 Hz.
    Falls back to the previous frame's period for continuity instead of a
    fixed monotone default.
    """
    if energy < 0.005 * max_energy:
        return False, 0
    if zcr > 0.35:
        return False, 0
    corr = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
    if corr[0] <= 1e-12 or len(corr) <= max_lag + 1:
        return False, 0
    seg = corr[min_lag:max_lag + 1] / max(corr[0], 1e-12)
    # Skip the formant lobe: start peak search after the first local min.
    start = 0
    for i in range(1, len(seg) - 1):
        if seg[i] < seg[i - 1] and seg[i] <= seg[i + 1]:
            start = i
            break
    best = -1
    for i in range(max(1, start), len(seg) - 1):
        if seg[i] > seg[i - 1] and seg[i] >= seg[i + 1] and seg[i] > 0.30:
            best = i
            break
    if best < 0:
        # No clean peak: continue the previous pitch (continuity) rather
        # than a monotone default; low-energy hiss stays unvoiced.
        if energy > 0.02 * max_energy and zcr < 0.20 and prev_period > 0:
            return True, int(prev_period)
        if energy > 0.02 * max_energy and zcr < 0.20:
            return True, int(fs / 120.0)
        return False, 0
    return True, min_lag + best


def _mixed_excitation(length: int, fs: int, voiced: bool, period: int,
                      voiced_noise_mix: float, rng: np.random.Generator,
                      frame_start: int = 0, jitter: float = 0.0,
                      shimmer: float = 0.0, noise_split_hz: float = 0.0) -> np.ndarray:
    """Mixed excitation with glottal jitter/shimmer and spectrally tilted
    noise, instead of a perfectly periodic pulse train (which is what
    makes a plain LPC vocoder sound robotic/buzzy rather than human).
    """
    if voiced and period > 1:
        exc = np.zeros(length)
        pos = (-frame_start) % period
        jrange = int(round(period * max(0.0, jitter)))
        while pos < length:
            jit = int(rng.integers(-jrange, jrange + 1)) if jrange > 0 else 0
            p = pos + jit
            amp = float(np.sqrt(max(1, period)))
            if shimmer > 0:
                amp *= max(0.05, 1.0 + shimmer * float(rng.standard_normal()))
            if 0 <= p < length:
                # Dispersed pulse (MELP-style pulse dispersion): spread the
                # impulse over a few samples so it doesn't buzz harshly.
                exc[p] += amp * 0.8
                if p + 1 < length:
                    exc[p + 1] += amp * 0.35
                if p - 1 >= 0:
                    exc[p - 1] += amp * 0.25
            pos += period
        noise = rng.standard_normal(length)
        if noise_split_hz > 0 and length > 8:
            try:
                sos = butter(1, min(noise_split_hz, fs * 0.45),
                            btype="highpass", fs=fs, output="sos")
                noise = noise + 0.6 * sosfilt(sos, noise)
            except Exception:
                pass
        exc += noise * voiced_noise_mix
        return exc
    return rng.standard_normal(length)


def _opus_via_opuslib(x: np.ndarray, fs: int, bitrate: int) -> np.ndarray:
    """Encode/decode through libopus at 6-12 kbps. Raises CodecUnavailable."""
    try:
        import opuslib  # type: ignore
    except Exception as exc:
        raise CodecUnavailable(f"opuslib not importable: {exc}")
    # opuslib supports 8/12/16/24/48 kHz; our NB is 8 kHz (supported).
    if fs not in (8000, 12000, 16000, 24000, 48000):
        raise CodecUnavailable(f"Opus with fs={fs} unsupported by opuslib")
    try:
        enc = opuslib.Encoder(fs, 1, opuslib.APPLICATION_VOIP)
        dec = opuslib.Decoder(fs, 1)
        try:
            enc.bitrate = bitrate
        except Exception:
            pass
        frame = 160 if fs == 8000 else 320  # 20 ms
        pcm = (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)
        out = np.zeros(0, dtype=np.float32)
        for i in range(0, len(pcm), frame):
            chunk = np.zeros(frame, dtype=np.int16)
            n = min(frame, len(pcm) - i)
            chunk[:n] = pcm[i:i + n]
            packet = enc.encode(chunk.tobytes(), frame)
            decoded = dec.decode(packet, frame)
            out = np.concatenate([out, np.frombuffer(decoded, dtype=np.int16)[:frame].astype(np.float32) / 32768.0])
        return out[:len(x)].astype(np.float64)
    except CodecUnavailable:
        raise
    except Exception as exc:
        raise CodecUnavailable(f"Opus round-trip failed: {exc}")


# ---------------------------------------------------------------------------
# Command-line entry point: `python radio_processor.py` runs a self-test that
# processes a synthetic speech-like signal through each codec and writes
# listenable WAVs; `python radio_processor.py in.wav out.wav [--codec lpc]
# [--seed 7] [--preset digital_tactical]` converts a real file. Only
# stdlib + numpy/scipy is used.
# ---------------------------------------------------------------------------

def _read_wav_mono(path: str):
    import wave
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        fs = w.getframerate()
        raw = w.readframes(n)
    if sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif sw == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype=np.int32).astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width: {sw * 8}-bit")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, fs


def _write_wav_mono(path: str, x: np.ndarray, fs: int) -> None:
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes((np.clip(x, -1.0, 1.0) * 32767).astype(np.int16).tobytes())


def _synthetic_speech(sr: int = 24000, seconds: float = 2.0) -> np.ndarray:
    """Speech-like test signal: harmonic vowels on a pitch glide + syllables."""
    t = np.arange(int(sr * seconds)) / sr
    f0 = 110.0 + 40.0 * (t / seconds)
    phase = 2 * np.pi * np.cumsum(f0) / sr
    x = sum(np.sin((k + 1) * phase) / (k + 1) for k in range(5))
    x = x * (0.6 + 0.4 * np.sin(2 * np.pi * 2.5 * t))
    x /= max(float(np.max(np.abs(x))), 1e-9)
    return (x * 0.7).astype(np.float64)


def _make_config(codec: str, seed: int, preset: str) -> RadioConfig:
    if preset == "digital_tactical":
        return RadioConfig.digital_tactical(codec=codec, seed=seed)
    return RadioConfig(codec=codec, seed=seed)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Tactical-radio DSP self-test / WAV converter")
    ap.add_argument("input", nargs="?",
                    help="input WAV (omit for synthetic self-test)")
    ap.add_argument("output", nargs="?",
                    help="output WAV (default: radio_selftest_<codec>.wav files)")
    ap.add_argument("--codec", default="lpc",
                    choices=["lpc", "melp", "cvsd", "opus", "none"],
                    help="codec stage to use (default: lpc)")
    ap.add_argument("--preset", default="default",
                    choices=["default", "digital_tactical"],
                    help="RadioConfig preset (default: default)")
    ap.add_argument("--seed", type=int, default=7,
                    help="RNG seed for reproducible run (default: 7)")
    ap.add_argument("--all-codecs", action="store_true",
                    help="self-test every codec (default with no input file)")
    args = ap.parse_args(argv)

    if args.input is None:
        codecs = ["none", "lpc", "cvsd", "opus", "melp"] if args.all_codecs \
            else [args.codec]
        sr = 24000
        x = _synthetic_speech(sr)
        print(f"self-test: {len(x) / sr:.1f}s synthetic speech @ {sr} Hz "
              f"(preset={args.preset})")
        for codec in codecs:
            proc = RadioProcessor(config=_make_config(codec, args.seed, args.preset))
            t0 = time.perf_counter()
            y = proc.process(x, sr)
            dt = (time.perf_counter() - t0) * 1000.0
            name = f"radio_selftest_{codec}.wav"
            _write_wav_mono(name, y, sr)
            print(f"  {codec:5s} -> {name}  ({dt:6.1f} ms, peak "
                  f"{float(np.max(np.abs(y))):.3f})")
        print("listen to radio_selftest_*.wav to A/B the codecs.")
        return 0

    x, fs = _read_wav_mono(args.input)
    y = RadioProcessor(
        config=_make_config(args.codec, args.seed, args.preset)).process(x, fs)
    out = args.output or "radio_out.wav"
    _write_wav_mono(out, y, fs)
    print(f"wrote {out} ({len(y) / fs:.2f}s @ {fs} Hz, codec={args.codec}, "
          f"preset={args.preset})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
