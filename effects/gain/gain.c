/* effects/gain/gain.c - the simplest complete effect, and your starting point.
 *
 * A level trim with one knob. Short enough to read top to bottom, and it
 * follows every mandatory rule, so copy this folder to start your own effect:
 * rename the identity fields in manifest.json (name, filename, id - pick your
 * id per docs/effect-ids.md), then replace the math in the loop.
 *
 * Keep the guard prologue and the output clamp. They are not style. Each one
 * exists because its absence crashed a real pedal. See SAFETY.md, and
 * docs/building-effects.md for the full kernel contract.
 *
 * This kernel is hardware-proven on an MS-70CDR+.
 *
 * The ABI it rides on (docs/zd2-abi.md):
 *   instance[1] -> the live coefficient table (float32)
 *   instance[2] -> per-instance state, zeroed at init (state_bytes)
 *   ctx[1]      -> the effect bus, processed IN PLACE:
 *                  16 frames of channel A at [0..15], channel B at [16..31]
 *   coeff[SH_COEFF_BYPASS] -> the firmware bypass crossfade, ramps 0..1
 *
 * Copyright (c) 2026 Thomas Hammer (Waveformer). MIT license.
 */

#include <stdint.h>
#include "sh_params.h"   /* generated into build/ from your manifest */

void SH_AUDIO_FN(void **instance, void **ctx)
{
    const float *coef = (const float *)instance[1];
    float *eff        = (float *)ctx[1];
    float *state      = (float *)instance[2];   /* [0] = the smoothed level */
    int i;

    /* ---- mandatory guard prologue --------------------------------------
     * The firmware can run this function against an instance whose init has
     * not run yet. Routinely, in fact: the pedal live-previews every effect
     * the user scrolls past in the browser. Computing with garbage
     * coefficients writes Inf or NaN onto the bus, and the pedal's audio
     * then latches silent until it is power-cycled.
     *
     * So check first, and if anything is not yet sane, write SILENCE rather
     * than forwarding whatever the transitional bus happens to hold.
     */
    unsigned ok = 1;
#ifdef SH_STATE_GUARD
    if (((const uint32_t *)instance[2])[SH_STATE_GUARD_WORD]
            != SH_STATE_GUARD)
        ok = 0;                 /* init has not run yet */
#endif
    float fade = coef[SH_COEFF_BYPASS];   /* 0 = bypassed, 1 = active */
    float tL   = coef[SH_PARAM_LEVEL];    /* the curved amplitude, 0..1.5 */
    if (!(fade >= 0.0f && fade <= 1.0f))
        ok = 0;                 /* garbage or NaN fade */
    if (!(tL >= 0.0f && tL <= 1.6f))
        ok = 0;                 /* garbage or NaN level */

    if (!ok) {
        for (i = 0; i < SH_FRAMES; i++) {
            eff[i]                  = 0.0f;
            eff[i + SH_CH_B_OFFSET] = 0.0f;
        }
        return;
    }

    /* ---- the effect -----------------------------------------------------
     * The Level knob arrives already mapped through the stock VOL curve
     * (manifest: "curve": "stock_vol"), so the coefficient IS the target
     * amplitude: unity at knob 80, 1.5x at 100, exactly like a stock Zoom VOL
     * knob.
     *
     * A knob write is a step, so smooth it per sample or it zipper-clicks on a
     * fast twist. Mixing against fade makes bypass an exact, click-free
     * pass-through.
     */
    float sL = state[0];          /* the smoothed level, kept across blocks */
    for (i = 0; i < SH_FRAMES; i++) {
        sL += 0.002f * (tL - sL); /* one-pole, tau ~= 11 ms at 44.1 kHz */
        float mix = 1.0f + fade * (sL - 1.0f);

        float a = eff[i] * mix;
        float b = eff[i + SH_CH_B_OFFSET] * mix;

        /* mandatory output clamp. NaN fails any comparison, so this scrubs
         * NaN, Inf and torn-read garbage from everything the kernel writes. */
        if (!(a > -8.0f && a < 8.0f)) a = 0.0f;
        if (!(b > -8.0f && b < 8.0f)) b = 0.0f;

        eff[i]                  = a;
        eff[i + SH_CH_B_OFFSET] = b;
    }
    state[0] = sL;
}
