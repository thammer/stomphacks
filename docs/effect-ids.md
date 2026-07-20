# Effect ids

Every ZD2 effect carries an id: a 32-bit number, written as 8 hex digits,
stamped into the container file. This doc is how to choose one for your own
effect so that it collides with nothing already on the pedal.

Ids are written as hex throughout, the way the tools print them (`07000f0a`).

## Where the id lives, and why it has to be unique

The id is a 32-bit little-endian integer at offset 96 (`0x60`) in the ZD2
container, just ahead of the effect name. Any effect file has one, and the
install tooling reads it straight from there.

Patches reference an effect by its id, not by its name. So if your effect
carries an id that some effect already on the pedal also carries (and a stock
Zoom effect is the collision that matters), every patch that meant that effect
now points at the wrong one. That is the single outcome to avoid.

What a patch actually resolves to when two installed effects share an id is
still untested. What is now confirmed, by accident, is the symptom one level
up: the pedal's effect browser shows icons only, no filenames or names, so two
DIY files sharing an id become visually indistinguishable there. Nothing in
this project's install tooling catches a DIY-vs-DIY id collision either; it
only ever checks a new upload against the stock catalog. Read the pedal back
and check before you install, not after.

## How ids are structured

The top byte is the effect group. It also decides which category the effect
shows up under in the pedal's browser. The table below shows the effect ids for
the MS-70CDR+. The category ids could differ for other pedals (like
the MS-60B+)), see for instance zoom-explorer, ZoomDevice.ts,
getCategoryNameFromID().

| Top byte | Browser category |
|---|---|
| 01 | DYNAMICS |
| 02 | FILTER |
| 03 | DRIVE |
| 04 | AMP |
| 05 | CABINET |
| 06 | MODULATION |
| 07 | SFX |
| 08 | DELAY |
| 09 | REVERB |
| 0b | PEDAL |

The group enum comes from `zoomzt2.py` in mungewell/zoom-zt2; other pedal
models have further groups. The rest of the id is the effect's identity within
its group, and stock effects sit at low values there. You can see the full
stock catalogue for your own pedal by reading it back, or in the zoom-zt2
catalog files.

## Picking an id

Note: this is an area the community still has to work out conventions for.
The text below is just my initial simple solution for my own experiments.
See the section below for more thoughts from me.

The rule is short: pick an id that collides with nothing, and put it on the DIY
page so the safety tooling recognises it as yours.

1. Use the DIY page `07000f00` to `07000fff`, and within it pick from
   `07000f01` to `07000fef`. The page sits in the SFX group and this project's
   tools recognise an id on it as a DIY effect, but Zoom occupies both ends:
   `07000f00` is the stock LINE SEL effect, and `07000ff0` is the pedal's
   built-in BPM module. The BPM module is easy to miss. On the MS-70CDR+ and
   MS-50G+ it has no effect file anywhere; it exists only as an entry in the
   pedal's own effect list, but the id is taken all the same. The installer
   knows about both and refuses them. Whether Zoom parks anything else above
   `07000fef` is unknown, so stay below it. The gain example ships as
   `07000f81`. Copy that folder and change the id to a value in that range you
   are not already using.

2. The safe installer only ever touches DIY ids. `tools-pedal/pedal_diy.py`
   installs or uninstalls an effect only if its id is in a DIY range AND its id,
   name and filename match no stock effect. It is fail-closed: an id outside the
   DIY range is refused outright, so the wrapper cannot act on a stock effect by
   accident. Put your effect on the DIY page and you keep that safety net; take
   it off the page and you lose it and have to drive the raw install by hand.
   See [../tools-pedal/README.md](../tools-pedal/README.md) and
   [../SAFETY.md](../SAFETY.md).

3. Do not collide with your own effects either. If you already have DIY effects
   installed, keep their ids distinct. Read the pedal back to see what is on it
   before you add another.

4. One id per effect, for good. Treat a released id like an API: a new version
   of an effect keeps its id, a genuinely new effect gets a new one.

5. The filename has to be unique too: 8 characters or fewer, from A-Z 0-9 and
   underscore. The id space and the pedal's filesystem are separate namespaces,
   and the tooling pairs an effect with its icon by filename. The gain effect is
   `DIYGAIN`. Name, filename and icon are part of an effect's identity when you
   release it (see [ip-and-licensing.md](ip-and-licensing.md)).

## The bigger question: who gets which ids

There is no agreed, community-wide scheme for handing id ranges to different
authors. Today people just stake out a range and try not to tread on each
other. ELynx's zoom-fx-modding project states it uses ids with a `f1` prefix; I
use the `07000f00` page here. That is the whole of the "system" so far.

One idea that has been floated is a per-author registry, where each author
claims a block of ids above the stock range and the claims are coordinated
somewhere neutral - like the issue tracker for stomphacks or zoom-zt2. Nothing like that is settled, and this release does not try to settle it.
Until it is, avoid collisions by hand: stay on the DIY page, and check a pedal
readback before you install.

## Needs investigating

* A shared per-author id-allocation scheme. None exists. The registry above is
  one proposal, not a decision, and even the venue and the block sizes are
  unresolved. For now, hand-check against a readback.
* Whether more built-in entries like the BPM module exist or will appear. The
  effect list of every current MS Plus model was checked and the BPM module is
  the only entry without a file today, but a firmware update could add one.
* What the pedal and the patch format actually do when two installed effects
  share an id. Untested, deliberately.
* Whether ids have to be unique across all pedal models, or only per model.
  Unknown. Assume global uniqueness to be safe.
