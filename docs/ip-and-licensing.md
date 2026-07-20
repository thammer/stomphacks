# IP and licensing

This repo is MIT licensed, and it exists so you can build your own pedal effects
and share them. That works only because of one hard line: Zoom's own code
never appears here, in any form. This doc is that line spelled out: what you can
share, what you cannot, and the checks to run before you release an effect. It
is project policy, deliberately conservative, and it is not legal advice.

## What this repo is, and what it will never contain

Everything authored here (the tools, the docs, the effect kernels and
manifests) is mine, MIT licensed (see [../README.md](../README.md) and the
`LICENSE` files). You can build the effects here, change them, and share what
you build, subject to the one caveat further down. The gain example is the
model: its kernel is original work, copyright Thomas Hammer, under MIT, and a
built copy is yours to pass on.

What this repo will never contain:

* Stock Zoom effect files: the `.ZD2`, `.ZIC` and `.ZIR` files that ship on
  the pedal.
* Any disassembly, extraction, annotated listing, or byte dump of a stock
  effect or of the pedal firmware.
* Any binary derived from Zoom's code.

Those are Zoom's, and they are copyrighted. Keeping them out is not caution for
its own sake; it is the difference between a project that can stay public and
one that cannot. There are no exceptions, not even a single stock file to help
someone out.

Facts are a different matter. Offsets, struct layouts, constants, the protocol
bytes on the wire: facts about a format are not copyrightable, and they are
documented here in my own words. That is what the rest of the docs are.

## The cleanroom route

The effects here are built cleanroom: the code that runs on the DSP is generated
from the documented ABI (see [zd2-abi.md](zd2-abi.md)) by this project's own
tools, in my own code. A built effect contains no Zoom-authored code: not
copied, not disassembled and reassembled, none of it. Nothing of Zoom's in the
file means nothing of Zoom's to infringe, so a cleanroom build is treated here
as your own work, yours to license and share. That is a careful reading, not a
legal ruling; the note at the top of this page applies. Before you release one,
validate it on hardware and run the checklist below.

Describing how a stock effect BEHAVES and then implementing that behaviour fresh
is fine, and is how most of what is documented here was worked out. Transcribing
stock disassembly into a kernel and calling it new is not fine: that is a
derivative work of Zoom's code, however much it is dressed up.

## The container: synthesized, three fields zeroed

The ZD2 container is synthesized from scratch by the build (see
tools/zd2_from_scratch.py), so a built effect contains nothing from any Zoom
file at all: not in the code blob, not in the container, not in the icon.

What made that possible: the container header has three fields whose meaning
is still undecoded (a sparse 73-byte flag block, a 3-byte category tag, and a
16-byte trailer). Builds ship all three ZEROED, and that configuration is
confirmed on hardware: an effect with all three fields zeroed loads, runs,
takes knob edits, bypasses and survives chain edits on an MS-70CDR+. Proven
for a simple SFX-class effect; other effect classes and pedal models are
extrapolation until tested. One cosmetic caveat is in the README's Needs
investigating: Guitar Lab may not list such an effect in its browser.

## Out of scope here: modifying stock effects

This release is about building effects from scratch. It does not cover modifying
a stock Zoom effect, and it ships no tool for doing so.

For the record, at the IP level: a modified stock effect binary still contains
Zoom's code, so it stays on your own pedal and goes no further. Whether the
MODIFICATION on its own (a change applied to a copy you already own, with no
Zoom code ever changing hands) could be shared in some non-infringing form is
a separate, unresolved question. A technical route might exist; the legal
picture genuinely needs real counsel. Do not read that as a yes or as a no. It
is simply not what this release is for, and it is not taught here.

## The rules, short

1. Never commit stock-derived material: stock effect files, pedal readbacks,
   extracted blobs, disassembly excerpts, or a diff whose bytes come from a
   stock file. This includes docs: quoting stock disassembly as evidence is
   still distributing it. State the fact in your own words instead. A single
   offset or constant is fine.

2. Never transcribe stock disassembly into a kernel. Work from a behavioural
   description and write your own.

3. GPL code is read, never copied. Some of the useful reference work out there
   is GPL (ELynx's zoom-fx-modding, for one). Learn from it, restate the ideas
   in your own words with credit, and write your own implementation. Copying GPL
   code, or GPL prose, would drag this repo under the GPL and end its MIT
   status.

4. Keep attribution intact. If you build on someone else's MIT-licensed code,
   their notices stay in the file.

## Before you release an effect

Run this over anything you are about to publish:

* The cleanroom build passed and every check in its build/REPORT.md is green
  (see [../tools/README.md](../tools/README.md)). The whole build chain is
  generated code plus your own kernel; no stock file enters it.
* No Zoom binaries, readbacks, or disassembly anywhere in what you are
  releasing.
* The id does not collide: a DIY-range id that no stock effect and nothing else
  on the pedal uses, with a unique filename, its own name and icon, and no Zoom
  trademark or implied endorsement in the name. See
  [effect-ids.md](effect-ids.md).
* The safety framing is intact: link [../SAFETY.md](../SAFETY.md), and say
  plainly that any DIY effect, yours included, can brick a pedal if the safety
  rules are ignored.

Release under whatever license you choose for your own code, keeping the
copyright yours.

## Needs investigating

* What the three zeroed header fields MEAN when stock effects set them. Zeroing
  them is hardware-proven to work (the section above), so decoding them is
  curiosity rather than a blocker, but the semantics are still unknown.
* Sharing modifications of stock effects, as mentioned above. A technical route may exist,
  but it is not currently the focus of this project to go in that direction.
  I'll rather focus on DYI effects.
