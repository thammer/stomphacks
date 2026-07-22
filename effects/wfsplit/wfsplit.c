/* effects/wfsplit/wfsplit.c: bus splitter, chain head.
 *
 * Copyright (c) 2026 Thomas Hammer (Waveformer). MIT license.
 *
 * Original effect. Concepts (parking a channel in a shared bus, restoring it
 * mid-chain) were learned from ELynx's GPL ZDL-generation Div0/RTFM, no code
 * or text reused; see docs/chain-routing.md "Where these ideas came from".
 *
 * THE MODEL (docs/chain-routing.md). Three buffers, five channels:
 *   MAIN bus = ctx[1], stereo. The serial chain. THE ONLY BUS STOCK EFFECTS
 *              PROCESS. (L = words 0..15, R = words 16..31.)
 *   AUX  bus = ctx[2], stereo. A shared, host-zeroed carrier. Effects can
 *              read/write it but never process it; it comes up empty each
 *              block. Anything parked here is TRANSPORTED, not treated.
 *   SIDE     = ctx[0][0..15], MONO. The pedal's dry input: the content-
 *              selected input at 1x, not a saturating L+R sum (feed law
 *              hw-measured 2026-07-16). 20 stock effects take their
 *              DETECTOR from here (N_GATE, ZNR, AUTOWAH, CRY, the
 *              Dyna/Slow/Trigger family...).
 *              WARNING: ctx[0][16..31] is NOT a right channel. It is live
 *              firmware state and writing it FREEZES THE PEDAL
 *              (hardware-confirmed). Never.
 *
 * WfSplit sits at the chain head and populates both stereo buses, so a group
 * of effects can work on the MAIN bus while the other signal waits in the AUX
 * bus for a WfSwap to bring it into the chain:
 *
 *   Mode DUAL: L to MAIN (on both channels), R to AUX (on both channels).
 *               Two mono paths: two instruments, or the two halves of a
 *               stereo pair, each processed independently.
 *   Mode COPY: the stereo input duplicated to BOTH buses. One instrument,
 *               two parallel processed copies (and the A/B footswitch rig,
 *               see WfMerge FtSw).
 *
 * The BROADCAST in DUAL mode is the trick the whole effect depends on: to
 * make an ordinary stock effect process ONE instrument, that instrument must
 * occupy BOTH channels of the effect bus while the other waits in the AUX
 * bus. Corollary, and it is a feature: a mono-summing effect inside a group
 * is HARMLESS (both channels already carry the same signal).
 *
 * SIDECHAIN (SidCh/SidLv): the fix for the one hole in "separate chains".
 * The dry bus normally carries only one instrument's content at a time, so a
 * gate/wah/envelope effect in the guitar's group would duck on the synth's
 * transients if it read the dry bus unmodified. SidCh overwrites the dry bus
 * with ONE input channel, so those effects key off the instrument their
 * group is actually carrying. Writing ctx[0][0..15] is hardware-proven
 * (the write landed and propagated to a downstream reader
 * within the block, no burst/latch), it is inaudible in the audio path (only
 * detectors read it), and it is transient (the host refills it from the ADC
 * every block, so nothing to clean up).
 *   SidCh = L+R  leave the pedal's own feed alone (default; a true no-op)
 *   SidCh = L    key off the LEFT input     } and deliberately NOT limited to
 *   SidCh = R    key off the RIGHT input    } the channel this group carries:
 * keying a wah on L off R's envelope is cross-instrument SIDE-CHAIN KEYING,
 * ducking, envelope-follows-the-drums, which the stock pedal cannot do at
 * all. SidLv is the key-input gain (0..2x, unity at 50). The kernel writes at
 * UNITY by default deliberately: the detector then sees the instrument
 * exactly as it would if that instrument were the only thing plugged in.
 * This value is hard-clipped at full scale, a conservative ceiling in the
 * same spirit as the level where the host's own feed starts to degrade
 * (SH_SIDE_FS, *inferred* full scale, see the header).
 *
 * GATE-INDEPENDENT: the AUX bus is used purely as inter-effect scratch
 * (OVERWRITE here, consume at WfMerge). Nothing relies on the host mixing
 * ctx[2] into the amp, so no LineSel / word0=2 router is needed, and with no
 * router in the patch the host ignores the AUX bus entirely (ladder E2a).
 * WARNING: OVERWRITE, never += : the overwrite-then-consume discipline is
 * what sidesteps the still-open "does the host zero ctx[2] when the gate is
 * CLOSED" question. Do not change it without testing that first.
 *
 * WARNING: Exactly ONE ctx[2] scheme per patch: never combine with any
 * other effect that writes the AUX bus, and never put a stock LINE SEL in
 * the patch (the only stock ctx[2] toucher, census-confirmed).
 *
 * Fix B/C/D mandatory (dereferences ctx[0] and ctx[2]): state guard + coeff
 * sanity -> silence on failure, never form an extended-ctx pointer
 * uninitialized; every written sample clamped |x| < 8 else 0.
 *
 * SAFE-DSP: leaf, no stack, no calls, no statics, no divides.
 */

#include <stdint.h>
#include "sh_params.h"

/* full scale for the sidechain hard-clip. *(inferred*: audio here is float
 * normalized to +-1.0; the Fix-D bound of 8 is a sanity limit, not FS. The
 * clip only ever shapes what DETECTORS see, never the audio path, so an
 * imprecise ceiling is harmless, but it is on the R1 hardware list.) */
#define SH_SIDE_FS 1.0f

/* first-block-after-init marker (see the snap block in the audio fn).
 * WARNING: WF_DONE_WORD must stay BELOW SH_STATE_GUARD_WORD (= state_bytes/4)
 * or it clobbers the Fix-B guard. state_bytes 32 -> guard at word 8; this
 * kernel uses state words 0..4, so word 5 is free and inside the init-zeroed
 * region. If you add another state word, bump state_bytes in the manifest
 * FIRST. */
#define WF_DONE_WORD 5
#define WF_DONE      0x57464432u   /* 'WFD2' */

void SH_AUDIO_FN(void **instance, void **ctx)
{
    const float *coef  = (const float *)instance[1];
    float *state       = (float *)instance[2];
    const uint32_t *ust = (const uint32_t *)instance[2];
    float *eff         = (float *)ctx[SH_CTX_EFF];

    unsigned ok = 1;
    if (ust[SH_STATE_GUARD_WORD] != SH_STATE_GUARD)
        ok = 0;                            /* init hasn't run yet */

    float fade = coef[SH_COEFF_BYPASS];    /* 0 = bypassed, 1 = active */
    float mT   = coef[SH_PARAM_MAIN];      /* Main  0..100 -> 0..1 */
    float xT   = coef[SH_PARAM_AUX];       /* AUX   0..100 -> 0..1 */
    float dT   = coef[SH_PARAM_MODE];      /* Mode  0..1   -> 0 DUAL / 1 COPY */
    float cT   = coef[SH_PARAM_SIDCH];     /* SidCh 0..2   -> 0 / 0.5 / 1.0 */
    float lT   = coef[SH_PARAM_SIDLV];     /* SidLv 0..100 -> 0..1 (x2 = gain) */
    if (!(fade >= 0.0f && fade <= 1.0f)) ok = 0;   /* garbage/NaN */
    if (!(mT >= 0.0f && mT <= 1.0f)) ok = 0;
    if (!(xT >= 0.0f && xT <= 1.0f)) ok = 0;
    if (!(dT >= 0.0f && dT <= 1.0f)) ok = 0;
    if (!(cT >= 0.0f && cT <= 1.0f)) ok = 0;
    if (!(lT >= 0.0f && lT <= 1.0f)) ok = 0;

    int i;
    if (!ok) {
        /* Fix C: transitional/garbage instance -> write silence, do NOT
         * forward the bus and do NOT form any extended-ctx pointer */
        for (i = 0; i < SH_FRAMES; i++) {
            eff[i]                  = 0.0f;
            eff[i + SH_CH_B_OFFSET] = 0.0f;
        }
        return;
    }

    float *out = (float *)ctx[SH_CTX_OUT];
    float sm   = state[0];                 /* smoothed Main  */
    float sx   = state[1];                 /* smoothed AUX   */
    float sd   = state[2];                 /* smoothed Mode  */
    float sc   = state[3];                 /* smoothed SidCh */
    float sl   = state[4];                 /* smoothed SidLv */

    /* FIRST BLOCK AFTER (RE)INIT: snap, do not ramp. The firmware re-runs
     * _init on EVERY chain edit and every preview instantiation (that is why
     * Fix A exists), which ZEROES the smoothers while Fix A restores the
     * coeffs. A one-poled SELECTOR would therefore start from 0 every time: a
     * saved Mode = COPY would spend ~57 ms (tau 11 ms) ramping up from DUAL,
     * and SidLv would ramp up from ZERO, so a gate keyed off the sidechain
     * would see silence and chatter on every add/delete. Snapping every
     * smoother to its target on the first block kills that (and the level
     * fade-in with it).
     * The stereochorus/totape9 precedent; the firmware's own coeff[0] bypass
     * ramp still makes the add itself click-free. */
    uint32_t *uw = (uint32_t *)instance[2];
    if (uw[WF_DONE_WORD] != WF_DONE) {
        sm = mT; sx = xT; sd = dT; sc = cT; sl = lT;
        uw[WF_DONE_WORD] = WF_DONE;
    }

    /* The sidechain buffer is only touched when SidCh is off L+R (or on its
     * way there) AND the kernel is not bypassed: otherwise the ctx[0] pointer
     * is never formed at all, so a patch that does not ask for keying cannot
     * be affected in any way. */
    float *side = 0;
    if (fade > 0.0f && (cT > 0.001f || sc > 0.001f))
        side = (float *)ctx[SH_CTX_GTRIN];

    for (i = 0; i < SH_FRAMES; i++) {
        sm += 0.002f * (mT - sm);          /* tau ~= 11 ms at 44.1 kHz */
        sx += 0.002f * (xT - sx);
        sd += 0.002f * (dT - sd);
        sc += 0.002f * (cT - sc);
        sl += 0.002f * (lT - sl);

        float a = eff[i];                  /* input L */
        float b = eff[i + SH_CH_B_OFFSET]; /* input R */

        /* Mode as one lerp (sd: 0 = DUAL, 1 = COPY):
         *   DUAL -> MAIN = (a,a), AUX = (b,b)   [broadcast]
         *   COPY -> MAIN = (a,b), AUX = (a,b)   [stereo duplicate]
         * Channel A of MAIN and channel B of AUX are the same in both modes;
         * only the other two channels move, so a Mode change CROSSFADES. */
        float mainA = sm * a;
        float mainB = sm * (a + sd * (b - a));
        float auxA  = sx * (b + sd * (a - b));
        float auxB  = sx * b;

        /* AUX bus: OVERWRITE (faded, so bypass writes a deterministic 0 and
         * the open "does the host zero ctx[2]" question stays irrelevant) */
        float pA = fade * auxA;
        float pB = fade * auxB;
        if (!(pA > -8.0f && pA < 8.0f)) pA = 0.0f;          /* Fix D */
        if (!(pB > -8.0f && pB < 8.0f)) pB = 0.0f;
        out[i]                  = pA;
        out[i + SH_CH_B_OFFSET] = pB;

        /* MAIN bus: crossfade to untouched stereo on bypass */
        float na = a + fade * (mainA - a);
        float nb = b + fade * (mainB - b);
        if (!(na > -8.0f && na < 8.0f)) na = 0.0f;          /* Fix D */
        if (!(nb > -8.0f && nb < 8.0f)) nb = 0.0f;
        eff[i]                  = na;
        eff[i + SH_CH_B_OFFSET] = nb;

        /* SIDECHAIN (mono, ctx[0] lower half only) */
        if (side) {
            /* Triangular partition of unity over the 0..2 selector axis:
             * 0 = L+R (leave the pedal's own feed alone), 1 = L, 2 = R.
             * WARNING: the three weights must be applied EXACTLY ONCE, as one
             * blend of the three targets. An earlier version scaled `key` by
             * wL/wR AND then crossfaded by (1 - wOff), applying the weight
             * twice, so the effective weights summed to 1 - x + x^2 (a 0.75
             * dip at x = 0.5) and the key content lost 6 dB mid-switch.
             * Detector-only, but it made a gate stutter through a SidCh
             * change. */
            float x  = sc + sc;
            float d1 = x - 1.0f;
            if (d1 < 0.0f) d1 = -d1;
            float d2 = 2.0f - x;
            float wOff = 1.0f - x;   /* L+R: keep the host's value */
            float wL   = 1.0f - d1;  /* L */
            float wR   = 1.0f - d2;  /* R */
            /* clamp to [0,1] at both ends: the lower clamp makes the
             * partition of unity, the upper one hardens against a stale
             * state[] (Fix B validates the coeffs but never the smoother
             * state) */
            if (wOff < 0.0f) wOff = 0.0f;  if (wOff > 1.0f) wOff = 1.0f;
            if (wL   < 0.0f) wL   = 0.0f;  if (wL   > 1.0f) wL   = 1.0f;
            if (wR   < 0.0f) wR   = 0.0f;  if (wR   > 1.0f) wR   = 1.0f;

            float g  = sl + sl;                 /* SidLv: 0..2x, unity at 50 */
            float kL = a * g;
            float kR = b * g;
            /* hard-clip each key source at full scale, a conservative
             * ceiling (the host's own feed is a content-selected input at
             * 1x that degrades above ~-4 dBFS/ch, not a saturating sum;
             * feed law hw-measured 2026-07-16). Detectors only,
             * inaudible in the audio path. */
            if (kL >  SH_SIDE_FS) kL =  SH_SIDE_FS;
            if (kL < -SH_SIDE_FS) kL = -SH_SIDE_FS;
            if (kR >  SH_SIDE_FS) kR =  SH_SIDE_FS;
            if (kR < -SH_SIDE_FS) kR = -SH_SIDE_FS;

            float dry = side[i];
            /* ONE blend of the three targets (weights sum to 1), then ONE
             * crossfade against bypass. At wOff = 1 (SidCh = L+R) the target IS
             * `dry`, and at fade = 0 nothing moves, so both no-op claims stay
             * bit-exact. */
            float tgt = wOff * dry + wL * kL + wR * kR;
            float nd  = dry + fade * (tgt - dry);
            if (!(nd > -8.0f && nd < 8.0f)) nd = 0.0f;      /* Fix D */
            side[i] = nd;
        }
    }
    state[0] = sm;
    state[1] = sx;
    state[2] = sd;
    state[3] = sc;
    state[4] = sl;
}
