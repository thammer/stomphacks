/* effects/wfswap/wfswap.c: MAIN <-> AUX bus exchange.
 *
 * Copyright (c) 2026 Thomas Hammer (Waveformer). MIT license.
 *
 * Original effect. Concepts learned from ELynx's GPL ZDL-generation Div0/RTFM,
 * no code or text reused (docs/chain-routing.md "Where these ideas came from").
 *
 * THE MISSING PRIMITIVE. Stock effects only ever process the MAIN bus (ctx[1]);
 * the AUX bus (ctx[2]) is a carrier they cannot touch. So a signal parked in
 * AUX is transported, never treated, and WfSplit + WfMerge alone can only give
 * ONE effect group. WfSwap is the mid-chain EXCHANGE that brings the parked
 * signal into the chain (and parks what the chain was carrying), which is what
 * lets a SECOND group of effects process the other signal:
 *
 *   [WfSplit] -> group A -> [WfSwap] -> group B -> [WfMerge]
 *
 * It is a pure, symmetric, full-stereo swap: MAIN and AUX trade places, both
 * channels, unity gain. Because it is symmetric it is its own inverse, and
 * because the bus is stereo the swap preserves a stereo effect's width intact
 * (docs/chain-routing.md).
 *
 * WARNING: ONE SWAP MAKES THE PARITY ODD. After it, the chain carries what was
 * parked and the park holds what the chain carried, so the tail must be a
 * WfMerge in Mode SWAP, or the two signals come out of the WRONG output jacks.
 * That trap is real and is proven at the desk.
 *
 * BYPASS = EXACT PASS-THROUGH, BOTH BUSES PRESERVED. Every write is a
 * read-modify-write crossfade against the value just read, so at fade = 0 each
 * bus is rewritten with its own content, a true no-op. Bypassing WfSwap
 * therefore COLLAPSES a two-group chain cleanly back to a one-group chain
 * instead of stranding the parked signal. Deliberate; do not "simplify" the
 * writes to fade-scaled stores (that would zero the AUX bus on bypass).
 *
 * SIDECHAIN (SidCh): the completion of WfSplit's keying. There is only ONE
 * dry/sidechain buffer (mono, ctx[0][0..15]) and it is global, so a single
 * write at the Split would leave group B's detectors keyed to group A's
 * instrument. SidCh = SWAP re-keys the sidechain from the signal the swap just
 * brought into the chain, so each group's gates/wahs/envelopes follow the
 * instrument that group is actually processing:
 *   SidCh = KEEP  leave the sidechain as it is (default; an exact no-op)
 *   SidCh = SWAP  re-key from the (new) MAIN bus, mono-summed
 * Writing ctx[0][0..15] is hardware-proven safe and propagates downstream
 * within the block. WARNING: NEVER ctx[0][16..31], firmware
 * state; writing it freezes the pedal (hardware-confirmed).
 *
 * GATE-INDEPENDENT (no LineSel needed), and exactly ONE ctx[2] scheme per
 * patch (WARNING): never combine with any other effect that writes the AUX
 * bus, or a stock LINE SEL.
 *
 * Fix B/C/D mandatory (dereferences ctx[0] and ctx[2]): state guard + coeff
 * sanity -> silence on failure, never form an extended-ctx pointer
 * uninitialized; the AUX read is Fix-D clamped before it enters the MAIN bus
 * (scrubs any NaN/garbage sitting in the shared bus); every write clamped.
 *
 * SAFE-DSP: leaf, no stack, no calls, no statics, no divides.
 */

#include <stdint.h>
#include "sh_params.h"

#define SH_SIDE_FS 1.0f        /* see wfsplit.c, detector-only hard clip */

/* first-block-after-init marker (see the snap block in the audio fn).
 * WARNING: WF_DONE_WORD must stay BELOW SH_STATE_GUARD_WORD (= state_bytes/4)
 * or it clobbers the Fix-B guard. state_bytes 16 -> guard at word 4; this
 * kernel uses state word 0, so word 1 is free and inside the init-zeroed
 * region. */
#define WF_DONE_WORD 1
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
    float cT   = coef[SH_PARAM_SIDCH];     /* SidCh 0 = KEEP, 1 = SWAP */
    if (!(fade >= 0.0f && fade <= 1.0f)) ok = 0;   /* garbage/NaN */
    if (!(cT   >= 0.0f && cT   <= 1.0f)) ok = 0;

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
    float sc   = state[0];                 /* smoothed SidCh */

    /* FIRST BLOCK AFTER (RE)INIT: snap, do not ramp. _init re-runs on EVERY
     * chain edit and preview instantiation (that is why Fix A exists) and
     * zeroes the smoothers, so a one-poled SELECTOR would restart from 0
     * every time: a saved SidCh = SWAP would spend ~57 ms back on KEEP,
     * leaving group B's detectors keyed to the WRONG instrument after every
     * add/delete. Snap it. (stereochorus/totape9 precedent;
     * docs/chain-routing.md.) */
    uint32_t *uw = (uint32_t *)instance[2];
    if (uw[WF_DONE_WORD] != WF_DONE) {
        sc = cT;
        uw[WF_DONE_WORD] = WF_DONE;
    }

    float *side = 0;                       /* only if actually asked for */
    if (fade > 0.0f && (cT > 0.001f || sc > 0.001f))
        side = (float *)ctx[SH_CTX_GTRIN];

    for (i = 0; i < SH_FRAMES; i++) {
        sc += 0.002f * (cT - sc);          /* tau ~= 11 ms at 44.1 kHz */

        float a = eff[i];                  /* the chain (group A's output) */
        float b = eff[i + SH_CH_B_OFFSET];
        float pA = out[i];                 /* the parked signal */
        float pB = out[i + SH_CH_B_OFFSET];
        if (!(pA > -8.0f && pA < 8.0f)) pA = 0.0f;   /* Fix D on the reads */
        if (!(pB > -8.0f && pB < 8.0f)) pB = 0.0f;

        /* THE EXCHANGE: read-modify-write crossfades, so fade = 0 rewrites
         * each bus with what it already held (an exact no-op both ways) */
        float na = a + fade * (pA - a);    /* MAIN <- parked */
        float nb = b + fade * (pB - b);
        float qa = pA + fade * (a - pA);   /* AUX  <- chain  */
        float qb = pB + fade * (b - pB);
        if (!(na > -8.0f && na < 8.0f)) na = 0.0f;          /* Fix D */
        if (!(nb > -8.0f && nb < 8.0f)) nb = 0.0f;
        if (!(qa > -8.0f && qa < 8.0f)) qa = 0.0f;
        if (!(qb > -8.0f && qb < 8.0f)) qb = 0.0f;
        eff[i]                  = na;
        eff[i + SH_CH_B_OFFSET] = nb;
        out[i]                  = qa;
        out[i + SH_CH_B_OFFSET] = qb;

        /* SIDECHAIN: re-key from the NEW main bus (mono sum), so group B's
         * detectors follow the instrument group B now carries */
        if (side) {
            float key = (na + nb) * 0.5f;
            if (key >  SH_SIDE_FS) key =  SH_SIDE_FS;
            if (key < -SH_SIDE_FS) key = -SH_SIDE_FS;

            float amt = fade * sc;         /* KEEP (sc = 0) -> exact no-op */
            float dry = side[i];
            float nd  = dry + amt * (key - dry);
            if (!(nd > -8.0f && nd < 8.0f)) nd = 0.0f;      /* Fix D */
            side[i] = nd;
        }
    }
    state[0] = sc;
}
