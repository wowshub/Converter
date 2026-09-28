# What changed, and what this tool does about it

Reference for each format the converter touches: what modern builds do
differently from 3.3.5a, and how the downgrade handles it. Field offsets are
from the start of the struct unless stated otherwise.

---

## M2 — models

### Container

3.3.5a reads a flat file beginning `MD20` followed by `uint32 version = 264`.
Legion (272) wrapped that same body in an `MD21` chunk and moved every external
reference into sibling chunks:

| Chunk | Carries | Handling |
|---|---|---|
| `MD21` | the MD20 body; offsets are relative to the chunk payload | unwrapped |
| `TXID` | one FileDataID per `M2Texture` | resolved to paths, written inline |
| `SFID` | skin profile FileDataIDs, then Legion LOD skins | first four used, rest reported |
| `AFID` | `{animId, subAnimId, fileId}` per external animation | renamed `<model><anim:04d>-<sub:02d>.anim` |
| `SKID` | the `.skel` holding bones and sequences | loaded and merged |
| `BFID` | `.bone` overrides | dropped |
| `PFID` | physics rig | dropped — 3.3.5a has no model physics |
| `PABC` `PADC` `PSBC` `PEDC` | parent-animation data | dropped |
| `LDV1` | LOD data | dropped |
| `TXAC` `EXPT` `EXP2` `PGD1` | extended particle data | dropped |
| `WFV1`–`WFV3` `DETL` `NERF` `EDGF` `DBOC` `AFRA` | shader/light extras | dropped |
| `RPID` `GPID` | recursive and geometry particle models | dropped |

Note the magics in an M2 chunk table are stored in reading order (`MD21`),
unlike ADT/WDT/WMO where they are byte-reversed (`REVM` for `MVER`). The chunk
reader detects which convention a file uses rather than assuming.

### Header

The MD20 header layout is identical between 264 and 274 — 0x130 bytes, or
0x138 when `global_flags & 0x08` adds the texture-combiner-combo array. Nothing
moves; only the contents need clamping.

### Structs that changed shape

| Struct | 264 | 272+ | Downgrade |
|---|---|---|---|
| `M2Sequence` (64 B) | `uint32 blend_time` at 0x24 | `uint16 blend_time_in`, `uint16 blend_time_out` | keeps the longer of the two |
| `M2Camera` | 100 B, `float fov` at 0x04, straight after `type` | 116 B, no scalar; `M2Track<M2SplineKey<float>> fov` appended | first key of the track (same units: 0.785 in both for the same camera), or 0.9749 rad |
| `M2Particle` | 476 B | 492 B | fields mapped individually (below) |

The FoV scalar's position matters more than its value: written after the
roll track instead, every later field reads four bytes early, the near clip
becomes the far clip and the three tracks point at garbage. That was checked
against 820 cameras in genuine 3.3.5a models.

Everything else — `M2CompBone` (88), `M2Texture` (16), `M2Material` (4),
`M2Color` (40), `M2TextureWeight` (20), `M2TextureTransform` (60),
`M2Attachment` (40), `M2Event` (36), `M2Light` (156), `M2Ribbon` (176) — is
byte-identical across the range, and the test suite asserts every one of those
sizes.

### Particles

Cataclysm reused the two bytes at 0x2C (`particle_type`, `head_or_tail`) for
multi-texture parameters and appended two 8-byte parameter blocks, giving 492.
The downgrade zeroes the reclaimed bytes, drops the appended blocks, and:

- clears flag `0x10000000` (multi-texture), keeping the first of the three
  5-bit texture indices packed into the `texture` field;
- unpacks gravity where flag `0x800000` says the keys are packed vectors
  (`int8 x, int8 y` in 1/128ths, `int16` magnitude × 0.04238648) — 38% of the
  emitters in 12.1. Read as floats those keys are garbage, often NaN. 3.3.5a's
  gravity `g` is the vector `(0, 0, -g)`, so the key becomes the negated
  vertical part; a sideways pull is lost and reported. Genuine CrackElfMale
  carries 1.0417 where retail packs `(0, 0, -24)`, which unpacks to 1.0173.
  Keys kept in an `.anim` cannot be unpacked there and are dropped, with a note;
- masks flags to `0x000FFFFF`, the bits 3.3.5a's own 25,440 emitters use;
- maps emitter types 3 (spline, Cataclysm) and 4 (bone, Legion) to 1 (plane),
  since 3.3.5a implements only plane and sphere;
- clamps blend modes above 6 to additive.

### Feature clamping

| Field | 3.3.5a range | Above it |
|---|---|---|
| `M2Material.blending_mode` | 0–6 | 7 (`BlendAdd`) → 4 (`Add`); anything higher → 2 (`Alpha`) |
| `M2Material.flags` | `0x1F` | cleared |
| `M2CompBone.flags` | `0x3FF` | cleared (`0x400` kinematic, `0x1000` helmet-scaled are Cata+) |
| header `global_flags` | `0x0B` | cleared |
| `M2Sequence.flags` | `0xEF` | cleared; a sequence Legion marks "in the model" with `0x100` gets Wrath's `0x20` (no genuine sequence has `0x100`, `0x200` or `0x800`) |
| `M2Texture.type` | ≤ 15 | a hardcoded texture when it names a file; otherwise type 11, which 3.3.5a leaves empty |
| `M2Texture.flags` | `0x3` | cleared |

An empty name — a replaceable texture's filename, a particle's geometry or
recursion model, a model with no internal name — is written as a single NUL
with a count of 1, as every genuine 3.3.5a model writes it. A count of 0 with
offset 0 points a reader that skips the count at the file's own `MD20`.

Sequences retail lists in `AFID` with FileDataID 0 have no `.anim` anywhere
(763 entries across 235 models); they are not reported missing, and are marked
as embedded like any other sequence with no keyframes.

A texture slot with nothing to load — a guild-emblem or character-extra type
(16 and up), or a hardcoded slot whose FileDataID is 0 — is never written as a
hardcoded texture with an empty name, which sends 3.3.5a and Noggit looking
for a file called "". It becomes type 11 ("monster skin 1"), filled from
CreatureDisplayInfo on a creature and empty on anything else, which is what
retail draws there too.

### Skeletons

Legion moved bones, key bone lookups, attachments, global loops and the whole
sequence table out of the M2 and into a `.skel` referenced by `SKID`, so that
every variant of a race could share one rig. 3.3.5a has nowhere to put that.
The converter loads the skeleton, follows its `SKPD` parent chain (first
definition of each section wins), and folds the sections back into the model.

A rigged model whose `.skel` cannot be found is a hard failure by default,
because the alternative is a model with no bones and no animations that looks
converted. `--allow-missing-skeleton` opts into that.

### Too many vertices

A `.skin` addresses model vertices through a `uint16` array, so no profile —
modern or Wrath — reaches past vertex 65 535. A model with more is
unrenderable by *any* client, which is why the converter tries compaction
before it gives up: geometry no submesh draws is dropped and the skins are
renumbered, which is lossless and usually settles it.

A model still too large after that is split into several `.m2` files sharing
one rig, materials and textures, with model-wide effects, attachments and
collision left on the first piece. Opt-in (`--split-models`), because the extra
pieces are assets nothing references yet.

---

## SKIN — draw data

3.3.5a's header is 48 bytes; Legion appended an `M2Array<M2ShadowBatch>` for
its shadow pass, giving 56. The two are told apart by where the payload starts
and whether the candidate array is plausible.

- Shadow batches are dropped; 3.3.5a draws model shadows from a blob texture.
- `M2Batch.textureCount` (offset **0x0E**) is clamped to 2.
- `M2Batch.shader_id` (0x02) keeps its `0x8000` combiner form only when the
  model carries a combiner table; otherwise it resets to 0.
- `M2Batch.textureCoordComboIndex` (0x12) names a run of the model's texture
  unit lookup table: 0 for UV set 0, 1 for UV set 1, -1 for environment
  mapping. 3.3.5a builds the shader from it and never checks the index.
  Retail writes that table **empty** (all 1,494 models sampled from 12.1) and
  picks the vertex shader from the shader id instead, so every batch indexes
  past the end. Such a batch is pointed at a run built from its shader — for a
  `0x8000` id, the vertex shader retail's shader table names
  (`Diffuse_T1_Env` → `0, -1`); otherwise `0, 1` — shared with any batch that
  needs the same run. Batches already in range are untouched.
- `M2SkinSection.boneCount` above 256 and `boneInfluences` above 4 are
  reported, not silently accepted.
- `boneCountMax` (header 0x2C) is the bone palette the profile was built for.
  Retail writes 0 in every skin; every one of 3.3.5a's 23,941 skins names the
  smallest of 21, 53, 64 and 256 that holds its largest submesh's bone count
  (41 name a larger one). A 0, or a value smaller than a submesh needs, is
  replaced by that size.
- `M2SkinSection.Level` is **not** a LOD number: it carries the high 16 bits of
  `indexStart`, which is how a skin addresses more than 65 536 triangle indices
  without widening the field. Any tool that rebuilds a skin has to write it, or
  large submeshes silently wrap.
- `vertexStart` has no such extension, so a skin's vertex list tops out at
  65 536 entries; `indexCount` is a plain `uint16`, capping one submesh at
  21 845 triangles.

---

## ANIM — external animations

3.3.5a reads a flat blob whose offsets come from the model's per-sequence track
sub-arrays. Legion wrapped it in `AFM2`, and a model rigged by a `.skel` splits
it by who addresses it, each chunk counting offsets from its own start:

| Chunk | Addressed by |
|---|---|
| `AFM2` | the model's own tracks — colours, texture animation, particles, and any bones or attachments the model defines itself |
| `AFSB` | the skeleton's bones: on a retail character, nearly every keyframe in the file |
| `AFSA` | the skeleton's attachments |

Converting lays them end to end — `AFM2` at 0, then `AFSB`, then `AFSA`, each
aligned to four bytes — and adds each chunk's new position to the offsets of
the tracks that address it. `AFSB` is not optional: on `humanmale_hd` the
`AFM2` payload of an animation is 112 bytes and its `AFSB` is 78 896, and every
bone span fits the latter exactly. A chunk is only placed after every span
that should address it is checked to fit; otherwise it is dropped with
`anim.skeleton.unfit` and no offset moves.

Relocation covers the sequences the file is named after *and* the aliases that
play them (sequence flag `0x40`, pointing through `alias_next`). An alias has
no `.anim` of its own and carries the same spans as its target, so it has to
move with it.

**Tracks read before their sequences are known.** A skeleton-rigged model has
no sequences of its own, and a skeleton's sequences may come from its parent,
so the parser cannot tell which sub-arrays are external when it reads them. An
external offset usually lands inside the file being read — a skeleton's bone
chunk runs to megabytes — and reads back as plausible keyframes. The spans are
kept verbatim, and once the sequence list is final (after the skeleton chain is
flattened, and again when the skeleton is merged into the model) every
external sub-array is re-marked and whatever was read at its offset is
discarded.

**Sequences with nothing to load.** Retail leaves many sequences without the
embedded flag (`0x20`) that no track has a keyframe for and no `.anim` exists
for — 61% of external sequences in a 25,000-model sample, nearly all on item
and character models. 3.3.5a and Noggit look for `<model><id>-<sub>.anim` for
every such sequence, so the converter sets `0x20` on them: the keyframes are
"in the model", where there are none, which plays the same with no lookup.
(Noggit also spells that name without zero padding and so never finds a real
`.anim`; it reads the offsets from the model instead. For the 12,998 models
placed on a sample of 1,500 converted tiles, no such read goes past the end.)

---

## BLP — textures

The container is unchanged: a 1172-byte header, then up to 16 mip payloads.
What changed is the encodings in use. 3.3.5a samples palettised (compression
1), DXT1/DXT3/DXT5 (compression 2, `alpha_type` 0/1/7) and raw BGRA
(compression 3). Legion normal maps use `alpha_type = 11` (BC5), which it
cannot decode at all.

- A texture already in a supported encoding, power-of-two and within the size
  cap, is re-emitted with its block data **byte for byte identical**, so
  repeated conversions never accumulate loss.
- BC5 is decoded (reconstructing Z into blue) and re-encoded. 3.3.5a has no
  normal-mapped shader path, so the result is only useful as diffuse or
  overlay art, and the report says so.
- Non-power-of-two dimensions are resized: the old client only mipmaps
  power-of-two textures.
- A copied texture's alpha depth is made to match its blocks. 3.3.5a picks the
  block format from the depth first (0 or 1 means DXT1) and only then from the
  alpha type, so DXT5 blocks declaring depth 0, or the 33 retail textures
  declaring depth 72, would decode as the wrong format. DXT1 gets 1 unless it
  says 0 or 1; DXT3/DXT5 get 8 unless they say 4 or 8.

Every one of the 828,628 textures a full 12.1 build writes passes the loader's
checks: magic and version, power-of-two sizes, every mip present down to 1×1
and inside the file, and each mip at least as large as its dimensions need
(genuine textures sometimes store their 1- and 2-pixel mips short, and so do
634 converted ones, copied as they are). The largest are 4096 pixels; 3.3.5a's
own top out at 1024.
- `--texture-format auto` picks DXT1 when the image is opaque, DXT1 with
  punch-through when alpha is only 0 or 255, and DXT5 when it has gradients.

---

## WMO — world objects

The version stayed 17, which is why modern WMOs look deceptively compatible.

### Root

| Modern | 3.3.5a | Handling |
|---|---|---|
| `MODI` (doodad FileDataIDs) | `MODN` name table | rebuilt; `MODD.nameIndex` changes from an array index to a byte offset |
| `MOSI` (skybox FileDataID) | `MOSB` filename | rebuilt |
| `GFID` (group FileDataIDs) | `<root>_000.wmo` … | groups renamed |
| no `MOTX`, FileDataIDs in `MOMT` | `MOTX` + byte offsets | rebuilt |
| `MOHD` `uint16 flags` + `uint16 numLod` | one `uint32 flags` | flags masked to `0x0F`, LOD count reported |
| `MOUV` `MAVG` `MAVD` `MBVD` `MOLS` `MOLP` `MOP2` … | — | dropped |

`SMOMaterial.shader` above 6 resets to diffuse and `blendMode` above 6 to
alpha; flags are masked to `0x1FF`. The three textures sit at 0x0C, 0x18 and
**0x24**; 0x20 between them is the ground type (footsteps and ground effects)
and is left alone. A modern material marks an absent texture with FileDataID
0, but in `MOTX` offset 0 is the first texture's name, so an absent texture
points at an empty entry, as in 3.3.5a's own WMOs.

### Groups

| Modern | 3.3.5a | Handling |
|---|---|---|
| `MOVX` (uint32 indices) | `MOVI` (uint16) | converted; over 65 535 vertices is a hard failure |
| `MPY2` (uint16 material ids) | `MOPY` (uint8) | converted; ids above 255 become the collision-only material `0xFF` |
| 3+ `MOTV` / `MOCV` layers | 2 | extra layers dropped and the header flags corrected to match |
| more than 65 535 vertices | several groups | split, with the root updated to match |
| `MOBS` `MOLS` `MOLP` `MOC2` `MOPL` `MDAL` … | — | dropped |

Shadowlands reused the first twelve bytes of each `MOBA` batch — Wrath's
bounding box — for a wide material id. When `MPY2` is present (the same era),
the boxes are recomputed from the group's own vertices rather than left as
values the client would cull against.

`MOGP`'s `flags2` and split-group indices are zeroed; Wrath has no `flags2` and
reads the last word as padding.

**Sub-chunk order is part of the format.** 3.3.5a's own groups — and Noggit,
which reads them sequentially — have `MOPY, MOVI, MOVT, MONR, MOTV, MOBA`, then
whichever of `MOLR, MODR, MOBN/MOBR, MOCV, MLIQ` the flags declare, and only
then the second `MOTV` and second `MOCV`. All six leading chunks are always
present (3.3.5a writes an empty `MOBA` for a group that draws nothing), so a
collision-only group gets a zeroed `MOTV` sized to its vertices and an empty
`MOBA`. Writing the second `MOTV` straight after the first made readers take it
for `MOBA` — in Noggit, bad batch data, an out-of-range subscript and a crash
on 53% of converted groups.

**Flags follow the chunks.** 3.3.5a reads an optional group chunk wherever its
flag is set, without checking the chunk's name; Noggit checks and logs "Broken
header". In all 12,708 of 3.3.5a's own groups the flags match the chunks
exactly. Retail keeps "has lights" (`0x200`) on 5,850 groups whose lights moved
out of `MOLR`, so every flag for an optional chunk — `MOLR` 0x200, `MODR` 0x800,
`MLIQ` 0x1000, `MOBN`/`MOBR` 0x1, the portal batches 0x400 (never written) — is
set from what is actually written. That includes the parts of a split group,
where only the first part keeps the liquid.

**Every group has a collision tree.** Every genuine group does too, and a group
without one is a group players walk through; 113 retail groups ship none. Such
a group gets a tree built over its triangles, the same way a split group's is.

### Root counts

`MOHD`'s first word is the **`MOMT` entry count**, which readers size the
material array from; 3.3.5a's own WMOs always agree. It was being written as
the number of distinct `MOTX` names, which is smaller whenever materials share
a texture — 65% of converted roots. The light count is likewise set from
`MOLT`, since a few retail roots declare more lights than they carry, and the
doodad count from `MODD`: 1,488 retail roots declare more doodads than they
hold.

**Ambient colour.** Legion moved a world object's ambient light into `MAVG`
(global, per doodad set) and `MAVD` (local volumes), which its client prefers
over `MOHD`'s colour; 4,366 retail roots leave that colour black, which in
3.3.5a — which reads only `MOHD` — leaves interiors without ambient light. The
header takes the default set's global colour where one is set, or, when the
header is black and no global colour exists, the first volume's. For the 1,984
roots that exist in both 3.3.5a and 12.1, 1,955 headers already agree.

A material whose newer shader keeps its diffuse texture in `texture_2`
(shader 23 leaves `texture_1` empty) falls back to diffuse with that texture
moved into `texture_1`, and the texture ids it keeps in `color_3`, `flags_3`
and the runtime words are cleared, since 3.3.5a reads those as colour and
flags.

### Splitting an oversized group

`MOVI` is `uint16`, so a 3.3.5a group tops out at 65 536 vertices — which is
exactly why Shadowlands introduced `MOVX`. Those groups cannot be narrowed, but
a WMO is a *collection* of groups and nothing stops it having more, so an
oversized group becomes several and the root is updated:

* `MOHD.nGroups` grows;
* each new part gets an `MOGI` entry with its own bounding box, the original
  group's flags, and a name appended to `MOGN`;
* the parts are written as `<root>_NNN.wmo` after every existing group, so the
  numbering the root already uses stays put.

Cuts are made on render-batch boundaries where possible, so in the common case
no vertex is duplicated. Each part's `MOBA` batches get corrected index ranges
and recomputed bounds, and **the collision tree is rebuilt** — `MOBN`/`MOBR`
index triangle numbers the split invalidates, and a group without a valid tree
is one players walk through. Liquid stays with the first part rather than being
rendered several times over.

---

## ADT — terrain

Cataclysm split each tile into `Zone_32_48.adt` (heights), `_tex0` (texture
layers), `_obj0` (placements) and LOD variants, and dropped `MCIN` because it
no longer needed the index. 3.3.5a reads one monolithic file with `MCIN`.

Merging walks all 256 map chunks across the three files and rebuilds each
`MCNK`:

- sub-chunk offsets are relative to the start of the chunk **including** its
  8-byte header, so all of them are recomputed;
- `MCRD` (doodad refs) and `MCRW` (WMO refs) concatenate back into one `MCRF`,
  with both counts written into the chunk header at 0x10 and 0x38;
- `sizeAlpha`, `sizeShadow` and `sizeLiquid` include the sub-chunk header, so
  a chunk with no liquid still declares 8;
- Cataclysm's 8×8 hole mask (flag `0x10000`) folds down to the 4×4 mask at
  0x3C. A split root finds its sub-chunks by walking them, so it keeps that
  mask in the `ofsHeight`/`ofsNormal` words at **0x14**; 0x40 still holds the
  low-quality texture map, as in Wrath, and is kept. (Folding 0x14 matches the
  3.3.5a tile's own mask on all 24,832 Northrend and Outland chunks checked;
  reading 0x40 instead punched holes through 29% of them.)
- at most four texture layers: retail allows eight (15,000 Azeroth chunks have
  more than four). The upper layers that show least — alpha times what the
  layers above leave uncovered — are dropped, the kept alpha maps carried over
  byte for byte into a rebuilt `MCAL`. A layer whose texture has no file
  (FileDataID 0) is dropped too, the lowest textured layer becoming the base
  if need be; one whose alpha map runs past `MCAL` is dropped rather than
  painted over everything beneath. The chunk's 8×8 low-quality texture map
  (0x40, two bits a cell) — which picks each cell's ground-effect doodads and
  what distant terrain draws — is renumbered to the kept layers, a cell whose
  layer was dropped taking the kept layer that shows most there;
- "has shadows" (0x1) and "has vertex colours" (0x40) follow what is written —
  retail leaves both set on chunks it no longer ships `MCSH`/`MCCV` for;
- `MCLV`, `MCMT`, `MCBB` and `MCDD` are dropped.

`MHDR`'s offsets, which are relative to the start of its own payload and point
at chunk headers, are rewritten. `MDID` FileDataIDs become an `MTEX` string
table. Legion pointed terrain at `<name>_s.blp` (diffuse with a specular mask
in its alpha); the plain `<name>.blp` 3.3.5a names still ships beside it for
all but 9 of 2,243, and is used whenever it exists. `MTXP`'s per-texture flags
become `MTXF` (only the cube-map bit, 0x1, means anything to 3.3.5a, and the
chunk is written only when a texture sets it); `MHID` and the blend-mesh
chunks are dropped. Placement flags are cut to what 3.3.5a defines (`MDDF`
0x1/0x2, `MODF` 0x1) and a WMO's Legion scale word, padding to 3.3.5a, is
cleared, since the old client cannot scale a WMO. Every one of
`MTEX`, `MMDX`, `MMID`, `MWMO`, `MWID`, `MDDF` and `MODF` is written even when
empty, because readers seek to them through `MHDR` without checking for 0.

### Placements

A doodad (`MDDF`, 36 bytes) or WMO (`MODF`, 64 bytes) placement names its
asset through `nameId`, an index into `MMID`/`MWID`. Battle for Azeroth added a
flag — `MDDF` `0x40`, `MODF` `0x8` — that makes `nameId` a FileDataID, and
retail now writes every placement that way with no name tables at all (99% of
the 10.5 million doodad placements in 12.1's maps). 3.3.5a indexes an empty
table with it; Noggit crashes loading the tile. Each such entry's FileDataID is
looked up in the listfile, the path is added to a rebuilt name table once, and
the flag is cleared. An unresolvable one follows `--unresolved`: fail the tile,
a placeholder path, or drop the placement — in which case every `MCRF`
reference is renumbered to match. A WDT's global WMO gets the same repair.

**Doodad sets.** Shadowlands lets one WMO placement show several doodad sets
at once: with `MODF` flag `0x80`, `doodadSet` is an index into `MWDR`, whose
range of `MWDS` entries lists the sets. 12.1 does this on 1.9% of placements,
683 of them in the rebuilt Quel'Thalas in Azeroth, always with more than one
set besides the default (an inn's furniture, its lights, its foliage). Read as
a set, the `MWDR` index names an arbitrary one, often one the WMO does not
have. 3.3.5a shows set 0 on every placement plus the one `doodadSet` names, so
the listed set with the most doodads (read from the WMO's own `MODS`) is kept
and the flag cleared; a plain `doodadSet` past the WMO's last set becomes 0.

Checked against the retail files for every Azeroth and Kalimdor tile and 1,500
others — 992,768 map chunks — the merged tiles carry heights, normals, vertex
colours, shadows, sound emitters, area ids, positions, the no-effect-doodad
mask, holes (folded), doodad and WMO references and placements across
unchanged; only chunks that lost layers differ in `MCLY`/`MCAL`. Where a map
chunk's header and the tile's `MCDD` could disagree about disabled detail
doodads, no retail tile sampled uses `MCDD`.

### Liquid (`MH2O`)

An instance's second word is the *liquid vertex format* in 3.3.5a, and says
what its vertex block holds per vertex: 0 height + depth (5 bytes), 1 height +
UV (8), 2 depth (1). Cataclysm added 3, all three (9). From Warlords of
Draenor, a value of 42 or more is a `LiquidObject` id instead, and 99% of
retail instances are written that way. The format then has to be looked up,
and the rule was settled against the vertex block sizes of 94,000 retail
instances, all of which agree: liquid type 2 (Ocean) is always depth-only;
any other type uses its `LiquidMaterial`'s format.

3.3.5a chooses the format by the kind of liquid (`LiquidType.SoundBank`), as
its own Northrend tiles and Noggit's writer both show: magma and slime use 1,
ocean lying flat at height 0 uses 2, everything else 0. Each instance is
decoded in its retail format and re-encoded in that one — heights missing from
the source are filled from the instance's minimum, depth as fully deep, UVs
the way Noggit lays out new ones — and data the target has no room for is
reported. Without the install's `LiquidType`/`LiquidMaterial` (no `--dbd`, or a
folder build), the block sizes decide the source format. A tile already in
3.3.5a formats is left byte for byte.

`LiquidType.dbc` keeps retail's types, each with textures and a material 3.3.5a
can draw (see *LiquidType textures* under the databases), and
`LiquidMaterial.dbc`'s format 3 becomes 1.

---

## WDT — map index

`MAID` (per-tile FileDataIDs) is dropped, along with `MANM`, `MPL2`/`MPL3` and
the rest. `MPHD.flags` is masked to `0x0F`. A map that is one WMO places it by
FileDataID with no `MWMO`; the name is looked up and written back, as for tile
placements.

One flag matters more than the others. `MPHD.flags & 0x4` ("big alpha") tells
the client `MCAL` holds 8-bit alpha maps rather than 4-bit ones. Cataclysm and
later always write the 8-bit form, so **the converter sets that bit** — without
it every terrain texture blend renders wrong. The ADT converter warns about the
same thing, because the two files are often converted separately.

---

<a name="wdl"></a>
## WDL — low-resolution heightmaps

What the client draws on the horizon before real terrain streams in. One 17x17
grid of 16-bit heights, plus the 16x16 grid between those points, stands in for
each of the map's 64x64 tiles.

| Chunk | Holds | Converted to |
|---|---|---|
| `MAOF` | 64x64 absolute file offsets, one per tile | rewritten for the new layout |
| `MARE` | the 17x17 and 16x16 height grids (1090 bytes) | kept verbatim |
| `MAHO` | one 16-bit hole mask per inner row | kept verbatim |
| `MWMO`/`MWID`/`MODF` | low-detail WMO placements | kept, or emitted empty |
| `ML*` | Legion's LOD mesh: vertices, indices, skirts, liquid, placements | dropped |

The heightmap itself never changed. What Legion added is a separate thing
sharing the file — a real LOD mesh, with its own copies of the doodad and WMO
placements, for a renderer 3.3.5a does not have.

The catch is `MAOF`: its 4096 entries are absolute file offsets, so dropping
anything ahead of the `MARE` blocks moves every one of them and they all have
to be rewritten. Wrath also expects the low-detail WMO tables that Legion
stopped writing, so empty ones are emitted to keep the file the shape the old
client reads. An offset that does not lead to a `MARE` of the right size is
reported (`wdl.tiles.unreadable`) and the tile left empty, rather than followed
into whatever happens to be there.

---

<a name="liquid"></a>
## WLW / WLQ / WLM — liquid volumes

Retail ships a handful of these beside its maps — 106 in 12.1.0.69814, all
unnamed — and **3.3.5a never loads them**. It takes liquid from each terrain
tile's `MCLQ`/`MH2O` and each world object's `MLIQ`; none of its archives
contains a liquid volume, and its executable has no file name pattern that
could ask for one (it has the `.adt` and `.wdt` ones). They are recognised by
signature and skipped with that reason.

The header is still read, for `inspect`. This layout is proven on every liquid
volume available (106 retail, 1 MoP Classic): each file is exactly
`16 + 360 × blocks + 4 + 76 × secondary blocks + 1` bytes.

| Offset | Type | Field |
|---|---|---|
| 0x00 | 4s | magic `*QIL` (`LIQ*` also accepted) |
| 0x04 | u16 | version (2 in every real file) |
| 0x06 | u16 | unknown, always 1 |
| 0x08 | u16 | liquid type: a `LiquidType` row id |
| 0x0A | u16 | padding |
| 0x0C | u32 | block count, then 360-byte blocks |
| … | u32 | secondary block count, then 76-byte blocks |
| … | u8 | trailing byte |

An earlier reading took `0x06` as the liquid type and `0x08` as the block
count, so a valid empty volume (21 bytes) was reported as "5 blocks that cannot
fit".

---|---|---|
| 0x00 | `magic` | `LIQ*`; accepted in either byte order |
| 0x04 | `version` | 3.3.5a reads 0 and 1 |
| 0x06 | `liquidType` | water, lava, slime … |
| 0x08 | `blockCount` | volume blocks that follow |

What this tool does is deliberately narrower than a conversion. The header is
well understood; the block layout after it is not documented well enough to
rewrite safely, and a liquid volume rewritten wrongly puts swimmable water
where there is none. So the version is checked and the body passed through
untouched: a version Wrath reads is already the file the old client wants, and
a newer one is refused by name rather than copied, because a client that
cannot parse the body is worse off with the file than without it.

If a build turns out to ship a version this refuses, that is the point at
which the block layout has to be worked out — and the report says so, instead
of leaving a misparsed file to be discovered in-game.

---

<a name="minimaps"></a>
## Minimaps

Retail keeps minimap tiles at `World\Minimaps\<map>\mapXX_YY.blp` and
`World\Minimaps\WMO\<path>\<name>_NNN_XX_YY.blp`. 3.3.5a does not look there:
it reads `Textures\Minimap\md5translate.trs` and loads each tile from
`Textures\Minimap\<hash>.blp`:

```
dir: Azeroth
Azeroth\map32_48.blp	1fcd95d6d410e7557d6b62081c5e87b5.blp
dir: WMO\Azeroth\Buildings\Castle
WMO\Azeroth\Buildings\Castle\castle01_000_00_00.blp	81676ea7…blp
```

(CRLF lines, directories in case-insensitive order — as the file in 3.3.5a's
own archives is written.) Tiles are written under the MD5 of their index name,
so a tile's destination is known before it converts, and the index lists
every minimap in the build — it replaces the client's own — with map
directories spelt as `Map.dbc` spells them. Retail's `noliquid_` variants have
no 3.3.5a counterpart and stay where they were.

---

<a name="sidecars"></a>
## Map sidecars — what is deliberately left out

A modern map folder holds more than the tile. These share an extension with
the map files they sit beside, so each is recognised by a chunk only it has:

| File | Marker chunks | Holds |
|---|---|---|
| `_lgt.wdt` | `MPLT` `MPL2` `MPL3` `MLTA` | per-tile light definitions and animations |
| `_occ.wdt` | `MAOI` `MAOH` | terrain occlusion hulls and heightmap |
| `_fogs.wdt` | `MVFX` `VFOG` | volumetric fog |
| `_mpv.wdt` | `MPVD` | particulate volumes |
| `_lod.adt` | `MLHD` `MLVH` `MLLL` | the LOD terrain mesh |
| `_tex1.adt` `_obj1.adt` | — | high-detail texture and object variants |

3.3.5a keeps none of this and never looks for the files, so they are skipped
with what they held rather than copied into the patch. The check runs *after*
the map formats identify themselves, because a real `.wdl` carries the same
LOD chunks a `_lod.adt` does and is told apart by the `MAOF` that makes it a
heightmap.

`_tex0` and `_obj0` are the exception: they are merged into the tile, and the
report records them as carried by it rather than leaving them unmentioned.

---

## CASC — reading a game install

```
.build.info           active build's config hash
Data/config/xx/yy/…   build config: root and encoding file hashes
Data/data/*.idx       EKey prefix -> (archive number, offset, size)
Data/data/data.NNN    a 30-byte entry header, then a BLTE stream
```

Resolving one FileDataID: root gives a **content** key, encoding turns that
into an **encoding** key, the index says where that lives, and BLTE decodes it.

- BLTE header sizes and chunk sizes are **big-endian**, unlike every other
  Blizzard format. Chunk modes: `N` stored, `Z` zlib, `4` LZ4, `F` nested,
  `E` encrypted (Salsa20 or ARC4, with the IV mixed with the chunk index).
- Index entries are 18 bytes: a 9-byte key prefix, a 40-bit big-endian position
  whose top 10 bits are the archive number, and a little-endian size.
- Root blocks store FileDataIDs as deltas (`id = previous + delta + 1`).
  `contentFlags`, `localeFlags` and the record count are **unsigned** —
  `localeFlags` is `0xFFFFFFFF` for "every locale".
- The root has three layouts. Before 8.2 it is bare blocks; from 8.2 a `TSFM`
  header comes first; from 10.1.7 that header starts with its own size (`0x18`)
  and a version. **Version 2** (11.1 onwards) changes the block header from
  12 bytes (`records, contentFlags, localeFlags`) to 17
  (`records, localeFlags, flags1, flags2, flags3:u8`), with the platform and
  violence bits in `flags1` and no-name-hash (`0x10000000`) in `flags2`.
  Reading a version 2 root with the old header finds a garbage record count in
  the first block: on 12.1.0.69814 that is 6 894 of 1 931 507 files.
- One FileDataID can be listed by several blocks, and the first is not the one
  to take. On 12.1.0.69814, 10 831 files list a **low-violence** variant
  (`contentFlags & 0x80`) *before* the normal one. Blocks are ranked — the
  wanted locale, then not low-violence, then not macOS-only (`0x10` without
  `0x8`) — and each FileDataID resolves to the best-ranked block that has it.
  An unknown header version is refused rather than read.

By default only local storage is read. With `--casc-cdn`, a file the install
does not store is fetched: the CDN config lists the build's archives, and
`Data/indices` already holds every archive's `.index`, the archive-group index
that merges them (key, size, then a 2-byte archive number and 4-byte offset,
big-endian) and the loose-file index. A CDN index is 4 KiB blocks of sorted
entries, a table of each block's last key and hash, and a 28-byte footer; it is
searched in place. Only the file's own bytes are requested (an HTTP range into
its archive), and they are used only if the BLTE header hashes to the encoding
key and the decoded content hashes to the content key.

---

## DB2 — client databases

Cataclysm renamed `.dbc` to `.db2`; Legion stopped storing records as structs.
A WDC-family table packs each column to the narrowest width its values need,
hoists constant columns into a side table, replaces repeated values with
indices into a palette, and scatters records across sections that may be
encrypted.

Magics read: **WDC1** (Legion 7.3) through **WDC5** (The War Within). Earlier
ones — `WDB2` to `WDB6`, Cataclysm through Legion 7.2 — are refused. They have
no field-storage table and measure string offsets from the string block rather
than from the field, so reading one as a WDC yields plausible wrong values
instead of an error.

Field storage types, all of which the reader handles:

| Type | Meaning |
|---|---|
| 0 `none` | plain bits in the record, one run per array element |
| 1 `bitpacked` | a narrow field at a bit offset |
| 2 `common_data` | a side table keyed by **record id**, with a constant default |
| 3 `bitpacked_indexed` | the record holds an index into a palette |
| 4 `bitpacked_indexed_array` | as above, but each slot holds a whole array |
| 5 `bitpacked_signed` | as 1, sign-extended |

Also handled: id lists (the id lives outside the record), copy tables (a row
cloned under a new id), relationship columns, and sparse offset maps.

Details a real build settled, each of which the synthetic tables had wrong:

- **String offsets count across every section.** A string field's value is
  measured from the field's position among *all* sections' records and lands
  in all sections' string tables laid end to end. A table with an encrypted
  section of unreleased rows has two or more sections, and measuring from one
  section read `ay` for "Dun Morogh".
- **An inline array's `field_size_bits` is the whole array**; elements split it
  evenly (`GeoBox[6]` is 192 bits).
- **A value is cut to the column's DBD width**, not the storage's: pallet and
  common entries are 32 bits and Blizzard leaves bits above a narrower column
  set (`ObjectEffectPackageID<16>` entries read `0x76xxxx`). Signed columns are
  sign-extended at that width.
- **A bare `$relation$` is an ordinary inline column**; only
  `$noninline,relation$` lives in the relationship map.
- **Encrypted sections** are skipped when their bytes read as zero, which is
  how a reader without the key sees them: the CASC layer zeroes an encrypted
  chunk it has no key for, for databases only.

A **sparse** table has no fixed-size record block: an offset map gives each
present row a `(offset, size)` into a region of packed records whose strings are
inline (UTF-8, like every other string) and whose arrays take their per-element
width. Where the map sits differs by era — WDC2 points at it from the section
header; WDC3 and later follow the records with the id list, the copy table, the
offset map, the offset map's own id list and only then the relationship data,
whose entries name a row by its id rather than its position. Because that
layout is easy to get wrong and a wrong read yields nonsense rather than an
error, everything is checked: offsets must land inside the record region, the
number of present rows must match the section header, and walking a record's
columns must consume exactly the bytes the map allotted it. Any mismatch raises.

A file whose header this reader has misjudged is caught the same way — the
record block is checked against the file's actual length before any row is
decoded.

Note `contentFlags`, `localeFlags` and the record count in a root-style block
are **unsigned** — reading `localeFlags` as signed makes `0xFFFFFFFF` ("every
locale") come out as -1 and match nothing.

### The 3.3.5a side

A definition file is a series of blank-line-separated blocks, and only builds
from WDB6 on carry a `LAYOUT` hash: a DBC-era block — every 3.3.5a table —
starts straight with its `BUILD` lines. Reading one as part of the block above
it made Map 166 fields instead of 66.

A Wrath `locstring` is **17 fields**: a string offset per locale, enUS first,
then a mask (`0x00FF01FE` on every row of an enUS client). A modern file stores
one string; it becomes slot 0 and the mask is written as a constant.

Five Wrath tables pack columns into single bytes (CharBaseInfo, CharStartOutfit,
PowerDisplay, SpellChainEffects, SpellItemEnchantmentCondition). The DBD widths
reproduce the record size of all 245 tables of a clean client, so the writer
uses them. A table with no id column is keyed by the modern row id.

A table whose definition has no 3.3.5a layout did not exist in Wrath (none of
12.1's 951 such tables is among a clean client's 246) and is skipped.

### Sky: LightIntBand, LightFloatBand and LightSkybox

3.3.5a keeps a sky's day cycle in two tables addressed by arithmetic on the
`LightParams` id — eighteen colour bands at `id * 18 - 17 + n`, six float
bands at `id * 6 - 5 + n`, each up to sixteen (time, value) keys. Legion folded
both into `LightData`, one row per param per time of day, so they are rebuilt
from it for every `LightParams` row. Each band's column was identified against
a clean client over the 615 params whose key times are unchanged: colour bands
0–17 are `DirectColor`, `AmbientColor`, `SkyTopColor`, `SkyMiddleColor`,
`SkyBand1Color`, `SkyBand2Color`, `SkySmogColor`, `SkyFogColor`,
`ShadowOpacity`, `SunColor`, `CloudSunColor`, `CloudLayer1AmbientColor`,
`CloudEmissiveColor`, `CloudLayer2AmbientColor`, `OceanCloseColor`,
`OceanFarColor`, `RiverCloseColor`, `RiverFarColor` (93% of those rows come out
identical to 3.3.5a's; the rest are retail colour changes); float bands 0, 1
and 3 are `FogEnd`, `FogScaler` and `CloudDensity`. Bands 2, 4 and 5 have no
column any more and are written as the 1.0, 0.95 and 1.0 3.3.5a's own table
holds in 97–98% of its keys.

`LightSkybox.Name` is the skybox model in 3.3.5a. Retail moved the model to
`SkyboxFileDataID` and newer rows use `Name` for a description ("12ZAM Sky 01")
that readers would try to load as a file, so the name is the `.mdx` path of
that FileDataID.

### LiquidType textures

3.3.5a animates a liquid from `Texture[0]`, a `%d` pattern filled in with
frames 1 to 30 — every animation in its archives has exactly thirty — and draws
it with one of three materials. Legion's liquids take still textures (shallow,
deep, specular, shore) and new materials instead: 149 of retail's 199 types
have no usable animation, and two have one whose frames differ in size, which
Noggit cannot put in one texture array. A type whose thirty frames all exist
at one size keeps its textures and material; any other gets the textures and
material of 3.3.5a's own liquid of the same kind (`SoundBank`).

### GroundEffectDoodad

3.3.5a's `Doodadpath` is a bare name such as `ElwFlo01.mdl`, loaded from
`World\NoDXT\Detail\`; retail names the model by FileDataID. Models still in
that folder are named relative to it. The few hundred that moved
(`models\world\nodxt\detail`, expansion doodad folders) are also written into
it — under their own name, or with their FileDataID appended when another
model already has that name — and the table names the copy.

`GroundEffectTexture` is converted as retail has it, but retail's table is
smaller than Wrath's (19,788 rows against 24,981). Of the textured layers in a
sample of converted tiles, 32% name an id retail's table has, 7% an id only
Wrath's table has (half of Northrend's), and 31% an id neither has — retail
terrain pointing at rows retail itself does not ship, in the table or in the
install's hotfix cache, so those layers grow no clutter in either client.
Merging onto the client's own tables (`--template-dir`) restores the Wrath-only
ones.

### LoadingScreens and AreaTable

Legion moved loading screen images to FileDataID columns. `FileName` is the
narrow image's path (else the 16:9, wide or main image), `HasWideScreen` is 1
only where the wide image is the narrow one's name plus `wide` — the one name
3.3.5a derives — and `Name` falls back to the image's file name. All 344 of
12.1's screens then name an image in the build; left empty, entering a map
loads a file called "".

`AreaTable.MinElevation` no longer exists; it is written as -500, the value
3.3.5a's own table gives 2,298 of its 2,307 areas. `ExplorationLevel` (now
content tuning) and `LightID` (0 on every 3.3.5a area) stay 0.

### ItemDisplayInfo

Modern builds keep none of Wrath's names on the row. Measured against a clean
3.3.5a client on the 36,438 ids both share:

| Wrath column | Joined from | Agreement |
|---|---|---|
| `ModelName[0/1]` | `ModelResourcesID` → ModelFileData (+ ComponentModelFileData position, race) | 99.9 / 99.8% |
| `ModelTexture[0/1]` | `ModelMaterialResourcesID` → TextureFileData | 98.5 / 96.3% |
| `Texture[0..7]` | ItemDisplayInfoMaterialRes by `ComponentSection` → TextureFileData, gender stripped | 98.8–99.4% |
| `InventoryIcon[0]` | lowest ItemAppearance → `DefaultIconFileDataID` | 80.1% |
| `SpellVisualID` | ItemRangedDisplayInfo `CastSpellVisualID` | 100% |
| `GroupSoundIndex` | lowest item's `ItemGroupSoundsID` (1–24) | 93.6% where both are set |
| `HelmetGeosetVisID`, `GeosetGroup`, `Flags & 7` | direct | 100% |

Icons cannot do much better: an icon belongs to an item, several items share a
display, and the clean icon is among the modern candidates for only 85.5%.

The client looks names up in fixed places, so the files are also written where
it looks: `Helm_X` → `Item\ObjectComponents\Head\Helm_X_<race><M|F>.m2` (with
its skins), a component texture → `Item\TextureComponents\<Section>Texture\
<name>_<U|M|F>.blp` (modern names put a FileDataID after the gender letter), a
model texture beside its model, an icon in `Interface\Icons`. On 12.1 that is
45,419 extra paths, and every name the converted table uses then resolves.
A joined name that is some other file's own path in the build (an icon and a
cape texture can share one) is left to that file: whichever was written first
would otherwise keep the name, and 21 real files were refused that way.

### Getting to a .dbc

Three inputs have to line up:

1. **The data**, from the `.db2`.
2. **Column names**, from a DBD definition matched on the file's own layout
   hash. A `.db2` carries no names, and mapping by position across fifteen
   years of column churn is not safe, so a conversion without a definition
   refuses rather than guessing.
3. **The target layout**, from a mapping file and ideally from the user's own
   client `.dbc` used as a template.

Merging onto a template keeps its records and string block **byte for byte** and
appends to them; a converted row whose id the template already has replaces
that row. That is right for the world tables a converted map needs (its areas,
liquids and lights are retail's), and wrong for tables whose ids mean something
else to a 3.3.5a server — `--id-offset` moves new rows clear instead. Existing string offsets stay valid, so the merge needs to know
the types only of the fields it writes — getting a field type wrong elsewhere in
the row cannot corrupt anything. A template whose field count disagrees with the
mapping is a hard failure, which is the check that catches a mapping written for
a different build.

Two spellings the client insists on, handled by transforms:

* model paths in a DBC end in **`.mdx`**, not `.m2`; the client swaps the
  extension when it opens the file, so a `.m2` path simply does not load;
* texture variation columns hold a **bare filename** with no directory and no
  extension, resolved against the model's own folder.

---

<a name="assumptions"></a>
## Versions, and what happens outside them

Every converter applies one layout. These are the headers that say whether it
applies, and what each converter does when a file disagrees:

| Format | Understood | Older | Newer |
|---|---|---|---|
| M2 | 264–274 | refused — this tool converts down, not up | read with the newest schemas, and flagged |
| WMO | 17 | refused — v14 is a different layout wearing the same magic | read as v17, and flagged |
| ADT / WDT / WDL | `MVER` 18 | flagged | flagged |
| BLP | BLP2 v1 | BLP1 refused | unknown compression refused |
| liquid | 2 (proven); 0–1 header only | skipped — 3.3.5a never loads one | skipped |
| CASC root | header 0 (8.2), 1 (10.1.7), 2 (11.1: 17-byte blocks) | pre-8.2 bare blocks read | refused |

Nothing downstream branches on the ADT, WDT or WDL version, which is exactly
why reading it is worth doing: a file declaring something else is being parsed
on an assumption nobody stated.

A chunk in neither the Wrath set nor the known-modern set — one Blizzard adds
after this was written, or one nobody documented — is dropped, because there is
no other option, but it is named in the report and makes the file lossy. That
covers M2, WMO roots and groups, ADT (top level and inside each `MCNK`), WDT
and WDL.

---

<a name="assumptions"></a>
## Where the format is ambiguous, and how it is settled

Three things the published documentation leaves open used to be chosen for
internal consistency and left at that. None of them is a standing assumption
any more: each is either decided from data the file itself carries, or checkable
against something you already have.

**`.anim` track offsets: the `AFM2` payload, or the whole file?** Being wrong
by eight bytes does not crash, it animates wrongly, which is the worst kind of
wrong. It is no longer guessed. A model names, for each sequence it keeps
outside itself, the exact `(offset, length)` of every keyframe array the
`.anim` is expected to hold; those spans have to fit the file, and an `.anim`
exists to hold them and nothing else, so under the right reading the last one
also ends exactly where the file does. Both readings are tested against the
spans per file, and the payload is emitted to match whichever one they support
— `anim.offsets.payload` or `anim.offsets.file` in the report says which, and
`anim.offsets.unmeasured` says when the model named nothing to measure.
Written in `src/wotlkconv/m2/anim.py`.

**`MCIN[i].size`: the payload, or the payload plus the chunk header?** The
offset is unambiguous — it points at the `MCNK` magic — so a reader that seeks
there and then trusts the chunk's own size field, as the client does, cannot be
misled either way. Only a tool that takes `MCIN`'s size as the extent of the
chunk can be, and for that reader the header-inclusive value is the safe one:
it spans the whole chunk, where the payload-only value stops eight bytes short
and cuts the end off the last sub-chunk. That is the default, and
`--reference-adt` reads the convention straight off any genuine 3.3.5a tile by
comparing each entry's size against the size the chunk itself declares.
Written in `src/wotlkconv/adt/convert.py`.

**The 3.3.5a DBC layouts.** These used to be the least certain thing in the
project — hand-written field counts and column indices that came from
documentation rather than from a client. They are no longer written down at
all. DBDefs carries a `BUILD 3.3.5.12340` layout for every table that existed
in Wrath, naming each column in order with its type and array size, and that is
where the layout now comes from; a `--template` `.dbc` cross-checks the width
and hard-fails on disagreement. A table the definitions do not cover for that
build is refused rather than guessed at, because a `.dbc` records how many
columns there are and never what belongs in them. Written in
`src/wotlkconv/db/target.py`.
