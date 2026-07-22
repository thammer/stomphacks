/* effects/wfmerge/wfmerge.c: bus merger, chain tail.
 *
 * Copyright (c) 2026 Thomas Hammer (Waveformer). MIT license.
 *
 * Original effect, concept-inspired by ELynx's GPL ZDL-generation RTFM, no
 * code or text reused (docs/chain-routing.md "Where these ideas came from").
 *
 * The tail of the family: WfMerge takes the two stereo buses, the MAIN bus
 * (ctx[1], what the chain is carrying) and the AUX bus (ctx[2], what is
 * parked), and recombines them into the single stereo signal the pedal will
 * play. It CONSUMES the AUX bus (zeroes it) as it goes.
 *
 *   Mode DUAL: MAIN -> LEFT jack, AUX -> RIGHT jack, each folded to mono.
 *                Two instruments (or the two halves of a stereo pair), each
 *                through its own effect group, each on its own output.
 *                Chain: [WfSplit DUAL] -> group -> [WfMerge DUAL].
 *   Mode SWAP  : the same, buses swapped: AUX -> LEFT, MAIN -> RIGHT.
 *                REQUIRED when a WfSwap sits between two groups: one swap
 *                makes the parity ODD (the chain ends up carrying what started
 *                on the RIGHT), so a DUAL tail would put both instruments on
 *                the WRONG jacks. Proven at the desk.
 *                Chain: [WfSplit DUAL] -> A -> [WfSwap] -> B -> [WfMerge SWAP].
 *   Mode MIX  : both buses summed into one stereo image, full width kept.
 *                The parallel rig: one instrument, two processed copies.
 *                Chain: [WfSplit COPY] -> A -> [WfSwap] -> B -> [WfMerge MIX].
 *
 * DUAL/SWAP fold each bus to mono with (A+B)*0.5, NOT A+B: an empty group must
 * come out at unity, not +6 dB. (Fair warning for the docs: folding a group
 * that ends in a widening stereo effect can phase-cancel, the honest cost of
 * a mono jack.)
 *
 * FOOTSWITCH A/B (FtSw): the pedal's ON/OFF button as an effect selector. No
 * new firmware feature is needed for this: the bypass state already arrives
 * in every kernel as coeff[0], and the firmware keeps calling a bypassed
 * effect's audio fn (hardware-proven). So in A/B mode WfMerge simply REINTERPRETS
 * its own bypass: instead of crossfading between "effected" and "bypassed",
 * it crossfades between the two BUSES.
 *   FtSw = BYP  the footswitch bypasses WfMerge normally (default)
 *   FtSw = A/B  footswitch ON  -> you hear the AUX bus  = effect group A
 *               footswitch OFF -> you hear the MAIN bus = effect group B
 * In the parallel rig ([WfSplit COPY] -> fx1 -> [WfSwap] -> fx2 -> [WfMerge]),
 * group A's output is parked in AUX and group B's is on the chain, so the
 * footswitch picks between fx1 and fx2. Both effects always RUN (you pay both
 * DSP loads, and the unheard one's delay/reverb tail keeps ringing, which
 * means switching gives you SPILLOVER, not a cut-off tail). Because the
 * firmware RAMPS coeff[0], the A/B switch is a click-free CROSSFADE.
 * WARNING: in A/B mode this effect can never be truly bypassed, "off" means
 * "the other effect". And A/B is meant for the parallel (COPY/MIX) rig: in
 * DUAL/SWAP the two buses hold DIFFERENT instruments, so it would mute one.
 *
 * The CONSUME is the trick the whole family depends on: WfMerge reads the AUX
 * bus into temps and then ZEROES it, on every block, INCLUDING when
 * bypassed. Overwrite-at-Split plus consume-here is what makes the family
 * GATE-INDEPENDENT: the AUX bus is pure inter-effect scratch, never relying
 * on the host mixing ctx[2] into the amp, so no LineSel / word0=2 router is
 * needed and nothing can leak.
 * WARNING: do NOT weaken the consume to anything but a zero, and do not make
 * it conditional on the knobs or on `fade`. The ONE exception is the Fix-C
 * path below, which returns BEFORE the consume: a guard-fail must not form
 * the ctx[2] pointer at all (that is the whole point of Fix C), and skipping
 * one block's consume is harmless, word0 = 0 means the host ignores ctx[2]
 * anyway, and the next good block consumes. Do NOT "fix" that by hoisting the
 * consume above the guard check.
 *
 * WARNING: Exactly ONE ctx[2] scheme per patch: never combine with any
 * other effect that writes the AUX bus, and never put a stock LINE SEL in
 * the patch.
 *
 * Mode and FtSw are SELECTORS (integer clicks). Like every plain param the host
 * hands the kernel knob/max, so a 3-way arrives as 0.0/0.5/1.0, NOT 0/1/2. The
 * kernel doubles it back to a 0..2 axis and blends the three routings with a
 * triangular partition of unity, one-poled like every other control: a Mode or
 * FtSw change CROSSFADES (~11 ms) instead of clicking.
 *
 * Fix B/C/D mandatory (dereferences ctx[2]): state guard + coeff sanity ->
 * silence on failure, never form the out pointer uninitialized; the AUX read is
 * Fix-D clamped before it enters the effect bus; every write clamped.
 *
 * SAFE-DSP: leaf, no stack, no calls, no statics, no divides. (The absolute
 * values in the mode blend are compare-and-negate, not a libm fabsf call.)
 */

#include <stdint.h>
#include "sh_params.h"

/* first-block-after-init marker (see the snap block in the audio fn).
 * WARNING: WF_DONE_WORD must stay BELOW SH_STATE_GUARD_WORD (= state_bytes/4)
 * or it clobbers the Fix-B guard. state_bytes 32 -> guard at word 8; this
 * kernel uses state words 0..3, so word 4 is free and inside the init-zeroed
 * region. If you add another state word, bump state_bytes in the manifest
 * FIRST. */
#define WF_DONE_WORD 4
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

    float fade = coef[SH_COEFF_BYPASS];    /* 0 = OFF/bypassed, 1 = ON */
    float mT   = coef[SH_PARAM_MAIN];      /* Main 0..100 -> 0..1 */
    float xT   = coef[SH_PARAM_AUX];       /* AUX  0..100 -> 0..1 */
    float dT   = coef[SH_PARAM_MODE];      /* Mode 0..2 -> 0 / 0.5 / 1.0 */
    float fT   = coef[SH_PARAM_FTSW];      /* FtSw 0 = BYP, 1 = A/B */
    if (!(fade >= 0.0f && fade <= 1.0f)) ok = 0;   /* garbage/NaN */
    if (!(mT >= 0.0f && mT <= 1.0f)) ok = 0;
    if (!(xT >= 0.0f && xT <= 1.0f)) ok = 0;
    if (!(dT >= 0.0f && dT <= 1.0f)) ok = 0;
    if (!(fT >= 0.0f && fT <= 1.0f)) ok = 0;

    int i;
    if (!ok) {
        /* Fix C: transitional/garbage instance -> silence, form no pointer */
        for (i = 0; i < SH_FRAMES; i++) {
            eff[i]                  = 0.0f;
            eff[i + SH_CH_B_OFFSET] = 0.0f;
        }
        return;
    }

    float *out = (float *)ctx[SH_CTX_OUT];
    float sm   = state[0];                 /* smoothed Main */
    float sx   = state[1];                 /* smoothed AUX  */
    float sd   = state[2];                 /* smoothed Mode */
    float sf   = state[3];                 /* smoothed FtSw */

    /* FIRST BLOCK AFTER (RE)INIT: snap, do not ramp. The firmware re-runs
     * _init on EVERY chain edit and every preview instantiation (that is why
     * Fix A exists), which ZEROES the smoothers while Fix A restores the
     * coeffs. A one-poled SELECTOR would therefore start from 0 every time: a
     * saved Mode = SWAP would spend ~57 ms (tau 11 ms) ramping up from DUAL,
     * i.e. in the flagship two-instrument chain BOTH INSTRUMENTS WOULD COME
     * OUT OF THE WRONG JACKS, cross-mixed, after every add/delete of any
     * effect. Snapping every smoother to its target on the first block kills
     * that (and the level fade-in with it). The stereochorus/totape9
     * precedent; the firmware's own
     * coeff[0] bypass ramp still makes the add itself click-free. */
    uint32_t *uw = (uint32_t *)instance[2];
    if (uw[WF_DONE_WORD] != WF_DONE) {
        sm = mT; sx = xT; sd = dT; sf = fT;
        uw[WF_DONE_WORD] = WF_DONE;
    }

    for (i = 0; i < SH_FRAMES; i++) {
        sm += 0.002f * (mT - sm);          /* tau ~= 11 ms at 44.1 kHz */
        sx += 0.002f * (xT - sx);
        sd += 0.002f * (dT - sd);
        sf += 0.002f * (fT - sf);

        /* CONSUME: read the parked stereo pair, then zero the scratch */
        float pA = out[i];
        float pB = out[i + SH_CH_B_OFFSET];
        if (!(pA > -8.0f && pA < 8.0f)) pA = 0.0f;   /* Fix D on the reads */
        if (!(pB > -8.0f && pB < 8.0f)) pB = 0.0f;
        out[i]                  = 0.0f;    /* mandatory, gate-independent */
        out[i + SH_CH_B_OFFSET] = 0.0f;

        float a = eff[i];                  /* the MAIN bus (the chain) */
        float b = eff[i + SH_CH_B_OFFSET];

        float mA = a * sm, mB = b * sm;    /* per-SOURCE levels */
        float xA = pA * sx, xB = pB * sx;

        /* --- routing (Mode): triangular partition of unity over 0..2 ---
         * 0 = DUAL (MAIN->L, AUX->R), 1 = SWAP (AUX->L, MAIN->R),
         * 2 = MIX (both buses summed, stereo width kept) */
        float x  = sd + sd;                /* 0.0/0.5/1.0 -> 0/1/2 */
        float d1 = x - 1.0f;
        if (d1 < 0.0f) d1 = -d1;           /* |x - 1|, no libm call */
        float w0 = 1.0f - x;          /* DUAL  */
        float w1 = 1.0f - d1;         /* SWAP  */
        float w2 = 1.0f - (2.0f - x); /* MIX */
        /* clamp to [0,1] at BOTH ends. The lower clamp makes the partition of
         * unity; the upper one is hardening: Fix B validates every coeff but
         * never the smoother state read back, and a stale state[] giving
         * x outside [0,2] would otherwise push a weight above 1 and break the
         * partition (a gain blow-up, caught only by Fix D's +-8). */
        if (w0 < 0.0f) w0 = 0.0f;  if (w0 > 1.0f) w0 = 1.0f;
        if (w1 < 0.0f) w1 = 0.0f;  if (w1 > 1.0f) w1 = 1.0f;
        if (w2 < 0.0f) w2 = 0.0f;  if (w2 > 1.0f) w2 = 1.0f;

        float monoM = (mA + mB) * 0.5f;    /* fold each bus to mono...   */
        float monoX = (xA + xB) * 0.5f;    /* ...for the two jack modes  */
        float rtA = w0 * monoM + w1 * monoX + w2 * (mA + xA);
        float rtB = w0 * monoX + w1 * monoM + w2 * (mB + xB);

        /* --- footswitch (FtSw) ---
         * BYP: fade is the normal bypass crossfade (routed -> untouched).
         * A/B: fade SELECTS a bus. ON (fade=1) -> AUX = group A;
         *      OFF (fade=0) -> MAIN = group B. Never actually bypassed. */
        float bypA = a + fade * (rtA - a);
        float bypB = b + fade * (rtB - b);
        float abA  = mA + fade * (xA - mA);
        float abB  = mB + fade * (xB - mB);

        float na = bypA + sf * (abA - bypA);   /* sf crossfades the two */
        float nb = bypB + sf * (abB - bypB);   /* footswitch behaviours */
        if (!(na > -8.0f && na < 8.0f)) na = 0.0f;          /* Fix D */
        if (!(nb > -8.0f && nb < 8.0f)) nb = 0.0f;
        eff[i]                  = na;
        eff[i + SH_CH_B_OFFSET] = nb;
    }
    state[0] = sm;
    state[1] = sx;
    state[2] = sd;
    state[3] = sf;
}
